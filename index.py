import os
import json
import uuid
import re
import logging
from typing import Dict, Any, Optional

import psycopg2
from psycopg2 import pool
import requests

# --- ЛОГИРОВАНИЕ (Чтобы видеть ошибки в консоли) ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- КОНФИГУРАЦИЯ ---
DATABASE_URL = os.environ.get('DATABASE_URL')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_BOT_USERNAME = os.environ.get('TELEGRAM_BOT_USERNAME', 'oopsmerchbot')

# Очистка и подготовка ID админской группы
raw_admin_id = os.environ.get('TG_ADMIN_GROUP_ID', '')
TG_ADMIN_GROUP_ID = str(raw_admin_id).strip().replace("'", "").replace('"', "")

# ID Канала для отзывов (НОВОЕ)
TG_REVIEWS_CHANNEL_ID = os.environ.get('TG_REVIEWS_CHANNEL_ID', '') # Например: -100123456789

# Support Username (авто-фикс @)
raw_support = os.environ.get('ADMIN_SUPPORT_USERNAME', 'oopssupport')
ADMIN_SUPPORT_USERNAME = raw_support if raw_support.startswith('@') else f"@{raw_support}"

# Реквизиты
SBERBANK_CARD = os.environ.get('SBERBANK_CARD', 'XXXX XXXX XXXX XXXX')
TBANK_CARD = os.environ.get('TBANK_CARD', 'YYYY YYYY YYYY YYYY')
ALFABANK_CARD = os.environ.get('ALFABANK_CARD', 'ZZZZ ZZZZ ZZZZ ZZZZ')

ORDERS_TABLE_NAME = 'orders'
TG_API_BASE = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/'

CORS_HEADERS = [
    ('Access-Control-Allow-Origin', '*'),
    ('Access-Control-Allow-Methods', 'POST, GET, OPTIONS'),
    ('Access-Control-Allow-Headers', 'Content-Type')
]

# --- ПУЛ СОЕДИНЕНИЙ С БД (ОПТИМИЗАЦИЯ) ---
try:
    pg_pool = psycopg2.pool.SimpleConnectionPool(
        1, 20, # min 1, max 20 соединений
        dsn=DATABASE_URL.replace('postgres://', 'postgresql://')
    )
    if pg_pool:
        logger.info("Connection pool created successfully")
except Exception as e:
    logger.error(f"Error creating connection pool: {e}")
    pg_pool = None

# --- СТАТУСЫ ---
STATUS_PENDING_AUTH = 'pending_phone_auth'
STATUS_PENDING_FULL_NAME = 'pending_full_name'
STATUS_PENDING_ADDRESS = 'pending_address'
STATUS_PENDING_DELIVERY_TYPE = 'pending_delivery_type'
STATUS_PENDING_CONFIRMATION = 'pending_confirmation'
STATUS_PENDING_PAYMENT = 'pending_payment'
STATUS_AWAITING_ADMIN = 'awaiting_admin_input'
STATUS_SHIPPING = 'shipping'          # Товар в пути
STATUS_ARRIVED = 'arrived_at_pickup'  # Прибыл в ПВЗ
STATUS_WAITING_REVIEW = 'waiting_review' # Ждем отзыв
STATUS_COMPLETED = 'completed'        # Заказ полностью завершен

# --- DATABASE MANAGER ---

def get_db_connection():
    return pg_pool.getconn()

def release_db_connection(conn):
    pg_pool.putconn(conn)

