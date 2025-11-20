import os
import json
import uuid
import psycopg2 
import psycopg2.errors
import requests 
from typing import Dict, Any
import re # Добавлен для очистки токена

# --- КОНФИГУРАЦИЯ ---
DATABASE_URL = os.environ.get('DATABASE_URL')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_BOT_USERNAME = os.environ.get('TELEGRAM_BOT_USERNAME', 'oopsmerchbot') 

# Очистка ID группы от мусора (обязательно должно быть в формате -100...)
raw_admin_id = os.environ.get('TG_ADMIN_GROUP_ID', '')
TG_ADMIN_GROUP_ID = str(raw_admin_id).strip().replace("'", "").replace('"', "")

ADMIN_SUPPORT_USERNAME = os.environ.get('ADMIN_SUPPORT_USERNAME', '@oopssupport')

# ПЕРЕМЕННЫЕ ДЛЯ ОПЛАТЫ (ваши реальные данные)
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

# --- МАШИНА СОСТОЯНИЙ ---
STATUS_PENDING_AUTH = 'pending_phone_auth'
STATUS_PENDING_FULL_NAME = 'pending_full_name'
STATUS_PENDING_ADDRESS = 'pending_address'
STATUS_PENDING_DELIVERY_TYPE = 'pending_delivery_type'
STATUS_PENDING_CONFIRMATION = 'pending_confirmation'
STATUS_PENDING_PAYMENT = 'pending_payment'
STATUS_AWAITING_ADMIN = 'awaiting_admin_input'
STATUS_COMPLETED = 'completed'


# --- DATABASE ---

def create_psql_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is not set.")
    conn_url = DATABASE_URL.replace('postgres://', 'postgresql://')
    conn = psycopg2.connect(conn_url)
    conn.autocommit = True
    return conn

def init_db(conn):
    try:
        with conn.cursor() as cur:
            # Создаем основную таблицу
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
            
            # --- РОБУСТНОЕ ИСПРАВЛЕНИЕ ОШИБКИ ДАТЫ (АВТОМАТИЧЕСКАЯ МИГРАЦИЯ) ---
            try:
                # 1. Проверяем текущий тип колонки
                cur.execute(f"""
                    SELECT data_type 
                    FROM information_schema.columns 
                    WHERE table_name = '{ORDERS_TABLE_NAME}' AND column_name = 'admin_delivery_date';
                """)
                result = cur.fetchone()
                
                # 2. Если тип не 'text' или 'character varying' (т.е. 'timestamp with time zone'), то меняем его
                if result and result[0] not in ('text', 'character varying'):
                    print(f"MIGRATION: Column admin_delivery_date is currently {result[0]}. Altering to TEXT.")
                    # Используем USING для принудительной конвертации (превращаем старые данные в текст)
                    cur.execute(f"""
                        ALTER TABLE {ORDERS_TABLE_NAME} 
                        ALTER COLUMN admin_delivery_date TYPE TEXT USING admin_delivery_date::TEXT;
                    """)
                    print("MIGRATION: admin_delivery_date successfully altered to TEXT.")
            except psycopg2.errors.UndefinedTable:
                # Таблица orders еще не создана, пропускаем
                pass
            except Exception as e:
                print(f"MIGRATION ERROR (admin_delivery_date type fix): {e}")

            # Добавление недостающих колонок, если они не существуют
            def add_column_if_not_exists(col_name, col_type):
                try:
                    cur.execute(f"ALTER TABLE {ORDERS_TABLE_NAME} ADD COLUMN {col_name} {col_type};")
                except psycopg2.errors.DuplicateColumn:
                    pass 

            add_column_if_not_exists('total_amount', 'NUMERIC(10, 2) NOT NULL DEFAULT 0.00')
            add_column_if_not_exists('delivery_type', 'VARCHAR(50) DEFAULT NULL')
            add_column_if_not_exists('delivery_address_data', 'TEXT DEFAULT NULL')
            add_column_if_not_exists('admin_track_number', 'VARCHAR(50) DEFAULT NULL')
            add_column_if_not_exists('admin_delivery_date', 'TEXT DEFAULT NULL') 

    except Exception as e:
        print(f"Database initialization error: {e}")

