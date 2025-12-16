import os
import json
import uuid
import re
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
import asyncpg
import aiohttp

# --- 1. КОНФИГУРАЦИЯ ---
class Config:
    # База данных
    DATABASE_URL = os.environ.get('DATABASE_URL')
    
    # Telegram
    TG_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
    TG_API_BASE = f'https://api.telegram.org/bot{TG_BOT_TOKEN}/'
    TG_BOT_USERNAME = os.environ.get('TELEGRAM_BOT_USERNAME', 'oopsmerchbot')
    
    # ID чатов
    TG_ADMIN_GROUP_ID = os.environ.get('TG_ADMIN_GROUP_ID')
    TG_REVIEWS_CHANNEL_ID = os.environ.get('TG_REVIEWS_CHANNEL_ID')
    
    # Тексты и ссылки
    ADMIN_SUPPORT_USERNAME = os.environ.get('ADMIN_SUPPORT_USERNAME', '@oopssupport')
    SITE_URL = os.environ.get('SITE_BASE_URL', 'oops-merch.ru')
    
    # URL самого приложения для Keep-Alive (Render URL)
    # Render автоматически ставит RENDER_EXTERNAL_URL
    APP_URL = os.environ.get('RENDER_EXTERNAL_URL') 

    # Реквизиты
    CARDS = {
        'sber': os.environ.get('SBERBANK_CARD', '2202203614486217'),
        'tbank': os.environ.get('TBANK_CARD', '2200702039512418'),
        'alfa': os.environ.get('ALFABANK_CARD', '2200154572801271')
    }

# --- 2. ЛОГИРОВАНИЕ ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("FastAPIBot")

# --- 3. ГЛОБАЛЬНЫЕ РЕСУРСЫ ---
class Resources:
    db_pool: asyncpg.Pool = None
    client_session: aiohttp.ClientSession = None

# --- 4. БАЗА ДАННЫХ ---
class DatabaseParams:
    ORDERS_TABLE = 'orders'
    ST_PENDING_AUTH = 'pending_phone_auth'
    ST_PENDING_NAME = 'pending_full_name'
    ST_PENDING_ADDR = 'pending_address'
    ST_PENDING_PAY = 'pending_payment'
    ST_AWAIT_ADMIN = 'awaiting_admin_claim'
    ST_ADMIN_PROC = 'admin_processing'
    ST_SHIPPING = 'shipping'
    ST_ARRIVED = 'arrived_at_pickup'
    ST_REVIEW = 'waiting_review'
    ST_COMPLETED = 'completed'

