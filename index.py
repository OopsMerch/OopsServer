import os
import json
import uuid
import re
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
import asyncpg
import aiohttp

# --- 1. КОНФИГУРАЦИЯ ---
class Config:
    # База данных
    DATABASE_URL = os.environ.get('DATABASE_URL')
    
    # Telegram
    TG_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not TG_BOT_TOKEN:
        # Логируем, но не падаем сразу, чтобы дать серверу запуститься и показать ошибку в логах
        print("CRITICAL: TELEGRAM_BOT_TOKEN is missing!")
        
    TG_API_BASE = f'https://api.telegram.org/bot{TG_BOT_TOKEN}/'
    TG_BOT_USERNAME = os.environ.get('TELEGRAM_BOT_USERNAME', 'oopsmerchbot')
    
    # ID чатов (строки)
    TG_ADMIN_GROUP_ID = os.environ.get('TG_ADMIN_GROUP_ID')
    TG_REVIEWS_CHANNEL_ID = os.environ.get('TG_REVIEWS_CHANNEL_ID')
    
    # Тексты и ссылки
    ADMIN_SUPPORT_USERNAME = os.environ.get('ADMIN_SUPPORT_USERNAME', '@oopssupport')
    # Используем SITE_BASE_URL как SITE_URL
    SITE_URL = os.environ.get('SITE_BASE_URL', 'oops-merch.ru')
    
    # Реквизиты
    CARDS = {
        'sber': os.environ.get('SBERBANK_CARD'),
        'tbank': os.environ.get('TBANK_CARD'),
        'alfa': os.environ.get('ALFABANK_CARD')
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
    # Статусы
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
        if not kwargs: return
        
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
                await conn.execute(query, *values)
        except Exception as e:
            logger.error(f"Update Error: {e}")

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
        return "\n".join([f"▫️ {item['name']} ({item['size']}) x {item['quantity']}" for item in cart_items])

    @staticmethod
    async def notify_admin(order):
        if not Config.TG_ADMIN_GROUP_ID: return
        cart_data = json.loads(order['cart_data']) if isinstance(order['cart_data'], str) else order['cart_data']
        
        txt = (
            f"🔥 **НОВЫЙ ЗАКАЗ ОПЛАЧЕН** 🔥\nID: `{order['order_token']}`\n\n"
            f"💰 **{order['total_amount']:.2f} ₽**\n"
            f"👤 {order['full_name']} (`{order['phone_number']}`)\n"
            f"TG: [ID {order['user_tg_id']}](tg://user?id={order['user_tg_id']})\n"
            f"🚚 Адрес: `{order['address']}`\n\n"
            f"🛒 **Товары:**\n{BotLogic._generate_cart_text(cart_data)}\n\n"
            f"👇 **ЧЕК НИЖЕ:**"
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
        
        if data.startswith('admin_process_'):
            token = data.split('_')[-1]
            order = await Database.get_order(token=token)
            
            if order and order['status'] == DatabaseParams.ST_AWAIT_ADMIN:
                await Database.update_order(token, status=DatabaseParams.ST_ADMIN_PROC)
                clean_text = cb['message']['text'].split('👇')[0].strip()
                await TelegramClient.edit_message(chat_id, msg_id, clean_text + '\n\n✅ **ВЗЯТ В РАБОТУ**', {})
                
                instr = (
                    f"✅ **В работе** `{token}`\n\n"
                    f"1. Трек: `{token} | ТРЕК | АДРЕС ПВЗ | ДАТА`\n"
                    f"2. Прибыл: `{token} | ПРИБЫЛ`"
                )
                await TelegramClient.send_message(chat_id, instr)
            elif order:
                await TelegramClient.edit_message(chat_id, msg_id, cb['message']['text'] + '\n\n⚠️ **УЖЕ ВЗЯТ**', {})
            return

        if data == 'user_received_order':
            order = await Database.get_order(tg_id=chat_id)
            if order and order['status'] == DatabaseParams.ST_ARRIVED:
                await Database.update_order(order['order_token'], status=DatabaseParams.ST_REVIEW)
                await TelegramClient.edit_message(chat_id, msg_id, cb['message']['text'].split('👇')[0] + '\n\n✅ **Получено**', None)
                await TelegramClient.send_message(chat_id, "🥳 **Ура!**\nЖдем ваш отзыв (фото+текст) прямо здесь!")

    @staticmethod
    async def _process_message(msg):
        chat_id = msg['chat']['id']
        text = msg.get('text', '').strip()
        
        # --- АДМИН ---
        if str(chat_id) == str(Config.TG_ADMIN_GROUP_ID):
            if '|' in text:
                parts = [x.strip() for x in text.split('|')]
                token = parts[0]
                order = await Database.get_order(token=token)
                if not order: return

                if len(parts) == 4 and order['status'] == DatabaseParams.ST_ADMIN_PROC:
                    track, pvz, date = parts[1], parts[2], parts[3]
                    await Database.update_order(token, admin_track_number=track, delivery_address_data=pvz, admin_delivery_date=date, status=DatabaseParams.ST_SHIPPING)
                    
                    user_msg = f"🚀 **Заказ отправлен!**\n\n📦 Трек: `{track}`\n🏢 ПВЗ: {pvz}\n⏳ Дата: {date}"
                    await TelegramClient.send_message(int(order['user_tg_id']), user_msg)
                    await TelegramClient.send_message(chat_id, f"✅ Трек отправлен клиенту ({token})")
                
                elif len(parts) == 2 and parts[1].upper() in ['ARRIVED', 'ПРИБЫЛ'] and order['status'] == DatabaseParams.ST_SHIPPING:
                    await Database.update_order(token, status=DatabaseParams.ST_ARRIVED)
                    user_msg = f"🏃 **Заказ прибыл!**\n\nПВЗ: {order['delivery_address_data']}\nКод в приложении СДЭК.\n\n👇 Нажмите, когда заберете!"
                    kb = {"inline_keyboard": [[{"text": "📦 Я забрал(а)!", "callback_data": "user_received_order"}]]}
                    await TelegramClient.send_message(int(order['user_tg_id']), user_msg, reply_markup=kb)
                    await TelegramClient.send_message(chat_id, f"✅ Клиент уведомлен о прибытии ({token})")
            return

        # --- ЮЗЕР ---
        if text.startswith('/start'):
            params = text.split()
            if len(params) > 1 and params[1].startswith('auth_'):
                token = params[1].replace('auth_', '')
                await Database.update_order(token, user_tg_id=str(chat_id), status=DatabaseParams.ST_PENDING_AUTH)
                kb = {"keyboard": [[{"text": "📱 Отправить телефон", "request_contact": True}]], "one_time_keyboard": True, "resize_keyboard": True}
                await TelegramClient.send_message(chat_id, "👋 Привет! Нажмите кнопку внизу, чтобы подтвердить номер.", reply_markup=kb)
            else:
                await TelegramClient.send_message(chat_id, f"👋 Поддержка: {Config.ADMIN_SUPPORT_USERNAME}")
            return

        order = await Database.get_order(tg_id=chat_id)
        if not order:
            if 'contact' in msg or (text and re.match(r'^\+?\d+$', text)):
                 await TelegramClient.send_message(chat_id, f"⚠️ Нет активного заказа. Оформите корзину на {Config.SITE_URL}")
            return

        st = order['status']
        token = order['order_token']

        if st == DatabaseParams.ST_PENDING_AUTH:
            phone = msg.get('contact', {}).get('phone_number') or text
            phone = re.sub(r'[^\d+]', '', phone)
            if len(phone) >= 10:
                await Database.update_order(token, phone_number=phone, status=DatabaseParams.ST_PENDING_NAME)
                await TelegramClient.send_message(chat_id, "✅ Пришлите **ФИО** (одним сообщением).", reply_markup={"remove_keyboard": True})
            return

        if st == DatabaseParams.ST_PENDING_NAME:
            if len(text.split()) < 2:
                await TelegramClient.send_message(chat_id, "⚠️ Нужно минимум два слова (Имя и Фамилия).")
                return
            await Database.update_order(token, full_name=text, status=DatabaseParams.ST_PENDING_ADDR)
            await TelegramClient.send_message(chat_id, "📍 Введите **Адрес** (Город, Улица, Дом).")
            return

        if st == DatabaseParams.ST_PENDING_ADDR:
            if len(text) < 5: return
            await Database.update_order(token, address=text, delivery_type='СДЭК', status=DatabaseParams.ST_PENDING_PAY)
            
            pay_msg = (
                f"💳 **К оплате: {order['total_amount']:.2f} ₽**\n\n"
                f"🟢 Сбер: `{Config.CARDS['sber']}`\n"
                f"🟡 Т-Банк: `{Config.CARDS['tbank']}`\n"
                f"🔴 Альфа: `{Config.CARDS['alfa']}`\n\n"
                f"📎 **ОБЯЗАТЕЛЬНО:** Пришлите ФАЙЛ чека сюда."
            )
            await TelegramClient.send_message(chat_id, pay_msg)
            return

        if st == DatabaseParams.ST_PENDING_PAY:
            if 'document' in msg or 'photo' in msg:
                await Database.update_order(token, status=DatabaseParams.ST_AWAIT_ADMIN)
                await BotLogic.notify_admin(order)
                await TelegramClient.forward_message(chat_id, msg['message_id'], Config.TG_ADMIN_GROUP_ID)
                await TelegramClient.send_message(chat_id, "✅ Чек принят! Ожидайте подтверждения.")
            return

        if st == DatabaseParams.ST_REVIEW:
            if Config.TG_REVIEWS_CHANNEL_ID:
                await TelegramClient.forward_message(chat_id, msg['message_id'], Config.TG_REVIEWS_CHANNEL_ID)
            await Database.update_order(token, status=DatabaseParams.ST_COMPLETED)
            await TelegramClient.send_message(chat_id, "🖤 Спасибо за отзыв! Заказ завершен.")
            return

# --- 7. ЗАПУСК ПРИЛОЖЕНИЯ ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    if Config.DATABASE_URL:
        Resources.db_pool = await asyncpg.create_pool(Config.DATABASE_URL, min_size=1, max_size=20)
        await Database.init_db()
    
    Resources.client_session = aiohttp.ClientSession()
    logger.info("🚀 Bot Started")
    yield
    if Resources.db_pool: await Resources.db_pool.close()
    if Resources.client_session: await Resources.client_session.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
async def root():
    return "FastAPI Bot Running"

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