def save_order_draft(conn, order_token, cart_data, total_amount):
    with conn.cursor() as cursor:
        query = f"""
        INSERT INTO {ORDERS_TABLE_NAME} (order_token, status, cart_data, total_amount)
        VALUES (%s, %s, %s, %s);
        """
        cursor.execute(query, (order_token, STATUS_PENDING_AUTH, json.dumps(cart_data), total_amount))

def update_order(conn, order_token=None, filter_user_tg_id=None, **kwargs):
    if not kwargs: return False
    updates = ["updated_at = CURRENT_TIMESTAMP"]
    params = []
    for key, value in kwargs.items():
        if key == 'cart_data':
             updates.append(f"{key} = %s::jsonb")
        else:
             updates.append(f"{key} = %s")
        params.append(value)
        
    where_clause = ""
    if order_token:
        where_clause = "order_token = %s"
        params.append(order_token)
    elif filter_user_tg_id:
        # Обновление только для активных заказов
        where_clause = f"user_tg_id = %s AND status IN ('{STATUS_PENDING_AUTH}', '{STATUS_PENDING_FULL_NAME}', '{STATUS_PENDING_ADDRESS}', '{STATUS_PENDING_DELIVERY_TYPE}', '{STATUS_PENDING_CONFIRMATION}')"
        params.append(filter_user_tg_id)
    else:
        return False
        
    query = f"UPDATE {ORDERS_TABLE_NAME} SET {', '.join(updates)} WHERE {where_clause}"
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.rowcount > 0

def get_order_by_tg_id(conn, user_tg_id):
    query = f"""
    SELECT 
        order_token, status, total_amount, cart_data, phone_number, full_name, address, delivery_type, delivery_address_data, user_tg_id
    FROM 
        {ORDERS_TABLE_NAME}
    WHERE 
        user_tg_id = %s 
        AND status IN ('{STATUS_PENDING_AUTH}', '{STATUS_PENDING_FULL_NAME}', '{STATUS_PENDING_ADDRESS}', '{STATUS_PENDING_DELIVERY_TYPE}', '{STATUS_PENDING_CONFIRMATION}', '{STATUS_PENDING_PAYMENT}', '{STATUS_AWAITING_ADMIN}')
    ORDER BY 
        created_at DESC
    LIMIT 1;
    """
    with conn.cursor() as cursor:
        cursor.execute(query, (user_tg_id,))
        row = cursor.fetchone()
        if row:
            columns = [desc[0] for desc in cursor.description]
            result = dict(zip(columns, row))
            return result
        return None
        
def get_order_by_token(conn, order_token):
    query = f"""
    SELECT 
        id, order_token, status, total_amount, cart_data, user_tg_id, phone_number, full_name, address, delivery_type, delivery_address_data
    FROM 
        {ORDERS_TABLE_NAME}
    WHERE 
        order_token = %s 
    LIMIT 1;
    """
    with conn.cursor() as cursor:
        cursor.execute(query, (order_token,))
        row = cursor.fetchone()
        if row:
            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, row))
        return None


# --- TELEGRAM UTILS ---

def send_message(chat_id, text, reply_markup=None):
    url = TG_API_BASE + 'sendMessage'
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}
    if reply_markup:
        if isinstance(reply_markup, dict):
            payload['reply_markup'] = json.dumps(reply_markup)
        else:
            payload['reply_markup'] = reply_markup
            
    try:
        response = requests.post(url, json=payload)
        print(f"DEBUG: Telegram sending to {chat_id}. Status: {response.status_code}. Response: {response.text}")
    except Exception as e:
        print(f"Error sending message to {chat_id}: {e}")

def generate_admin_order_message(order_data):
    cart_data_raw = order_data['cart_data']
    if isinstance(cart_data_raw, str):
        cart_items = json.loads(cart_data_raw)
    else:
        cart_items = cart_data_raw

    items_list = "\n".join([f"- {item['quantity']} шт. | {item['name']} (Размер: {item['size']}, {item.get('price', 'N/A')} ₽/шт.)" for item in cart_items])
    
    inline_keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Принять в обработку", "callback_data": f"admin_process_{order_data['order_token']}"}
            ]
        ]
    }

    message = f"""
🚨 **НОВЫЙ ЗАКАЗ** 🚨
Токен: `{order_data['order_token']}`
Сумма: **{order_data['total_amount']:.2f} ₽**

---
**Пользовательские данные:**
👤 ФИО: {order_data['full_name']}
📱 Телефон: {order_data['phone_number']}
📧 TG ID: `{order_data['user_tg_id']}`

---
**Доставка:**
🚚 Тип: **{order_data['delivery_type']}**
📍 Адрес/Пункт: {order_data['delivery_address_data'] or 'Не указан'}

---
**Содержимое заказа:**
{items_list}
"""
    return message, inline_keyboard


