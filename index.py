import os
import json
import uuid
import re
import logging
import psycopg2
from psycopg2 import pool
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_BOT_USERNAME = os.environ.get('TELEGRAM_BOT_USERNAME', 'oopsmerchbot')

raw_admin_id = os.environ.get('TG_ADMIN_GROUP_ID', '')
TG_ADMIN_GROUP_ID = str(raw_admin_id).strip().replace("'", "").replace('"', "")

# Канал для пересылки отзывов
TG_REVIEWS_CHANNEL_ID = os.environ.get('TG_REVIEWS_CHANNEL_ID', '')

# Поддержка
ADMIN_SUPPORT_USERNAME = '@oopssupport'

# Данные для оплаты
SBERBANK_CARD = '2202203614486217'
TBANK_CARD = '2200702039512418'
ALFABANK_CARD = '2200154572801271'

ORDERS_TABLE_NAME = 'orders'
TG_API_BASE = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/'

CORS_HEADERS = [
    ('Access-Control-Allow-Origin', '*'),
    ('Access-Control-Allow-Methods', 'POST, GET, OPTIONS'),
    ('Access-Control-Allow-Headers', 'Content-Type')
]

try:
    pg_pool = psycopg2.pool.SimpleConnectionPool(
        1, 20,
        dsn=DATABASE_URL.replace('postgres://', 'postgresql://')
    )
except Exception as e:
    logger.error(f"Error creating connection pool: {e}")
    pg_pool = None

# Упрощенные статусы: удален выбор типа доставки и подтверждение
STATUS_PENDING_AUTH = 'pending_phone_auth'
STATUS_PENDING_FULL_NAME = 'pending_full_name'
STATUS_PENDING_ADDRESS = 'pending_address'
STATUS_PENDING_PAYMENT = 'pending_payment'
STATUS_AWAITING_ADMIN = 'awaiting_admin_input'
STATUS_SHIPPING = 'shipping'
STATUS_ARRIVED = 'arrived_at_pickup'
STATUS_WAITING_REVIEW = 'waiting_review'
STATUS_COMPLETED = 'completed'

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
            # Учитываем новый, упрощенный флоу
            statuses = [STATUS_PENDING_AUTH, STATUS_PENDING_FULL_NAME, STATUS_PENDING_ADDRESS, STATUS_PENDING_PAYMENT]
            where_clause = f"user_tg_id = %s AND status IN ('{"', '".join(statuses)}')"
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
    conn = get_db_connection()
    try:
        # Ищем незавершенные заказы
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
    
    # Новый шаблон уведомления для админа
    message = f"""
🔥 **НОВЫЙ ЗАКАЗ ОПЛАЧЕН** 🔥
ID: `{order_data['order_token']}`

💰 **Сумма:** {order_data['total_amount']:.2f} ₽

👤 **Покупатель:**
ФИО: {order_data['full_name']}
Тел: `{order_data['phone_number']}`
TG: [ID {order_data['user_tg_id']}](tg://user?id={order_data['user_tg_id']})

🚚 **Доставка:**
Куда: `{order_data['address'] or 'Адрес не указан'}` 
_Тип: СДЭК (по умолчанию)_

🛒 **Товары:**
{items_text}

👇 **ЧЕК ОБ ОПЛАТЕ НИЖЕ:**
"""
    # Подсказка для админа
    admin_hint = (
        f"🛠 **Для отправки трека клиенту, используй шаблон:**\n"
        f"`{order_data['order_token']} | ТРЕК-НОМЕР | ПВЗ АДРЕС | ДАТА ДОСТАВКИ`\n"
        f"(Пример: `b83a1602884a | 4772747272 | Мира 146 | ~18 декабря`)"
    )

    keyboard = {"inline_keyboard": [[{"text": "🛠 Взять в работу", "callback_data": f"admin_process_{order_data['order_token']}"}]]}
    
    send_message(TG_ADMIN_GROUP_ID, message, reply_markup=keyboard)
    send_message(TG_ADMIN_GROUP_ID, admin_hint)

    if receipt_message_id and user_chat_id:
        forward_message(user_chat_id, receipt_message_id, TG_ADMIN_GROUP_ID)

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
    
    if data.startswith('admin_process_'):
        order_token = data.replace('admin_process_', '')
        order = get_order_by_token(order_token)
        
        if order and order['status'] == STATUS_AWAITING_ADMIN:
            admin_text = (
                f"✅ **В обработке** `{order_token}`\n\n"
                f"🛠 **Для отправки трека клиенту, используй шаблон:**\n"
                f"`{order_token} | ТРЕК-НОМЕР | ПВЗ АДРЕС | ДАТА ДОСТАВКИ`\n"
                f"(Пример: `b83a1602884a | 4772747272 | Мира 146 | ~18 декабря`)"
            )
            # Изменяем сообщение, чтобы показать, что заказ взят в работу
            clean_text = re.sub(r'👇.*', '', query['message']['text'])
            edit_message(chat_id, message_id, clean_text + '\n\n✅ **В обработке**', reply_markup={"inline_keyboard": []})
            send_message(chat_id, admin_text)
        return

    order = get_order_by_tg_id(str(chat_id))
    if not order: return

    order_token = order['order_token']

    if data == 'user_received_order' and order['status'] == STATUS_ARRIVED:
        update_order(order_token=order_token, status=STATUS_WAITING_REVIEW)
        
        # Новое сообщение для отзыва
        review_msg = (
            "🥳 **Ура! Поздравляем с покупкой!**\n\n"
            "Нам будет очень приятно, если вы оставите отзыв с фото.\n"
            "Это поможет нам стать лучше, а другим - сделать выбор.\n\n"
            "📸 Пожалуйста, отправьте текст отзыва (можно приложить фотографию) в этот чат, и бот автоматически перешлет его в канал с отзывами!"
        )
        
        # Обновляем оригинальное сообщение, удаляя кнопку
        edit_message(chat_id, message_id, query['message']['text'].split('Пожалуйста, нажмите кнопку ниже')[0].strip() + '\n\n✅ **Получено**', reply_markup=None)
        send_message(chat_id, review_msg)

