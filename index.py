import os
import json
import uuid
import re
import logging
import psycopg2
from psycopg2 import pool
from psycopg2.extras import Json
from contextlib import contextmanager
import requests
from logging.handlers import RotatingFileHandler

# --- 1. КОНФИГУРАЦИЯ (Singleton) ---
class Config:
    # Окружение
    DATABASE_URL = os.environ.get('DATABASE_URL')
    if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://')
        
    TG_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
    TG_API_BASE = f'https://api.telegram.org/bot{TG_BOT_TOKEN}/'
    
    # ID и Каналы
    TG_ADMIN_GROUP_ID = os.environ.get('TG_ADMIN_GROUP_ID', '').strip().replace("'", "").replace('"', "")
    TG_REVIEWS_CHANNEL_ID = os.environ.get('TG_REVIEWS_CHANNEL_ID', '')
    ADMIN_SUPPORT_USERNAME = '@oopssupport'
    SITE_URL = 'oops-merch.ru'
    
    # Реквизиты (Лучше хранить в ENV, но для оптимизации оставим тут как константы класса)
    CARDS = {
        'sber': '2202203614486217',
        'tbank': '2200702039512418',
        'alfa': '2200154572801271'
    }

    # Настройки БД
    DB_MIN_CONN = 1
    DB_MAX_CONN = 20  # Для Render Free/Starter это ок, для High-load можно больше, если БД позволяет

    # CORS
    CORS_HEADERS = [
        ('Access-Control-Allow-Origin', '*'),
        ('Access-Control-Allow-Methods', 'POST, GET, OPTIONS'),
        ('Access-Control-Allow-Headers', 'Content-Type')
    ]