def send_admin_order_notification(order_data):
    global TG_ADMIN_GROUP_ID 
    
    if not TG_ADMIN_GROUP_ID or TG_ADMIN_GROUP_ID == 'YOUR_ADMIN_GROUP_ID':
         print("Warning: TG_ADMIN_GROUP_ID is not set.")
         return

    message, reply_markup = generate_admin_order_message(order_data)
    send_message(TG_ADMIN_GROUP_ID, message, reply_markup=reply_markup)
    
def send_payment_details(chat_id, order_data):
    global SBERBANK_CARD, TBANK_CARD, ALFABANK_CARD
    total = order_data['total_amount']
    message = f"""
🎉 **Прекрасно! Данные собраны!** 🎉

Осталось только **оплатить заказ** на сумму:
💰 **{total:.2f} ₽**

Выберите удобный способ оплаты:

1.  **Сбербанк** (Для РФ):
    `{SBERBANK_CARD}`

2.  **Тинькофф** (Для РФ):
    `{TBANK_CARD}`
    
3.  **Альфа-Банк** (Для РФ):
    `{ALFABANK_CARD}`
    
---
**❗ После оплаты:**
Пришлите скриншот или **чек-квитанцию файлом** в этот чат для подтверждения оплаты.
Как только оплата будет подтверждена, мы передадим заказ в обработку.

Спасибо за покупку!
"""
    remove_keyboard = {"remove_keyboard": True}
    send_message(chat_id, message, reply_markup=remove_keyboard)
    
# --- TELEGRAM BOT LOGIC ---