def init_db():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {ORDERS_TABLE_NAME} (
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
            """)
            conn.commit()
            
            # Миграции (безопасное добавление колонок)
            columns_to_check = {
                'total_amount': 'NUMERIC(10, 2) NOT NULL DEFAULT 0.00',
                'delivery_type': 'VARCHAR(50) DEFAULT NULL',
                'delivery_address_data': 'TEXT DEFAULT NULL',
                'admin_track_number': 'VARCHAR(50) DEFAULT NULL',
                'admin_delivery_date': 'TEXT DEFAULT NULL'
            }
            
            for col, col_type in columns_to_check.items():
                cur.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name = '{ORDERS_TABLE_NAME}' AND column_name = '{col}';")
                if not cur.fetchone():
                    logger.info(f"Adding column {col}...")
                    cur.execute(f"ALTER TABLE {ORDERS_TABLE_NAME} ADD COLUMN {col} {col_type};")
            conn.commit()
    except Exception as e:
        logger.error(f"DB Init Error: {e}")
        conn.rollback()
    finally:
        release_db_connection(conn)

def save_order_draft(order_token, cart_data, total_amount):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            query = f"INSERT INTO {ORDERS_TABLE_NAME} (order_token, status, cart_data, total_amount) VALUES (%s, %s, %s, %s);"
            cursor.execute(query, (order_token, STATUS_PENDING_AUTH, json.dumps(cart_data), total_amount))
            conn.commit()
    except Exception as e:
        logger.error(f"Save Draft Error: {e}")
        conn.rollback()
    finally:
        release_db_connection(conn)

def update_order(order_token=None, filter_user_tg_id=None, **kwargs):
    if not kwargs: return False
    conn = get_db_connection()
    try:
        updates = ["updated_at = CURRENT_TIMESTAMP"]
        params = []
        for key, value in kwargs.items():
            if key == 'cart_data': updates.append(f"{key} = %s::jsonb")
            else: updates.append(f"{key} = %s")
            params.append(value)
            
        where_clause = ""
        if order_token:
            where_clause = "order_token = %s"
            params.append(order_token)
        elif filter_user_tg_id:
            # Обновляем только активные черновики
            where_clause = f"user_tg_id = %s AND status IN ('{STATUS_PENDING_AUTH}', '{STATUS_PENDING_FULL_NAME}', '{STATUS_PENDING_ADDRESS}', '{STATUS_PENDING_DELIVERY_TYPE}', '{STATUS_PENDING_CONFIRMATION}')"
            params.append(filter_user_tg_id)
        else:
            return False
            
        query = f"UPDATE {ORDERS_TABLE_NAME} SET {', '.join(updates)} WHERE {where_clause}"
        with conn.cursor() as cursor:
            cursor.execute(query, params)
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"Update Order Error: {e}")
        conn.rollback()
        return False
    finally:
        release_db_connection(conn)

def get_order_by_tg_id(user_tg_id):
    # Ищем активный заказ (не завершенный)
    conn = get_db_connection()
    try:
        query = f"SELECT * FROM {ORDERS_TABLE_NAME} WHERE user_tg_id = %s AND status NOT IN ('{STATUS_COMPLETED}') ORDER BY created_at DESC LIMIT 1;"
        with conn.cursor() as cursor:
            cursor.execute(query, (str(user_tg_id),))
            row = cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
            return None
    finally:
        release_db_connection(conn)
        
def get_order_by_token(order_token):
    conn = get_db_connection()
    try:
        query = f"SELECT * FROM {ORDERS_TABLE_NAME} WHERE order_token = %s LIMIT 1;"
        with conn.cursor() as cursor:
            cursor.execute(query, (order_token,))
            row = cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
            return None
    finally:
        release_db_connection(conn)

# --- TELEGRAM API WRAPPER ---

def send_message(chat_id, text, reply_markup=None):
    url = TG_API_BASE + 'sendMessage'
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup) if isinstance(reply_markup, dict) else reply_markup
    try:
        requests.post(url, json=payload)
    except Exception as e:
        logger.error(f"TG Send Error: {e}")

def edit_message(chat_id, message_id, text, reply_markup=None):
    url = TG_API_BASE + 'editMessageText'
    payload = {
        'chat_id': chat_id, 'message_id': message_id, 
        'text': text, 'parse_mode': 'Markdown'
    }
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup) if isinstance(reply_markup, dict) else reply_markup
    try:
        requests.post(url, json=payload)
    except Exception as e:
        logger.error(f"TG Edit Error: {e}")

def forward_message(from_chat_id, message_id, to_chat_id):
    url = TG_API_BASE + 'forwardMessage'
    payload = {'chat_id': to_chat_id, 'from_chat_id': from_chat_id, 'message_id': message_id}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        logger.error(f"TG Forward Error: {e}")

def copy_message(from_chat_id, message_id, to_chat_id, caption=None):
    """Копирует сообщение (для отзывов, чтобы не было видно отправителя-форварда, если нужно)"""
    url = TG_API_BASE + 'copyMessage'
    payload = {'chat_id': to_chat_id, 'from_chat_id': from_chat_id, 'message_id': message_id}
    if caption:
        payload['caption'] = caption
        payload['parse_mode'] = 'Markdown'
    try:
        requests.post(url, json=payload)
    except Exception as e:
        logger.error(f"TG Copy Error: {e}")

# --- TEXT GENERATORS ---

def generate_cart_text(order_data):
    cart_data_raw = order_data['cart_data']
    cart_items = json.loads(cart_data_raw) if isinstance(cart_data_raw, str) else cart_data_raw
    text = ""
    for item in cart_items:
        text += f"▫️ {item['name']} (Размер: {item['size']}) x {item['quantity']}\n"
    return text

def send_admin_order_notification(order_data, receipt_message_id=None, user_chat_id=None):
    if not TG_ADMIN_GROUP_ID: return

    items_text = generate_cart_text(order_data)
    
    message = f"""
🔥 **НОВЫЙ ЗАКАЗ ОПЛАЧЕН** 🔥
ID: `{order_data['order_token']}`

💰 **Сумма:** {order_data['total_amount']:.2f} ₽

👤 **Покупатель:**
ФИО: {order_data['full_name']}
Тел: `{order_data['phone_number']}`
TG: [{order_data['user_tg_id']}](tg://user?id={order_data['user_tg_id']})

🚚 **Доставка:**
Тип: {order_data['delivery_type']}
Куда: `{order_data['delivery_address_data'] or 'Адрес не распознан'}`

🛒 **Товары:**
{items_text}

👇 **ЧЕК ОБ ОПЛАТЕ НИЖЕ:**
"""
    keyboard = {"inline_keyboard": [[{"text": "🛠 Взять в работу", "callback_data": f"admin_process_{order_data['order_token']}"}]]}
    send_message(TG_ADMIN_GROUP_ID, message, reply_markup=keyboard)
    
    if receipt_message_id and user_chat_id:
        forward_message(user_chat_id, receipt_message_id, TG_ADMIN_GROUP_ID)

# --- BOT LOGIC ---

def handle_telegram_update(update):
    if 'callback_query' in update:
        process_callback(update['callback_query'])
        return
    if 'message' in update:
        process_message(update['message'])
        return

def process_callback(query):
    chat_id = query['message']['chat']['id']
    message_id = query['message']['message_id']
    data = query['data']
    
    # --- ADMIN ACTION ---
    if data.startswith('admin_process_'):
        order_token = data.replace('admin_process_', '')
        order = get_order_by_token(order_token)
        
        if order and order['status'] == STATUS_AWAITING_ADMIN:
            admin_text = (
                f"🛠 **Обработка заказа** `{order_token}`\n\n"
                f"1️⃣ Скопируйте ID заказа.\n"
                f"2️⃣ Отправьте ответным сообщением по шаблону:\n\n"
                f"`{order_token} | ТРЕК-НОМЕР | АДРЕС ПВЗ/ПОЧТЫ | ДАТА ДОСТАВКИ`"
            )
            # Убираем кнопку у сообщения
            edit_message(chat_id, message_id, query['message']['text'] + '\n\n✅ **В обработке**', reply_markup={"inline_keyboard": []})
            send_message(chat_id, admin_text)
        return

    # --- USER ACTIONS ---
    order = get_order_by_tg_id(str(chat_id))
    if not order: return

    order_token = order['order_token']

    # Выбор доставки
    if data in ['delivery_sdek', 'delivery_russian_post'] and order['status'] == STATUS_PENDING_DELIVERY_TYPE:
        d_type = 'СДЭК' if data == 'delivery_sdek' else 'Почта России'
        # Для СДЭК мы используем тот же адрес для поиска ПВЗ, но статус меняем
        update_order(order_token=order_token, delivery_type=d_type, delivery_address_data=order['address'], status=STATUS_PENDING_CONFIRMATION)
        
        confirm_msg = (
            f"✅ **Способ доставки:** {d_type}\n"
            f"📍 Адрес для подбора: _{order['address']}_\n\n"
            f"📋 **Итоговые данные:**\n"
            f"👤 {order['full_name']}\n"
            f"📱 {order['phone_number']}\n\n"
            f"Всё верно?"
        )
        kb = {"inline_keyboard": [
            [{"text": "✅ Всё верно, к оплате", "callback_data": "confirm_final"}],
            [{"text": "🔄 Исправить данные", "callback_data": "reset_data"}]
        ]}
        edit_message(chat_id, message_id, confirm_msg, reply_markup=kb)

    # Подтверждение и Оплата
    elif data == 'confirm_final' and order['status'] == STATUS_PENDING_CONFIRMATION:
        update_order(order_token=order_token, status=STATUS_PENDING_PAYMENT)
        
        payment_msg = (
            f"💳 **Оплата заказа**\n\n"
            f"К оплате: **{order['total_amount']:.2f} ₽**\n\n"
            f"Перевод на карту (Любой банк):\n"
            f"🟢 Сбер: `{SBERBANK_CARD}`\n"
            f"🟡 Тинькофф: `{TBANK_CARD}`\n"
            f"🔴 Альфа: `{ALFABANK_CARD}`\n\n"
            f"📎 **ОБЯЗАТЕЛЬНО:** Пришлите **ФАЙЛ (Квитанцию/PDF)** с чеком сюда."
        )
        edit_message(chat_id, message_id, payment_msg, reply_markup={"inline_keyboard": []})

    # Сброс
    elif data == 'reset_data':
        update_order(order_token=order_token, full_name=None, address=None, delivery_type=None, status=STATUS_PENDING_FULL_NAME)
        edit_message(chat_id, message_id, "🔄 Данные сброшены.", reply_markup=None)
        send_message(chat_id, "Введите ваше **ФИО** (Фамилия Имя Отчество):")

    # Я ПОЛУЧИЛ ЗАКАЗ
    elif data == 'user_received_order' and order['status'] == STATUS_ARRIVED:
        update_order(order_token=order_token, status=STATUS_WAITING_REVIEW)
        
        review_msg = (
            "🥳 **Ура! Поздравляем с обновкой!**\n\n"
            "Нам будет очень приятно, если вы оставите **отзыв с фото**.\n"
            "Это поможет нам стать лучше, а другим — сделать выбор.\n\n"
            "📸 Просто отправьте фото (можно с текстом) в этот чат, и мы опубликуем его в канале отзывов!"
        )
        # Убираем кнопку "Я получил"
        edit_message(chat_id, message_id, query['message']['text'] + '\n\n✅ **Получено**', reply_markup=None)
        send_message(chat_id, review_msg)

def process_message(message):
    chat_id = message['chat']['id']
    text = message.get('text', '').strip()
    
    # --- ADMIN COMMANDS ---
    if str(chat_id) == TG_ADMIN_GROUP_ID:
        if '|' in text:
            parts = [x.strip() for x in text.split('|')]
            
            # Сценарий 1: Отправка трека (TOKEN | TRACK | PVZ | DATE)
            if len(parts) == 4:
                token, track, pvz, date = parts
                order = get_order_by_token(token)
                if order and order['status'] == STATUS_AWAITING_ADMIN:
                    update_order(order_token=token, admin_track_number=track, delivery_address_data=pvz, admin_delivery_date=date, status=STATUS_SHIPPING)
                    
                    user_msg = (
                        f"🚀 **Заказ отправлен!**\n\n"
                        f"📦 **Трек-номер:** `{track}`\n"
                        f"🏢 **Пункт выдачи:** {pvz}\n"
                        f"⏳ **Ожидайте:** {date}\n\n"
                        f"Мы оповестим вас, когда товар прибудет! 😎"
                    )
                    send_message(int(order['user_tg_id']), user_msg)
                    send_message(chat_id, f"✅ Трек отправлен клиенту (Заказ `{token}`)")
                else:
                    send_message(chat_id, "⚠️ Ошибка статуса или заказа.")
            
            # Сценарий 2: Товар прибыл (TOKEN | ARRIVED)
            elif len(parts) == 2 and parts[1].upper() in ['ARRIVED', 'ПРИБЫЛ', 'ДОСТАВЛЕН']:
                token = parts[0]
                order = get_order_by_token(token)
                if order and order['status'] == STATUS_SHIPPING:
                    update_order(order_token=token, status=STATUS_ARRIVED)
                    
                    user_msg = (
                        f"🏃 **Ваш заказ прибыл!**\n\n"
                        f"Он ждет вас в пункте выдачи: {order['delivery_address_data']}\n"
                        f"Трек: `{order['admin_track_number']}`\n\n"
                        f"Пожалуйста, нажмите кнопку ниже, когда заберете посылку 👇"
                    )
                    kb = {"inline_keyboard": [[{"text": "📦 Я забрал(а) заказ!", "callback_data": "user_received_order"}]]}
                    send_message(int(order['user_tg_id']), user_msg, reply_markup=kb)
                    send_message(chat_id, f"✅ Клиент оповещен о прибытии (Заказ `{token}`)")
                else:
                    send_message(chat_id, "⚠️ Заказ не в пути или не найден.")
        return

    # --- START COMMAND ---
    if text.startswith('/start'):
        params = text.split()
        if len(params) > 1 and params[1].startswith('auth_'):
            token = params[1].replace('auth_', '')
            if update_order(order_token=token, filter_user_tg_id=None, user_tg_id=str(chat_id), status=STATUS_PENDING_AUTH):
                kb = {"keyboard": [[{"text": "📱 Подтвердить телефон", "request_contact": True}]], "one_time_keyboard": True, "resize_keyboard": True}
                send_message(chat_id, "👋 **Добро пожаловать в Oops Merch!**\n\nДля оформления заказа нам нужно подтвердить ваш номер телефона. Нажмите кнопку внизу:", reply_markup=kb)
            else:
                send_message(chat_id, "⚠️ Ссылка устарела. Попробуйте оформить корзину заново.")
        else:
            send_message(chat_id, f"👋 Привет! Если есть вопросы, пиши нам: {ADMIN_SUPPORT_USERNAME}")
        return

    # --- USER FLOW ---
    order = get_order_by_tg_id(str(chat_id))
    if not order:
        # Если просто пишут боту без активного заказа
        send_message(chat_id, "🛍 Чтобы сделать заказ, перейдите в наш магазин.")
        return

    status = order['status']
    token = order['order_token']

    # 1. Телефон (Contact)
    if status == STATUS_PENDING_AUTH and 'contact' in message:
        phone = message['contact']['phone_number']
        update_order(order_token=token, phone_number=phone, status=STATUS_PENDING_FULL_NAME)
        send_message(chat_id, "✅ Отлично!\n\nТеперь напишите ваше **ФИО** (полностью):", reply_markup={"remove_keyboard": True})
        return

    # 2. ФИО
    if status == STATUS_PENDING_FULL_NAME:
        if len(text.split()) < 2:
            send_message(chat_id, "⚠️ Пожалуйста, введите Фамилию и Имя.")
            return
        update_order(order_token=token, full_name=text, status=STATUS_PENDING_ADDRESS)
        send_message(chat_id, "📍 **Введите адрес доставки** (Город, Улица, Дом).\n\n_Мы подберем ближайший пункт выдачи к этому адресу._")
        return

    # 3. Адрес
    if status == STATUS_PENDING_ADDRESS:
        if len(text) < 5:
            send_message(chat_id, "⚠️ Адрес слишком короткий.")
            return
        update_order(order_token=token, address=text, status=STATUS_PENDING_DELIVERY_TYPE)
        kb = {"inline_keyboard": [
            [{"text": "🟢 СДЭК (Быстро)", "callback_data": "delivery_sdek"}],
            [{"text": "🔵 Почта России", "callback_data": "delivery_russian_post"}]
        ]}
        send_message(chat_id, "🚚 Выберите удобный способ доставки:", reply_markup=kb)
        return

    # 4. Оплата (Файл)
    if status == STATUS_PENDING_PAYMENT:
        if 'document' in message:
            update_order(order_token=token, status=STATUS_AWAITING_ADMIN)
            send_admin_order_notification(get_order_by_tg_id(str(chat_id)), message['message_id'], chat_id)
            send_message(chat_id, "✅ **Чек принят!**\n\nМы проверяем оплату. Как только отправим заказ — пришлем трек-номер сюда. Спасибо, что выбрали нас! 🖤")
        elif 'photo' in message:
            send_message(chat_id, "⚠️ Пожалуйста, отправьте чек именно **ФАЙЛОМ** (скрепка -> файл), чтобы качество не терялось.")
        else:
            send_message(chat_id, "⏳ Ждем файл с квитанцией об оплате.")
        return

    # 5. Отзыв
    if status == STATUS_WAITING_REVIEW:
        if TG_REVIEWS_CHANNEL_ID:
            caption_text = f"⭐️ **Отзыв от клиента** {order['full_name']}\n\n{text}"
            # Пересылаем в канал отзывов
            if 'photo' in message:
                # Если фото + текст
                file_id = message['photo'][-1]['file_id']
                url = TG_API_BASE + 'sendPhoto'
                requests.post(url, json={'chat_id': TG_REVIEWS_CHANNEL_ID, 'photo': file_id, 'caption': caption_text, 'parse_mode': 'Markdown'})
            elif text:
                # Просто текст
                send_message(TG_REVIEWS_CHANNEL_ID, caption_text)
            
            # Закрываем заказ
            update_order(order_token=token, status=STATUS_COMPLETED)
            send_message(chat_id, "🖤 **Спасибо за отзыв!**\n\nБудем рады видеть вас снова!")
        else:
            # Если канала нет, просто завершаем
            update_order(order_token=token, status=STATUS_COMPLETED)
            send_message(chat_id, "Спасибо! Заказ завершен.")
        return

# --- WSGI APPLICATION ---
def application(environ, start_response):
    method = environ.get('REQUEST_METHOD', 'GET')
    path = environ.get('PATH_INFO', '/')
    
    if path == '/' and method in ['GET', 'HEAD']:
        start_response('200 OK', [('Content-type', 'text/plain')])
        return [b"Oops Merch Bot Server is Running 🚀"]

    if method == 'OPTIONS':
        start_response('200 OK', CORS_HEADERS)
        return [b'']

    # Инициализация БД (Создание таблиц при первом запуске)
    if not hasattr(application, 'db_initialized'):
        init_db()
        application.db_initialized = True

    if method == 'POST' and path == '/init-auth':
        try:
            size = int(environ.get('CONTENT_LENGTH', 0))
            body = environ['wsgi.input'].read(size)
            data = json.loads(body)
            
            items = data.get('items', [])
            total = data.get('total_amount', 0)
            
            if not items:
                start_response('400 Bad Request', CORS_HEADERS)
                return [b'{"error": "No items"}']
                
            token = str(uuid.uuid4()).replace('-', '')[:12]
            save_order_draft(token, items, total)
            
            link = f"https://t.me/{TELEGRAM_BOT_USERNAME}?start=auth_{token}"
            resp = json.dumps({'success': True, 'telegram_bot_url': link}).encode('utf-8')
            
            start_response('200 OK', CORS_HEADERS + [('Content-Type', 'application/json')])
            return [resp]
        except Exception as e:
            logger.error(f"/init-auth error: {e}")
            start_response('500 Error', CORS_HEADERS)
            return [b'Error']

    if method == 'POST' and path == '/webhook':
        try:
            size = int(environ.get('CONTENT_LENGTH', 0))
            body = environ['wsgi.input'].read(size)
            update = json.loads(body)
            handle_telegram_update(update)
            start_response('200 OK', [('Content-Type', 'text/plain')])
            return [b'OK']
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            start_response('200 OK', [('Content-Type', 'text/plain')])
            return [b'OK']

    start_response('404 Not Found', [('Content-Type', 'text/plain')])
    return [b'Not Found']