# --- 2. ЛОГИРОВАНИЕ (Ротация + Консоль) ---
def setup_logger():
    if not os.path.exists('logs'):
        os.makedirs('logs', exist_ok=True)

    log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s')
    
    # Файловый хендлер (макс 5мб, храним 3 файла)
    file_handler = RotatingFileHandler('logs/bot_app.log', maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
    file_handler.setFormatter(log_formatter)
    file_handler.setLevel(logging.INFO)

    # Консольный хендлер
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    console_handler.setLevel(logging.INFO)

    logger = logging.getLogger('OopsMerchBot')
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger

logger = setup_logger()

# --- 3. КЛИЕНТ БАЗЫ ДАННЫХ (Optimized) ---
class DatabaseParams:
    ORDERS_TABLE = 'orders'
    # Статусы (enum-like)
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

    ACTIVE_STATUSES = (
        ST_PENDING_AUTH, ST_PENDING_NAME, ST_PENDING_ADDR, 
        ST_PENDING_PAY, ST_AWAIT_ADMIN, ST_ADMIN_PROC, 
        ST_SHIPPING, ST_ARRIVED, ST_REVIEW
    )

class Database:
    _pool = None

    @classmethod
    def initialize(cls):
        if cls._pool is None:
            try:
                cls._pool = psycopg2.pool.SimpleConnectionPool(
                    Config.DB_MIN_CONN, 
                    Config.DB_MAX_CONN, 
                    dsn=Config.DATABASE_URL
                )
                cls._create_table()
                logger.info("Database connection pool initialized.")
            except Exception as e:
                logger.critical(f"DB Connection Failed: {e}")
                raise

    @classmethod
    def _create_table(cls):
        with cls.get_cursor() as cur:
            cur.execute(f"""
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

    @classmethod
    @contextmanager
    def get_cursor(cls):
        """Контекстный менеджер для безопасного получения курсора и возврата соединения"""
        conn = cls._pool.getconn()
        try:
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"DB Transaction Error: {e}")
            raise
        finally:
            cls._pool.putconn(conn)

    @staticmethod
    def save_draft(token, cart, total):
        try:
            with Database.get_cursor() as cur:
                query = f"INSERT INTO {DatabaseParams.ORDERS_TABLE} (order_token, status, cart_data, total_amount) VALUES (%s, %s, %s, %s)"
                cur.execute(query, (token, DatabaseParams.ST_PENDING_AUTH, Json(cart), total))
        except Exception:
            pass # Логгируется внутри get_cursor

    @staticmethod
    def update_order(order_token=None, filter_tg_id=None, **kwargs):
        if not kwargs: return False
        
        updates = ["updated_at = CURRENT_TIMESTAMP"]
        params = []
        
        for key, value in kwargs.items():
            if key == 'cart_data':
                updates.append(f"{key} = %s::jsonb")
                params.append(Json(value))
            else:
                updates.append(f"{key} = %s")
                params.append(value)
        
        where_parts = []
        if order_token:
            where_parts.append("order_token = %s")
            params.append(order_token)
        elif filter_tg_id:
            # Оптимизированный запрос с ANY для массивов
            where_parts.append("user_tg_id = %s")
            params.append(str(filter_tg_id))
            where_parts.append("status = ANY(%s)")
            params.append(DatabaseParams.ACTIVE_STATUSES)
        else:
            return False

        query = f"UPDATE {DatabaseParams.ORDERS_TABLE} SET {', '.join(updates)} WHERE {' AND '.join(where_parts)}"
        
        try:
            with Database.get_cursor() as cur:
                cur.execute(query, tuple(params))
                return cur.rowcount > 0
        except Exception:
            return False

    @staticmethod
    def get_order(token=None, tg_id=None):
        try:
            with Database.get_cursor() as cur:
                if token:
                    query = f"SELECT * FROM {DatabaseParams.ORDERS_TABLE} WHERE order_token = %s LIMIT 1"
                    cur.execute(query, (token,))
                elif tg_id:
                    query = f"SELECT * FROM {DatabaseParams.ORDERS_TABLE} WHERE user_tg_id = %s AND status != %s ORDER BY created_at DESC LIMIT 1"
                    cur.execute(query, (str(tg_id), DatabaseParams.ST_COMPLETED))
                else:
                    return None
                
                row = cur.fetchone()
                if row:
                    columns = [desc[0] for desc in cur.description]
                    return dict(zip(columns, row))
                return None
        except Exception:
            return None

# --- 4. TELEGRAM API КЛИЕНТ (Session-based) ---
class TelegramClient:
    _session = requests.Session()
    _session.mount('https://', requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=100))

    @classmethod
    def _post(cls, method, payload):
        try:
            # Timeout обязателен для production, чтобы не вешать воркеры
            cls._session.post(Config.TG_API_BASE + method, json=payload, timeout=5)
        except Exception as e:
            logger.error(f"TG API Error ({method}): {e}")

    @classmethod
    def send_message(cls, chat_id, text, reply_markup=None):
        payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}
        if reply_markup: payload['reply_markup'] = reply_markup
        cls._post('sendMessage', payload)

    @classmethod
    def edit_message(cls, chat_id, message_id, text, reply_markup=None):
        payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'Markdown'}
        if reply_markup: payload['reply_markup'] = reply_markup
        cls._post('editMessageText', payload)

    @classmethod
    def forward_message(cls, from_chat, msg_id, to_chat):
        payload = {'chat_id': to_chat, 'from_chat_id': from_chat, 'message_id': msg_id}
        cls._post('forwardMessage', payload)

# --- 5. БИЗНЕС-ЛОГИКА ---
class BotLogic:
    @staticmethod
    def generate_cart_text(cart_items):
        if isinstance(cart_items, str): cart_items = json.loads(cart_items)
        return "\n".join([f"▫️ {item['name']} ({item['size']}) x {item['quantity']}" for item in cart_items])

    @staticmethod
    def notify_admin_new_order(order):
        if not Config.TG_ADMIN_GROUP_ID: return
        
        txt = (
            f"🔥 **НОВЫЙ ЗАКАЗ ОПЛАЧЕН** 🔥\n"
            f"ID: `{order['order_token']}`\n\n"
            f"💰 **Сумма:** {order['total_amount']:.2f} ₽\n"
            f"👤 **Покупатель:** {order['full_name']} (`{order['phone_number']}`)\n"
            f"TG: [ID {order['user_tg_id']}](tg://user?id={order['user_tg_id']})\n"
            f"🚚 **Адрес:** `{order['address'] or 'Нет адреса'}`\n\n"
            f"🛒 **Товары:**\n{BotLogic.generate_cart_text(order['cart_data'])}\n\n"
            f"👇 **ЧЕК НИЖЕ:**"
        )
        kb = {"inline_keyboard": [[{"text": "🛠 Взять в работу", "callback_data": f"admin_process_{order['order_token']}"}]]}
        TelegramClient.send_message(Config.TG_ADMIN_GROUP_ID, txt, kb)

    @staticmethod
    def handle_update(update):
        if 'callback_query' in update:
            BotLogic._process_callback(update['callback_query'])
        elif 'message' in update:
            BotLogic._process_message(update['message'])

    @staticmethod
    def _process_callback(cb):
        chat_id = cb['message']['chat']['id']
        msg_id = cb['message']['message_id']
        data = cb['data']
        
        # Админ берет заказ
        if data.startswith('admin_process_'):
            token = data.split('_')[-1]
            order = Database.get_order(token=token)
            if order and order['status'] == DatabaseParams.ST_AWAIT_ADMIN:
                Database.update_order(order_token=token, status=DatabaseParams.ST_ADMIN_PROC)
                
                # Обновляем сообщение с чеком
                clean_text = cb['message']['text'].split('👇')[0].strip()
                TelegramClient.edit_message(chat_id, msg_id, clean_text + '\n\n✅ **ВЗЯТ В РАБОТУ**', {})
                
                # Инструкция админу
                admin_instr = (
                    f"✅ **В работе** `{token}`\n\n"
                    f"1. **Трек:** `{token} | ТРЕК | АДРЕС ПВЗ | ДАТА`\n"
                    f"2. **Прибыл:** `{token} | ПРИБЫЛ`"
                )
                TelegramClient.send_message(chat_id, admin_instr)
            elif order:
                TelegramClient.edit_message(chat_id, msg_id, cb['message']['text'] + '\n\n⚠️ **УЖЕ В РАБОТЕ**', {})
            return

        # Пользователь забрал заказ
        if data == 'user_received_order':
            order = Database.get_order(tg_id=chat_id)
            if order and order['status'] == DatabaseParams.ST_ARRIVED:
                Database.update_order(order_token=order['order_token'], status=DatabaseParams.ST_REVIEW)
                TelegramClient.edit_message(chat_id, msg_id, cb['message']['text'].split('Пожалуйста')[0] + '\n\n✅ **Получено**', None)
                TelegramClient.send_message(chat_id, "🥳 **Поздравляем!**\nБудем рады вашему отзыву (фото+текст) прямо здесь!")

    @staticmethod
    def _process_message(msg):
        chat_id = msg['chat']['id']
        text = msg.get('text', '').strip()
        
        # --- АДМИН ПАНЕЛЬ ---
        if str(chat_id) == Config.TG_ADMIN_GROUP_ID:
            if '|' in text:
                parts = [x.strip() for x in text.split('|')]
                token = parts[0]
                order = Database.get_order(token=token)
                if not order: return

                if len(parts) == 4 and order['status'] == DatabaseParams.ST_ADMIN_PROC:
                    # Отправка трека
                    track, pvz, date = parts[1], parts[2], parts[3]
                    Database.update_order(token, admin_track_number=track, delivery_address_data=pvz, admin_delivery_date=date, status=DatabaseParams.ST_SHIPPING)
                    
                    user_msg = f"🚀 **Заказ отправлен!**\n\n📦 Трек: `{track}`\n🏢 ПВЗ: {pvz}\n⏳ Дата: {date}\n\nМы сообщим, когда он прибудет!"
                    TelegramClient.send_message(int(order['user_tg_id']), user_msg)
                    TelegramClient.send_message(chat_id, f"✅ Трек отправлен ({token})")
                
                elif len(parts) == 2 and parts[1].upper() in ['ARRIVED', 'ПРИБЫЛ'] and order['status'] == DatabaseParams.ST_SHIPPING:
                    # Прибытие
                    Database.update_order(token, status=DatabaseParams.ST_ARRIVED)
                    user_msg = f"🏃 **Заказ прибыл!**\n\nПВЗ: {order['delivery_address_data']}\nКод в приложении СДЭК.\n\n👇 Нажмите кнопку, когда заберете!"
                    kb = {"inline_keyboard": [[{"text": "📦 Я забрал(а)!", "callback_data": "user_received_order"}]]}
                    TelegramClient.send_message(int(order['user_tg_id']), user_msg, reply_markup=kb)
                    TelegramClient.send_message(chat_id, f"✅ Клиент оповещен ({token})")
            return

        # --- ПОЛЬЗОВАТЕЛЬ ---
        
        # 1. Start / Auth
        if text.startswith('/start'):
            params = text.split()
            if len(params) > 1 and params[1].startswith('auth_'):
                token = params[1].replace('auth_', '')
                if Database.update_order(order_token=token, user_tg_id=str(chat_id), status=DatabaseParams.ST_PENDING_AUTH):
                    kb = {"keyboard": [[{"text": "📱 Отправить номер", "request_contact": True}]], "one_time_keyboard": True, "resize_keyboard": True}
                    TelegramClient.send_message(chat_id, "👋 Привет! Для заказа нужен ваш номер.", reply_markup=kb)
                else:
                    TelegramClient.send_message(chat_id, "⚠️ Ссылка устарела.")
            else:
                TelegramClient.send_message(chat_id, f"👋 Поддержка: {Config.ADMIN_SUPPORT_USERNAME}")
            return

        # Ищем заказ
        order = Database.get_order(tg_id=chat_id)
        if not order:
            if 'contact' in msg or (text and text.replace('+', '').isdigit()):
                 TelegramClient.send_message(chat_id, f"⚠️ Нет активного заказа. Оформите на {Config.SITE_URL}")
            return

        st = order['status']
        token = order['order_token']

        # 2. Телефон
        if st == DatabaseParams.ST_PENDING_AUTH:
            phone = msg.get('contact', {}).get('phone_number') or text
            phone = re.sub(r'[^\d+]', '', phone)
            if len(phone) >= 10:
                Database.update_order(token, phone_number=phone, status=DatabaseParams.ST_PENDING_NAME)
                TelegramClient.send_message(chat_id, "✅ Пришлите **ФИО** одним сообщением.", reply_markup={"remove_keyboard": True})
            else:
                TelegramClient.send_message(chat_id, "⚠️ Нужен корректный номер.")
            return

        # 3. ФИО
        if st == DatabaseParams.ST_PENDING_NAME:
            if len(text.split()) < 2: 
                TelegramClient.send_message(chat_id, "⚠️ Введите Фамилию и Имя.")
                return
            Database.update_order(token, full_name=text, status=DatabaseParams.ST_PENDING_ADDR)
            TelegramClient.send_message(chat_id, "📍 Введите **Адрес** (Город, Улица, Дом).")
            return

        # 4. Адрес -> Оплата
        if st == DatabaseParams.ST_PENDING_ADDR:
            if len(text) < 5: return
            Database.update_order(token, address=text, delivery_type='СДЭК', status=DatabaseParams.ST_PENDING_PAY)
            
            pay_msg = (
                f"💳 **К оплате: {order['total_amount']:.2f} ₽**\n\n"
                f"🟢 Сбер: `{Config.CARDS['sber']}`\n"
                f"🟡 Т-Банк: `{Config.CARDS['tbank']}`\n"
                f"🔴 Альфа: `{Config.CARDS['alfa']}`\n\n"
                f"📎 **Пришлите ФАЙЛ/СКРИН чека.**"
            )
            TelegramClient.send_message(chat_id, pay_msg)
            return

        # 5. Чек
        if st == DatabaseParams.ST_PENDING_PAY:
            if 'document' in msg or 'photo' in msg:
                Database.update_order(token, status=DatabaseParams.ST_AWAIT_ADMIN)
                # Уведомляем админа + форвард чека
                BotLogic.notify_admin_new_order(order)
                TelegramClient.forward_message(chat_id, msg['message_id'], Config.TG_ADMIN_GROUP_ID)
                TelegramClient.send_message(chat_id, "✅ Чек получен! Ждем подтверждения админа.")
            return

        # 6. Отзыв
        if st == DatabaseParams.ST_REVIEW:
            if Config.TG_REVIEWS_CHANNEL_ID:
                TelegramClient.forward_message(chat_id, msg['message_id'], Config.TG_REVIEWS_CHANNEL_ID)
            Database.update_order(token, status=DatabaseParams.ST_COMPLETED)
            TelegramClient.send_message(chat_id, "🖤 Спасибо за заказ!")
            return

# --- 6. WSGI APPLICATION (Entry Point) ---
def application(environ, start_response):
    # Инициализация БД при первом запуске воркера
    if not Database._pool:
        try:
            Database.initialize()
        except Exception:
            start_response('500 Internal Server Error', Config.CORS_HEADERS)
            return [b'DB Init Failed']

    method = environ.get('REQUEST_METHOD', 'GET')
    path = environ.get('PATH_INFO', '/')

    # Health Check
    if path == '/' and method == 'GET':
        start_response('200 OK', [('Content-Type', 'text/plain')])
        return [b"Bot Running optimized."]

    # OPTIONS (CORS)
    if method == 'OPTIONS':
        start_response('200 OK', Config.CORS_HEADERS)
        return [b'']

    # Инициализация заказа (с сайта)
    if method == 'POST' and path == '/init-auth':
        try:
            body_size = int(environ.get('CONTENT_LENGTH', 0))
            data = json.loads(environ['wsgi.input'].read(body_size))
            
            token = uuid.uuid4().hex[:12]
            # Быстрая запись в БД
            Database.save_draft(token, data.get('items', []), data.get('total_amount', 0))
            
            bot_link = f"https://t.me/{os.environ.get('TELEGRAM_BOT_USERNAME', 'bot')}?start=auth_{token}"
            resp = json.dumps({'success': True, 'telegram_bot_url': bot_link}).encode('utf-8')
            
            start_response('200 OK', Config.CORS_HEADERS + [('Content-Type', 'application/json')])
            return [resp]
        except Exception as e:
            logger.error(f"/init-auth error: {e}")
            start_response('500 Error', Config.CORS_HEADERS)
            return [b'Server Error']

    # Webhook от Telegram
    if method == 'POST' and path == '/webhook':
        try:
            body_size = int(environ.get('CONTENT_LENGTH', 0))
            update = json.loads(environ['wsgi.input'].read(body_size))
            
            # Вся логика теперь тут
            BotLogic.handle_update(update)
            
            start_response('200 OK', [('Content-Type', 'text/plain')])
            return [b'OK']
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            # Телеграму всегда отвечаем 200, иначе он будет долбить повторами
            start_response('200 OK', [('Content-Type', 'text/plain')])
            return [b'OK']

    start_response('404 Not Found', [('Content-Type', 'text/plain')])
    return [b'Not Found']