def handle_telegram_update(conn, update):
    
    if 'callback_query' in update:
        query = update['callback_query']
        chat_id = query['message']['chat']['id']
        message_id = query['message']['message_id']
        data = query['data']

        if data.startswith('admin_process_'):
            order_token = data.replace('admin_process_', '')
            order_data = get_order_by_token(conn, order_token)
            
            if order_data and order_data['status'] == STATUS_AWAITING_ADMIN: 
                 # ОБНОВЛЕННЫЙ ТЕКСТ ПОДСКАЗКИ ДЛЯ АДМИНА
                 response_text = f"✅ Заказ **{order_token}** принят в обработку. \n\n**Введите данные в формате:**\n`ТОКЕН | ТРЕК_НОМЕР | АДРЕС_ПВЗ | ПРИМЕРНАЯ_ДАТА_ПОЛУЧЕНИЯ`"
                 edit_message_url = TG_API_BASE + 'editMessageText'
                 requests.post(edit_message_url, json={
                    'chat_id': chat_id,
                    'message_id': message_id,
                    'text': query['message']['text'] + '\n\n**Статус:** 🔄 Ожидает ввода данных от администратора',
                    'parse_mode': 'Markdown',
                    'reply_markup': json.dumps({"inline_keyboard": []})
                 })
                 send_message(chat_id, response_text, reply_markup=None)
            return
        
        # User Logic
        order = get_order_by_tg_id(conn, str(chat_id))
        if order and order['status'] in [STATUS_PENDING_DELIVERY_TYPE, STATUS_PENDING_CONFIRMATION]:
            order_token = order['order_token']
            delivery_type = None
            delivery_info = ""
            
            if data == 'delivery_sdek' and order['status'] == STATUS_PENDING_DELIVERY_TYPE:
                delivery_type = 'СДЭК'
                delivery_info = f"Для **СДЭК** будет выбран ближайший пункт выдачи (ПВЗ) к указанному вами адресу: *{order['address']}*."
            elif data == 'delivery_russian_post' and order['status'] == STATUS_PENDING_DELIVERY_TYPE:
                delivery_type = 'Почта России'
                delivery_info = f"Для **Почты России** будет использован полный адрес для доставки до почтового отделения: *{order['address']}*."
            
            if delivery_type:
                update_order(conn, order_token=order_token, delivery_type=delivery_type, delivery_address_data=order['address'], status=STATUS_PENDING_CONFIRMATION)
                confirmation_message = f"""
✅ Способ получения: **{delivery_type}** выбран! 
{delivery_info}

---
**Проверьте ваши данные:**
👤 **ФИО:** {order['full_name']}
📱 **Телефон:** {order['phone_number']}
📍 **Адрес:** {order['address']}
🚚 **Способ:** {delivery_type}

---
**Подтвердите оформление заказа?**
"""
                keyboard = {"inline_keyboard": [[{"text": "✅ Подтвердить", "callback_data": "confirm_order"}], [{"text": "❌ Заполнить заново", "callback_data": "start_over"}]]}
                edit_message_url = TG_API_BASE + 'editMessageText'
                requests.post(edit_message_url, json={'chat_id': chat_id, 'message_id': query['message']['message_id'], 'text': confirmation_message, 'parse_mode': 'Markdown', 'reply_markup': json.dumps(keyboard)})
                return
            
            elif data == 'confirm_order' and order['status'] == STATUS_PENDING_CONFIRMATION:
                update_order(conn, order_token=order_token, status=STATUS_PENDING_PAYMENT)
                send_payment_details(chat_id, order)
                edit_message_url = TG_API_BASE + 'editMessageText'
                requests.post(edit_message_url, json={'chat_id': chat_id, 'message_id': query['message']['message_id'], 'text': query['message']['text'] + '\n\n**Статус:** ✅ **Подтверждено.** Ожидаем оплаты.', 'parse_mode': 'Markdown', 'reply_markup': json.dumps({"inline_keyboard": []})})
                return
            
            elif data == 'start_over':
                update_order(conn, order_token=order_token, full_name=None, address=None, delivery_type=None, status=STATUS_PENDING_FULL_NAME)
                send_message(chat_id, "🔄 **Начинаем заново.** Введите ваше **ФИО** (Полностью):")
                edit_message_url = TG_API_BASE + 'editMessageText'
                requests.post(edit_message_url, json={'chat_id': chat_id, 'message_id': query['message']['message_id'], 'text': query['message']['text'] + '\n\n**Статус:** ❌ **Сброшено.**', 'parse_mode': 'Markdown', 'reply_markup': json.dumps({"inline_keyboard": []})})
                return
        return
        
    if 'message' not in update: return
    
    message = update['message']
    chat_id = message['chat']['id']
    text = message.get('text', '')
    
    # Admin Logic Check (ОБНОВЛЕННАЯ ЛОГИКА)
    global TG_ADMIN_GROUP_ID, ADMIN_SUPPORT_USERNAME
    
    if str(chat_id) == TG_ADMIN_GROUP_ID:
        print("DEBUG: Admin group detected. Parsing text...")
        
        # 1. Очистка текста: удаляем возможные префиксы (имя пользователя, цитаты) и берем только первую строку
        clean_text = text.split('\n')[0].strip()
        
        # 2. Разбиваем на 4 части по разделителю '|'. limit=3 гарантирует, что все, что идет после 3-го '|', попадет в последнюю часть (дата)
        parts = [x.strip() for x in clean_text.split('|', 3)]
        
        if len(parts) != 4:
             print(f"DEBUG: Admin text format incorrect. Parts found: {len(parts)}. Text: {clean_text}")
             send_message(chat_id, "⚠️ **Неверный формат ввода.** Пожалуйста, используйте: \n`ТОКЕН | ТРЕК_НОМЕР | АДРЕС_ПВЗ | ПРИМЕРНАЯ_ДАТА_ПОЛУЧЕНИЯ`")
             return 
             
        try:
            order_token, track_number, pvz_address, delivery_date_str = parts
            
            # --- УСИЛЕННАЯ ОЧИСТКА ТОКЕНА (для удаления префикса 'OOPS SUPPORT [24/7]: ') ---
            if len(order_token) > 12:
                # Ищем 12-значную последовательность букв и цифр (стандартный токен)
                match = re.search(r'([0-9a-fA-F]{12})', order_token)
                if match:
                    order_token = match.group(1)
                else:
                    # Резервный вариант: если токен не найден, берем последнюю часть
                    order_token = order_token.split(':')[-1].strip().split(' ')[-1].strip()

            # Ищем заказ по токену
            order_to_update = get_order_by_token(conn, order_token) 

            if order_to_update and order_to_update['status'] == STATUS_AWAITING_ADMIN:
                # Обновляем заказ и завершаем
                # admin_delivery_date теперь TEXT, поэтому принимает любой ввод
                update_order(conn, order_token=order_token, admin_track_number=track_number, delivery_address_data=pvz_address, admin_delivery_date=delivery_date_str, status=STATUS_COMPLETED)
                
                client_message = f"""
✅ **Ваш заказ оформлен!** (Токен: `{order_token}`)

Вот **трек-номер**: `{track_number}`

Пункт выдачи: 
*{pvz_address}*

🕰️ Примерная дата получения:
**{delivery_date_str}**

---
🔗 По всем вопросам к администратору: {ADMIN_SUPPORT_USERNAME}
"""
                send_message(int(order_to_update['user_tg_id']), client_message)
                send_message(chat_id, f"✅ Сообщение о доставке отправлено пользователю **{order_to_update['full_name']}** (Токен: {order_token})")
                return
            elif order_to_update and order_to_update['status'] != STATUS_AWAITING_ADMIN:
                send_message(chat_id, f"⚠️ Заказ с токеном `{order_token}` найден, но он находится в статусе: **{order_to_update['status']}** (ожидается не 'ввод админа').")
                return
            else:
                send_message(chat_id, f"⚠️ Не найден активный заказ с токеном: `{order_token}`.")
                return

        except Exception as e:
            # Ошибка БД будет поймана здесь, но миграция в init_db должна ее исправить
            print(f"Admin input processing error: {e}")
            send_message(chat_id, f"⚠️ **Ошибка обработки данных:** `{str(e).splitlines()[0]}`. Проверьте формат:\n`ТОКЕН | ТРЕК_НОМЕР | АДРЕС_ПВЗ | ПРИМЕРНАЯ_ДАТА_ПОЛУЧЕНИЯ`")
            return
    
    # User Logic
    order = get_order_by_tg_id(conn, str(chat_id))
    
    if 'contact' in message and order and order['status'] == STATUS_PENDING_AUTH:
        phone = message['contact']['phone_number']
        if update_order(conn, order_token=order['order_token'], phone_number=phone, status=STATUS_PENDING_FULL_NAME):
            remove_keyboard = {"remove_keyboard": True}
            send_message(chat_id, "✅ Телефон принят! Теперь введите ваше **ФИО** (Полностью):", reply_markup=remove_keyboard)
        else:
            send_message(chat_id, "⚠️ Ошибка обновления заказа. Попробуйте начать заново с сайта.", reply_markup={"remove_keyboard": True})
        return

    if text.startswith('/start'):
        params = text.split()
        if len(params) > 1 and params[1].startswith('auth_'):
            order_token = params[1].replace('auth_', '')
            update_success = update_order(conn, order_token=order_token, filter_user_tg_id=None, user_tg_id=str(chat_id), status=STATUS_PENDING_AUTH)
            if update_success:
                keyboard = {"keyboard": [[{"text": "📱 Отправить номер телефона", "request_contact": True}]], "one_time_keyboard": True, "resize_keyboard": True}
                send_message(chat_id, "👋 Привет! Мы получили ваш заказ.\nДля продолжения, пожалуйста, нажмите кнопку ниже, чтобы **подтвердить номер телефона**.", reply_markup=keyboard)
            else:
                send_message(chat_id, "⚠️ Ошибка: Заказ не найден или уже обработан.")
        else:
            send_message(chat_id, "Используйте кнопку 'Оформить заказ' на сайте.")
        return
        
    if order:
        order_token = order['order_token']
        current_status = order['status']
        
        if current_status == STATUS_PENDING_FULL_NAME:
            full_name = text.strip()
            if len(full_name) < 5 or len(full_name.split()) < 2:
                 send_message(chat_id, "⚠️ Пожалуйста, введите полное **ФИО** (минимум Имя и Фамилия).")
                 return
            update_order(conn, order_token=order_token, full_name=full_name, status=STATUS_PENDING_ADDRESS)
            send_message(chat_id, "Спасибо, **ФИО** принято!\n\nВведите ваш **адрес** (например: *город, улица, дом, квартира*). \n\n*❗ Обратите внимание: Мы будем использовать этот адрес для выбора ближайшего пункта выдачи СДЭК или Почты России.*")
            return
        elif current_status == STATUS_PENDING_ADDRESS:
            address = text.strip()
            if len(address) < 10:
                 send_message(chat_id, "⚠️ Пожалуйста, введите более полный и точный адрес.")
                 return
            update_order(conn, order_token=order_token, address=address, status=STATUS_PENDING_DELIVERY_TYPE)
            keyboard = {"inline_keyboard": [[{"text": "🚚 СДЭК", "callback_data": "delivery_sdek"}], [{"text": "📬 Почта России", "callback_data": "delivery_russian_post"}]]}
            send_message(chat_id, "✅ Адрес принят.\n\n**Выберите удобный способ получения заказа:**", reply_markup=keyboard)
            return
        elif current_status == STATUS_PENDING_DELIVERY_TYPE:
             keyboard = {"inline_keyboard": [[{"text": "🚚 СДЭК", "callback_data": "delivery_sdek"}], [{"text": "📬 Почта России", "callback_data": "delivery_russian_post"}]]}
             send_message(chat_id, "⚠️ Не удалось распознать способ. Выберите **СДЭК** или **Почта России**.", reply_markup=keyboard)
             return
        elif current_status == STATUS_PENDING_PAYMENT:
            if 'photo' in message or 'document' in message or text:
                 update_order(conn, order_token=order_token, status=STATUS_AWAITING_ADMIN)
                 order_data_full = get_order_by_tg_id(conn, str(chat_id))
                 if order_data_full:
                    send_admin_order_notification(order_data_full)
                 send_message(chat_id, "✨ **Отлично!** Мы получили ваше подтверждение оплаты.\n\nПередаем заказ администратору для оформления доставки и трек-номера. Это займет некоторое время.")
                 return