def process_message(message):
    chat_id = message['chat']['id']
    text = message.get('text', '').strip()
    
    if str(chat_id) == TG_ADMIN_GROUP_ID:
        if '|' in text:
            parts = [x.strip() for x in text.split('|')]
            
            if len(parts) == 4:
                token, track, pvz, date = parts
                order = get_order_by_token(token)
                if order and order['status'] == STATUS_AWAITING_ADMIN:
                    # Доставка всегда СДЭК
                    update_order(order_token=token, admin_track_number=track, delivery_type='СДЭК', delivery_address_data=pvz, admin_delivery_date=date, status=STATUS_SHIPPING)
                    
                    # Новое сообщение об отправке
                    user_msg = (
                        f"🚀 **Заказ сформирован!**\n\n"
                        f"📦 **Трек-номер:** `{track}`\n"
                        f"🏢 **Пункт выдачи:** {pvz}\n"
                        f"⏳ **Примерная дата получения:** {date}\n\n"
                        f"Мы оповестим вас, когда товар прибудет🤝\n"
                        f"Связь - {ADMIN_SUPPORT_USERNAME}"
                    )
                    send_message(int(order['user_tg_id']), user_msg)
                    send_message(chat_id, f"✅ Трек отправлен клиенту (Заказ `{token}`)")

            elif len(parts) == 2 and parts[1].upper() in ['ARRIVED', 'ПРИБЫЛ', 'ДОСТАВЛЕН']:
                token = parts[0]
                order = get_order_by_token(token)
                if order and order['status'] == STATUS_SHIPPING:
                    update_order(order_token=token, status=STATUS_ARRIVED)
                    
                    # Новое сообщение о прибытии
                    user_msg = (
                        f"🏃 **Ваш заказ прибыл!**\n\n"
                        f"Он ждет вас в пункте выдачи: {order['delivery_address_data']}\n\n"
                        f"Код для получения находится в личном кабинете СДЭК. Для входа используйте номер, который вы использовали для оформления заказа.\n\n"
                        f"Пожалуйста, нажмите кнопку ниже, когда заберете посылку 👇"
                    )
                    kb = {"inline_keyboard": [[{"text": "📦 Я забрал(а) заказ!", "callback_data": "user_received_order"}]]}
                    send_message(int(order['user_tg_id']), user_msg, reply_markup=kb)
                    send_message(chat_id, f"✅ Клиент оповещен о прибытии (Заказ `{token}`)")
                else:
                    send_message(chat_id, "⚠️ Заказ не в пути или не найден.")
        return

    if text.startswith('/start'):
        params = text.split()
        if len(params) > 1 and params[1].startswith('auth_'):
            token = params[1].replace('auth_', '')
            if update_order(order_token=token, filter_user_tg_id=None, user_tg_id=str(chat_id), status=STATUS_PENDING_AUTH):
                kb = {"keyboard": [[{"text": "📱 Подтвердить телефон", "request_contact": True}]], "one_time_keyboard": True, "resize_keyboard": True}
                # Новое приветствие
                welcome_msg = "👋 **Добро пожаловать в Oops Merch!**\n\nДля оформления заказа нам нужен ваш номер телефона.\nНажмите на кнопку внизу или отправьте номер вручную."
                send_message(chat_id, welcome_msg, reply_markup=kb)
            else:
                send_message(chat_id, "⚠️ Ссылка устарела. Попробуйте оформить корзину заново.")
        else:
            send_message(chat_id, f"👋 Привет! Если есть вопросы, пиши нам: {ADMIN_SUPPORT_USERNAME}")
        return

    order = get_order_by_tg_id(str(chat_id))
    if not order:
        send_message(chat_id, "🛍 Чтобы сделать заказ, перейдите в наш магазин.")
        return

    status = order['status']
    token = order['order_token']

    if status == STATUS_PENDING_AUTH:
        phone = None
        if 'contact' in message:
            phone = message['contact']['phone_number']
        elif text:
            normalized_text = re.sub(r'[^\d+]', '', text)
            if re.match(r'^\+?\d{10,15}$', normalized_text) and len(normalized_text) >= 10:
                if normalized_text.startswith('8'):
                    normalized_text = '+7' + normalized_text[1:]
                elif not normalized_text.startswith('+'):
                    normalized_text = '+' + normalized_text
                
                phone = normalized_text
            else:
                send_message(chat_id, "⚠️ Неверный формат номера. Пожалуйста, отправьте номер в формате `+7 (XXX) XXX-XX-XX` или нажмите кнопку.", reply_markup={"keyboard": [[{"text": "📱 Подтвердить телефон", "request_contact": True}]], "one_time_keyboard": True, "resize_keyboard": True})
                return
        
        if phone:
            update_order(order_token=token, phone_number=phone, status=STATUS_PENDING_FULL_NAME)
            send_message(chat_id, "✅ Номер принят.\n\nПожалуйста, отправьте ваше **ФИО полным сообщением**:", reply_markup={"remove_keyboard": True})
            return

    if status == STATUS_PENDING_FULL_NAME:
        if len(text.split()) < 2:
            send_message(chat_id, "⚠️ Пожалуйста, введите Фамилию и Имя (минимум 2 слова).")
            return
        update_order(order_token=token, full_name=text, status=STATUS_PENDING_ADDRESS)
        # Новый запрос адреса (всегда СДЭК)
        send_message(chat_id, "📍 Введите ваш **адрес проживания** (Город, Улица, Дом, Квартира).\n\nМы подберем ближайший СДЭК к этому адресу.")
        return

    if status == STATUS_PENDING_ADDRESS:
        if len(text) < 5:
            send_message(chat_id, "⚠️ Адрес слишком короткий или неполный. Пожалуйста, введите полный адрес проживания.")
            return
        
        # Пропускаем выбор доставки, сразу переходим к оплате (доставка - СДЭК по умолчанию)
        update_order(order_token=token, address=text, delivery_type='СДЭК', status=STATUS_PENDING_PAYMENT)
        
        updated_order = get_order_by_tg_id(str(chat_id))
        
        # Новое сообщение об оплате
        payment_msg = (
            f"💳 **Оплата заказа**\n\n"
            f"К оплате: **{updated_order['total_amount']:.2f} ₽**\n\n"
            f"Перевод на одну из карт:\n"
            f"🟢 Сбер: `{SBERBANK_CARD}`\n"
            f"🟡 Тинькофф: `{TBANK_CARD}`\n"
            f"🔴 Альфа: `{ALFABANK_CARD}`\n\n"
            f"📎 **ОБЯЗАТЕЛЬНО:** Пришлите **ФАЙЛ (Квитанцию/PDF)** с чеком сюда."
        )
        send_message(chat_id, payment_msg, reply_markup=None)
        return

    if status == STATUS_PENDING_PAYMENT:
        if 'document' in message or 'photo' in message:
            receipt_message_id = message['message_id']
            update_order(order_token=token, status=STATUS_AWAITING_ADMIN)
            send_admin_order_notification(get_order_by_tg_id(str(chat_id)), receipt_message_id, chat_id)
            
            # Новое сообщение после принятия чека
            success_msg = "✅ **Спасибо, чек принят!**\n\nМы проверяем оплату. Как только заказ пройдет ручную модерацию - бот автоматически пришлет трек-номер и остальную информацию. Спасибо, что выбрали нас🫶"
            send_message(chat_id, success_msg)
        else:
            send_message(chat_id, "⏳ Ждем файл с квитанцией об оплате.")
        return

    if status == STATUS_WAITING_REVIEW:
        if TG_REVIEWS_CHANNEL_ID and (text or 'photo' in message or 'document' in message):
            
            # Пересылаем сообщение пользователя в канал отзывов
            forward_message(chat_id, message['message_id'], TG_REVIEWS_CHANNEL_ID)
            
            # Финальное сообщение
            final_msg = "🖤 **Спасибо за отзыв!**\n\nБудем рады видеть вас снова!"
            update_order(order_token=token, status=STATUS_COMPLETED)
            send_message(chat_id, final_msg)
        else:
            update_order(order_token=token, status=STATUS_COMPLETED)
            send_message(chat_id, "Спасибо! Заказ завершен.")
        return

def application(environ, start_response):
    method = environ.get('REQUEST_METHOD', 'GET')
    path = environ.get('PATH_INFO', '/')
    
    if path == '/' and method in ['GET', 'HEAD']:
        start_response('200 OK', [('Content-type', 'text/plain; charset=utf-8')])
        return ["Oops Merch Bot Server is Running 🚀".encode('utf-8')]

    if method == 'OPTIONS':
        start_response('200 OK', CORS_HEADERS)
        return [b'']

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
            
            start_response('200 OK', CORS_HEADERS + [('Content-