class Database:
    @staticmethod
    async def init_db():
        if not Resources.db_pool: return
        async with Resources.db_pool.acquire() as conn:
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {DatabaseParams.ORDERS_TABLE} (
                    id SERIAL PRIMARY KEY,
                    order_token VARCHAR(36) UNIQUE NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    cart_data JSONB NOT NULL,
                    total_amount NUMERIC(10, 2) NOT NULL DEFAULT 0.00,
                    user_tg_id VARCHAR(50) DEFAULT NULL,
                    phone_number VARCHAR(20) DEFAULT NULL,
                    full_name VARCHAR(255) DEFAULT NULL,
                    address TEXT DEFAULT NULL,
                    delivery_type VARCHAR(50) DEFAULT NULL,
                    delivery_address_data TEXT DEFAULT NULL,
                    admin_track_number VARCHAR(50) DEFAULT NULL,
                    admin_delivery_date TEXT DEFAULT NULL, 
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_order_token ON {DatabaseParams.ORDERS_TABLE} (order_token);
                CREATE INDEX IF NOT EXISTS idx_user_tg_id ON {DatabaseParams.ORDERS_TABLE} (user_tg_id);
            """)

    @staticmethod
    async def save_draft(token, cart, total):
        try:
            async with Resources.db_pool.acquire() as conn:
                await conn.execute(
                    f"INSERT INTO {DatabaseParams.ORDERS_TABLE} (order_token, status, cart_data, total_amount) VALUES ($1, $2, $3, $4)",
                    token, DatabaseParams.ST_PENDING_AUTH, json.dumps(cart), total
                )
        except Exception as e:
            logger.error(f"Save Draft Error: {e}")

    @staticmethod
    async def get_order(token=None, tg_id=None):
        try:
            async with Resources.db_pool.acquire() as conn:
                if token:
                    row = await conn.fetchrow(f"SELECT * FROM {DatabaseParams.ORDERS_TABLE} WHERE order_token = $1 LIMIT 1", token)
                elif tg_id:
                    row = await conn.fetchrow(
                        f"SELECT * FROM {DatabaseParams.ORDERS_TABLE} WHERE user_tg_id = $1 AND status != $2 ORDER BY created_at DESC LIMIT 1",
                        str(tg_id), DatabaseParams.ST_COMPLETED
                    )
                else:
                    return None
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"Get Order Error: {e}")
            return None

    @staticmethod
    async def update_order(order_token, **kwargs):
        if not kwargs: return False
        
        set_parts = ["updated_at = CURRENT_TIMESTAMP"]
        values = []
        i = 1
        
        for key, value in kwargs.items():
            set_parts.append(f"{key} = ${i}")
            if key == 'cart_data' and isinstance(value, (dict, list)):
                values.append(json.dumps(value))
            else:
                values.append(value)
            i += 1
            
        values.append(order_token)
        query = f"UPDATE {DatabaseParams.ORDERS_TABLE} SET {', '.join(set_parts)} WHERE order_token = ${i}"
        
        try:
            async with Resources.db_pool.acquire() as conn:
                result = await conn.execute(query, *values)
                # result string example: "UPDATE 1"
                if result and "UPDATE" in result:
                    return True
                return False
        except Exception as e:
            logger.error(f"Update Error: {e}")
            return False

# --- 5. TELEGRAM API ---
class TelegramClient:
    @staticmethod
    async def _post(method, payload):
        if not Resources.client_session: return
        try:
            async with Resources.client_session.post(Config.TG_API_BASE + method, json=payload) as resp:
                pass 
        except Exception as e:
            logger.error(f"TG API Error: {e}")

    @staticmethod
    async def send_message(chat_id, text, reply_markup=None):
        payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}
        # ВАЖНО: Отключаем превью ссылок, чтобы не забивать чат
        payload['disable_web_page_preview'] = True
        if reply_markup: payload['reply_markup'] = reply_markup
        await TelegramClient._post('sendMessage', payload)

    @staticmethod
    async def edit_message(chat_id, message_id, text, reply_markup=None):
        payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'Markdown'}
        if reply_markup: payload['reply_markup'] = reply_markup
        await TelegramClient._post('editMessageText', payload)

    @staticmethod
    async def forward_message(from_chat, msg_id, to_chat):
        payload = {'chat_id': to_chat, 'from_chat_id': from_chat, 'message_id': msg_id}
        await TelegramClient._post('forwardMessage', payload)

# --- 6. БИЗНЕС-ЛОГИКА ---
class BotLogic:
    @staticmethod
    def _generate_cart_text(cart_items):
        if isinstance(cart_items, str): cart_items = json.loads(cart_items)
        return "\n".join([f"▫️ {item['name']} (Размер: {item['size']}) x {item['quantity']}" for item in cart_items])

    @staticmethod
    async def notify_admin(order):
        if not Config.TG_ADMIN_GROUP_ID: return
        cart_data = json.loads(order['cart_data']) if isinstance(order['cart_data'], str) else order['cart_data']
        
        # ТОЧНЫЙ ТЕКСТ ДЛЯ АДМИНА
        txt = (
            f"🔥 **НОВЫЙ ЗАКАЗ ОПЛАЧЕН** 🔥\n"
            f"ID: `{order['order_token']}`\n\n"
            f"💰 **Сумма:** {order['total_amount']:.2f} ₽\n\n"
            f"👤 **Покупатель:**\n"
            f"ФИО: {order['full_name']}\n"
            f"Тел: `{order['phone_number']}`\n"
            f"TG: [ID {order['user_tg_id']}](tg://user?id={order['user_tg_id']})\n\n"
            f"🚚 **Доставка:**\n"
            f"Куда: {order['address'] or 'Адрес не указан'} \n"
            f"Тип: СДЭК (по умолчанию)\n\n"
            f"🛒 **Товары:**\n{BotLogic._generate_cart_text(cart_data)}\n\n"
            f"👇 **ЧЕК ОБ ОПЛАТЕ НИЖЕ:**"
        )
        kb = {"inline_keyboard": [[{"text": "🛠 Взять в работу", "callback_data": f"admin_process_{order['order_token']}"}]]}
        await TelegramClient.send_message(Config.TG_ADMIN_GROUP_ID, txt, kb)

    @staticmethod
    async def handle_update(update: dict):
        if 'callback_query' in update:
            await BotLogic._process_callback(update['callback_query'])
        elif 'message' in update:
            await BotLogic._process_message(update['message'])

    @staticmethod
    async def _process_callback(cb):
        chat_id = cb['message']['chat']['id']
        msg_id = cb['message']['message_id']
        data = cb['data']
        
        # --- АДМИН: Взять в работу ---
        if data.startswith('admin_process_'):
            token = data.split('_')[-1]
            order = await Database.get_order(token=token)
            
            if order and order['status'] == DatabaseParams.ST_AWAIT_ADMIN:
                await Database.update_order(token, status=DatabaseParams.ST_ADMIN_PROC)
                
                # Обрезаем исходное сообщение, убираем чек и добавляем статус
                clean_text = cb['message']['text'].split('👇')[0].strip()
                await TelegramClient.edit_message(chat_id, msg_id, clean_text + '\n\n✅ **ВЗЯТ В РАБОТУ**', {})
                
                # Инструкция админу (ТОЧНЫЙ ТЕКСТ)
                instr = (
                    f"✅ **В обработке** `{token}`\n\n"
                    f"🛠 **Доступные команды для работы с заказами:**\n"
                    f"1. **Отправка трека:** `{token} | ТРЕК-НОМЕР | ПВЗ АДРЕС | ДАТА ДОСТАВКИ`\n"
                    f"   _(Пример: `b83a1602884a | 4772747272 | Мира 146 | ~18 декабря`)_\n"
                    f"2. **Отметка о прибытии:** `{token} | ПРИБЫЛ`\n"
                    f"   _(Клиенту будет отправлено уведомление с кнопкой 'Я забрал(а)')_"
                )
                await TelegramClient.send_message(chat_id, instr)
            elif order:
                await TelegramClient.edit_message(chat_id, msg_id, cb['message']['text'] + '\n\n⚠️ **ЗАКАЗ УЖЕ ВЗЯТ В РАБОТУ**', {})
            return

        # --- ЮЗЕР: Получил заказ ---
        if data == 'user_received_order':
            order = await Database.get_order(tg_id=chat_id)
            if order and order['status'] == DatabaseParams.ST_ARRIVED:
                await Database.update_order(order['order_token'], status=DatabaseParams.ST_REVIEW)
                
                # Меняем сообщение с кнопкой на "✅ Получено"
                orig_text = cb['message']['text'].split('Пожалуйста, нажмите кнопку ниже')[0].strip()
                await TelegramClient.edit_message(chat_id, msg_id, orig_text + '\n\n✅ **Получено**', None)
                
                # Текст просьбы отзыва
                review_req = (
                    "🥳 **Ура! Поздравляем с покупкой!**\n\n"
                    "Нам будет очень приятно, если вы оставите отзыв с фото.\n"
                    "Это поможет нам стать лучше, а другим - сделать выбор.\n\n"
                    "📸 Пожалуйста, отправьте текст отзыва (можно приложить фотографию) в этот чат, и бот автоматически перешлет его в канал с отзывами!"
                )
                await TelegramClient.send_message(chat_id, review_req)

    @staticmethod
    async def _process_message(msg):
        chat_id = msg['chat']['id']
        text = msg.get('text', '').strip()
        
        # --- АДМИН ПАНЕЛЬ ---
        if str(chat_id) == str(Config.TG_ADMIN_GROUP_ID):
            if '|' in text:
                parts = [x.strip() for x in text.split('|')]
                token = parts[0]
                order = await Database.get_order(token=token)
                
                if not order:
                    await TelegramClient.send_message(chat_id, f"⚠️ Заказ с токеном `{token}` не найден.")
                    return

                # Отправка трека
                if len(parts) == 4 and order['status'] == DatabaseParams.ST_ADMIN_PROC:
                    track, pvz, date = parts[1], parts[2], parts[3]
                    await Database.update_order(token, admin_track_number=track, delivery_address_data=pvz, admin_delivery_date=date, status=DatabaseParams.ST_SHIPPING)
                    
                    # Сообщение КЛИЕНТУ
                    user_msg = (
                        f"🚀 **Заказ сформирован!**\n\n"
                        f"📦 **Трек-номер:** `{track}`\n"
                        f"🏢 **Пункт выдачи:** {pvz}\n"
                        f"⏳ **Примерная дата получения:** {date}\n\n"
                        f"Мы оповестим вас, когда товар прибудет🤝\n"
                        f"Связь - {Config.ADMIN_SUPPORT_USERNAME}"
                    )
                    await TelegramClient.send_message(int(order['user_tg_id']), user_msg)
                    
                    # Сообщение АДМИНУ
                    await TelegramClient.send_message(chat_id, f"✅ Трек отправлен клиенту (Заказ `{token}`)")
                
                # Прибытие
                elif len(parts) == 2 and parts[1].upper() in ['ARRIVED', 'ПРИБЫЛ'] and order['status'] == DatabaseParams.ST_SHIPPING:
                    await Database.update_order(token, status=DatabaseParams.ST_ARRIVED)
                    
                    # Сообщение КЛИЕНТУ
                    user_msg = (
                        f"🏃 **Ваш заказ прибыл!**\n\n"
                        f"Он ждет вас в пункте выдачи: {order['delivery_address_data']}\n\n"
                        f"Код для получения находится в личном кабинете СДЭК. Для входа используйте номер, который вы использовали для оформления заказа.\n\n"
                        f"Пожалуйста, нажмите кнопку ниже, когда заберете посылку 👇"
                    )
                    kb = {"inline_keyboard": [[{"text": "📦 Я забрал(а) заказ!", "callback_data": "user_received_order"}]]}
                    await TelegramClient.send_message(int(order['user_tg_id']), user_msg, reply_markup=kb)
                    
                    # Сообщение АДМИНУ
                    await TelegramClient.send_message(chat_id, f"✅ Клиент оповещен о прибытии (Заказ `{token}`)")
            return

        # --- ЮЗЕР ---
        
        # 1. СТАРТ / АВТОРИЗАЦИЯ
        if text.startswith('/start'):
            params = text.split()
            if len(params) > 1 and params[1].startswith('auth_'):
                token = params[1].replace('auth_', '')
                
                # Пытаемся привязать заказ
                success = await Database.update_order(token, user_tg_id=str(chat_id), status=DatabaseParams.ST_PENDING_AUTH)
                
                if success:
                    kb = {"keyboard": [[{"text": "📱 Отправить номер", "request_contact": True}]], "one_time_keyboard": True, "resize_keyboard": True}
                    welcome_msg = (
                        "👋 **Добро пожаловать в Oops Merch!**\n\n"
                        "Для оформления заказа нам нужен ваш номер телефона.\n"
                        "Нажмите на кнопку внизу или отправьте номер вручную."
                    )
                    await TelegramClient.send_message(chat_id, welcome_msg, reply_markup=kb)
                else:
                    await TelegramClient.send_message(chat_id, "⚠️ Ссылка устарела или недействительна. Попробуйте оформить корзину на сайте заново.")
                
                # ВАЖНО: Делаем return, чтобы код не пошел дальше проверять "активный заказ" и не выдал ошибку
                return
            else:
                # Просто /start без токена
                await TelegramClient.send_message(chat_id, f"👋 Привет! Если есть вопросы, пиши нам: {Config.ADMIN_SUPPORT_USERNAME}")
                return

        # 2. ПОИСК ЗАКАЗА
        order = await Database.get_order(tg_id=chat_id)
        
        # Если заказа нет
        if not order:
            # Проверка: если юзер пытается ввести номер или контакт, но заказа нет
            if 'contact' in msg or (text and re.match(r'^\+?\d{10,15}$', re.sub(r'[^\d+]', '', text))):
                 await TelegramClient.send_message(chat_id, f"⚠️ Нет активного заказа. Оформите корзину на {Config.SITE_URL}")
                 return
            
            # Иначе дефолт
            await TelegramClient.send_message(chat_id, f"⚠️ Нет активного заказа. Оформите корзину на {Config.SITE_URL}")
            return

        st = order['status']
        token = order['order_token']

        # 3. ТЕЛЕФОН
        if st == DatabaseParams.ST_PENDING_AUTH:
            phone = msg.get('contact', {}).get('phone_number') or text
            phone = re.sub(r'[^\d+]', '', phone)
            
            if len(phone) >= 10:
                # Форматируем номер
                if phone.startswith('8'): phone = '+7' + phone[1:]
                elif not phone.startswith('+'): phone = '+' + phone
                
                await Database.update_order(token, phone_number=phone, status=DatabaseParams.ST_PENDING_NAME)
                await TelegramClient.send_message(chat_id, "✅ **Номер принят.**\n\nПожалуйста, отправьте ваше **ФИО полным сообщением**:", reply_markup={"remove_keyboard": True})
            else:
                await TelegramClient.send_message(chat_id, "⚠️ Неверный формат номера. Пожалуйста, отправьте номер в формате `+7 (XXX) XXX-XX-XX` или нажмите кнопку.")
            return

        # 4. ФИО
        if st == DatabaseParams.ST_PENDING_NAME:
            if len(text.split()) < 2:
                await TelegramClient.send_message(chat_id, "⚠️ Пожалуйста, введите Фамилию и Имя (минимум 2 слова).")
                return
            await Database.update_order(token, full_name=text, status=DatabaseParams.ST_PENDING_ADDR)
            await TelegramClient.send_message(chat_id, "📍 Введите ваш **адрес проживания** (Город, Улица, Дом, Квартира).\n\nМы подберем ближайший СДЭК к этому адресу.")
            return

        # 5. АДРЕС -> ОПЛАТА
        if st == DatabaseParams.ST_PENDING_ADDR:
            if len(text) < 5: 
                await TelegramClient.send_message(chat_id, "⚠️ Адрес слишком короткий.")
                return
            await Database.update_order(token, address=text, delivery_type='СДЭК', status=DatabaseParams.ST_PENDING_PAY)
            
            # Получаем обновленные данные (цену)
            updated_order = await Database.get_order(token=token)
            
            pay_msg = (
                f"💳 **Оплата заказа**\n\n"
                f"К оплате: **{updated_order['total_amount']:.2f} ₽**\n\n"
                f"Перевод на одну из карт:\n"
                f"🟢 Сбер: `{Config.CARDS['sber']}`\n"
                f"🟡 Тинькофф: `{Config.CARDS['tbank']}`\n"
                f"🔴 Альфа: `{Config.CARDS['alfa']}`\n\n"
                f"📎 **ОБЯЗАТЕЛЬНО:** Пришлите **ФАЙЛ (Квитанцию/PDF)** с чеком сюда."
            )
            await TelegramClient.send_message(chat_id, pay_msg)
            return

        # 6. ЧЕК
        if st == DatabaseParams.ST_PENDING_PAY:
            if 'document' in msg or 'photo' in msg:
                await Database.update_order(token, status=DatabaseParams.ST_AWAIT_ADMIN)
                # Уведомление админу
                await BotLogic.notify_admin(order)
                # Форвард чека админу
                await TelegramClient.forward_message(chat_id, msg['message_id'], Config.TG_ADMIN_GROUP_ID)
                
                success_msg = (
                    "✅ **Спасибо, чек принят!**\n\n"
                    "Мы проверяем оплату. Как только заказ пройдет ручную модерацию - бот автоматически пришлет трек-номер и остальную информацию. Спасибо, что выбрали нас🫶"
                )
                await TelegramClient.send_message(chat_id, success_msg)
            else:
                await TelegramClient.send_message(chat_id, "⏳ Ждем файл с квитанцией об оплате.")
            return

        # 7. ОТЗЫВ
        if st == DatabaseParams.ST_REVIEW:
            if Config.TG_REVIEWS_CHANNEL_ID and (text or 'photo' in msg or 'document' in msg):
                await TelegramClient.forward_message(chat_id, msg['message_id'], Config.TG_REVIEWS_CHANNEL_ID)
                await Database.update_order(token, status=DatabaseParams.ST_COMPLETED)
                await TelegramClient.send_message(chat_id, "🖤 **Спасибо за отзыв!**\n\nБудем рады видеть вас снова!")
            else:
                # Если нажали что-то другое
                await Database.update_order(token, status=DatabaseParams.ST_COMPLETED)
                await TelegramClient.send_message(chat_id, "🖤 Спасибо за заказ! Будем рады видеть вас снова!")
            return

# --- 8. KEEP ALIVE (Анти-сон) ---
async def keep_alive_ping():
    """Пингует сам себя каждые 10 минут, чтобы Render не засыпал"""
    url = Config.APP_URL
    if not url:
        logger.warning("No RENDER_EXTERNAL_URL found. Keep-alive disabled.")
        return

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{url}/health") as resp:
                    logger.info(f"Keep-Alive Ping: {resp.status}")
        except Exception as e:
            logger.error(f"Keep-Alive Error: {e}")
        
        # Ждем 10 минут (600 секунд)
        await asyncio.sleep(600)

# --- 9. ЗАПУСК ПРИЛОЖЕНИЯ ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Start DB
    if Config.DATABASE_URL:
        Resources.db_pool = await asyncpg.create_pool(Config.DATABASE_URL, min_size=1, max_size=20)
        await Database.init_db()
    
    # 2. Start Session
    Resources.client_session = aiohttp.ClientSession()
    
    # 3. Start Keep-Alive Task
    asyncio.create_task(keep_alive_ping())
    
    logger.info("🚀 Bot Started & Keep-Alive Active")
    yield
    
    # Shutdown
    if Resources.db_pool: await Resources.db_pool.close()
    if Resources.client_session: await Resources.client_session.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
async def root():
    return "FastAPI Bot Running"

@app.get("/health")
async def health():
    return "OK"

@app.post("/init-auth")
async def init_auth(request: Request):
    try:
        data = await request.json()
        token = uuid.uuid4().hex[:12]
        await Database.save_draft(token, data.get('items', []), data.get('total_amount', 0))
        return {"success": True, "telegram_bot_url": f"https://t.me/{Config.TG_BOT_USERNAME}?start=auth_{token}"}
    except Exception as e:
        logger.error(f"Init Error: {e}")
        return Response(content="Error", status_code=500)

@app.post("/webhook")
async def webhook(request: Request):
    try:
        update = await request.json()
        await BotLogic.handle_update(update)
    except Exception as e:
        logger.error(f"Webhook Error: {e}")
    return "OK"