# --- MAIN ---
def application(environ, start_response):
    method = environ.get('REQUEST_METHOD', 'GET')
    path = environ.get('PATH_INFO', '/')
    if path == '/' and method in ['GET', 'HEAD']:
        start_response('200 OK', [('Content-type', 'text/plain')])
        return [b"Server is running"]
    conn = None
    try:
        conn = create_psql_connection()
        init_db(conn) # Вызываем миграцию при каждом обращении к серверу
        if method == 'OPTIONS':
            start_response('200 OK', CORS_HEADERS)
            return [b'']
        if method == 'POST' and path == '/init-auth':
            try:
                size = int(environ.get('CONTENT_LENGTH', 0))
                body = environ['wsgi.input'].read(size)
                data = json.loads(body)
                items = data.get('items', [])
                total_amount = data.get('total_amount', 0)
                if not items:
                    start_response('400 Bad Request', CORS_HEADERS + [('Content-Type', 'application/json')])
                    return [json.dumps({'error': 'No items provided'}).encode('utf-8')]
                order_token = str(uuid.uuid4()).replace('-', '')[:12]
                save_order_draft(conn, order_token, items, total_amount)
                tg_link = f"https://t.me/{TELEGRAM_BOT_USERNAME}?start=auth_{order_token}"
                resp = json.dumps({'success': True, 'telegram_bot_url': tg_link}).encode('utf-8')
                start_response('200 OK', CORS_HEADERS + [('Content-Type', 'application/json')])
                return [resp]
            except Exception as e:
                print(f"Init Auth Error: {e}")
                start_response('500 Internal Server Error', CORS_HEADERS + [('Content-Type', 'application/json')])
                return [json.dumps({'error': f"Internal Server Error: {str(e)}"}).encode('utf-8')]
        if method == 'POST' and path == '/webhook':
            try:
                size = int(environ.get('CONTENT_LENGTH', 0))
                body = environ['wsgi.input'].read(size)
                update = json.loads(body)
                handle_telegram_update(conn, update)
                start_response('200 OK', [('Content-Type', 'text/plain')])
                return [b'OK']
            except Exception as e:
                print(f"Webhook processing error: {e}") 
                start_response('200 OK', [('Content-Type', 'text/plain')]) 
                return [b'OK']
        start_response('404 Not Found', [('Content-Type', 'text/plain')])
        return [b'Not Found']
    except Exception as e:
        print(f"Critical Error: {e}")
        start_response('500 Internal Server Error', [('Content-Type', 'text/plain')])
        return [f"Critical Server Error: {str(e)}".encode('utf-8')]
    finally:
        if conn: conn.close()
