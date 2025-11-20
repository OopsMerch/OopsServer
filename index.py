import os
import json
import uuid
import psycopg2 
import psycopg2.errors
import requests 
import hmac
import hashlib 
from typing import Dict, Any

# --- КОНФИГУРАЦИЯ ---
DATABASE_URL = os.environ.get('DATABASE_URL')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_BOT_USERNAME = os.environ.get('TELEGRAM_BOT_USERNAME', 'oopsmerchbot') 

# !!! ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ДЛЯ БИЗНЕС-ЛОГИКИ !!!
TG_ADMIN_GROUP_ID = os.environ.get('TG_ADMIN_GROUP_ID')
ADMIN_SUPPORT_USERNAME = os.environ.get('ADMIN_SUPPORT_USERNAME', '@oopssupport')

# ПЕРЕМЕННЫЕ ДЛЯ ОПЛАТЫ
SBERBANK_CARD = os.environ.get('SBERBANK_CARD', 'XXXX XXXX XXXX XXXX')
TBANK_CARD = os.environ.get('TBANK_CARD', 'YYYY YYYY YYYY YYYY')
ALFABANK_CARD = os.environ.get('ALFABANK_CARD', 'ZZZZ ZZZZ ZZZZ ZZZZ') 

ORDERS_TABLE_NAME = 'orders'
TG_API_BASE = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/'

# CORS заголовки
CORS_HEADERS = [
    ('Access-Control-Allow-Origin', '*'), 
    ('Access-Control-Allow-Methods', 'POST, GET, OPTIONS'), 
    ('Access-Control-Allow-Headers', 'Content-Type')
]

# --- МАШИНА СОСТОЯНИЙ ДЛЯ БОТА ---
STATUS_PENDING_AUTH = 'pending_phone_auth'
STATUS_PENDING_FULL_NAME = 'pending_full_name'
STATUS_PENDING_ADDRESS = 'pending_address'
STATUS_PENDING_DELIVERY_TYPE = 'pending_delivery_type'
STATUS_PENDING_CONFIRMATION = 'pending_confirmation'
STATUS_PENDING_PAYMENT = 'pending_payment'
STATUS_AWAITING_ADMIN = 'awaiting_admin_input'
STATUS_COMPLETED = 'completed'


# --- ФУНКЦИИ БАЗЫ ДАННЫХ ---

def create_psql_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is not set.")
    # ИСПРАВЛЕНИЕ: Преобразуем postgres:// в postgresql:// для корректной работы psycopg2
    conn_url = DATABASE_URL.replace('postgres://', 'postgresql://')
    conn = psycopg2.connect(conn_url)
    conn.autocommit = True
    return conn

def init_db(conn):
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
            
            # Проверка и добавление новых колонок (если таблица существовала)
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

def update_order(conn, order_token=None, user_tg_id=None, **kwargs):
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
    elif user_tg_id:
        # Ищем только "активные" заказы по user_tg_id
        where_clause = f"user_tg_id = %s AND status IN ('{STATUS_PENDING_AUTH}', '{STATUS_PENDING_FULL_NAME}', '{STATUS_PENDING_ADDRESS}', '{STATUS_PENDING_DELIVERY_TYPE}', '{STATUS_PENDING_CONFIRMATION}')"
        params.append(user_tg_id)
    else:
        return False
        
    query = f"UPDATE {ORDERS_TABLE_NAME} SET {', '.join(updates)} WHERE {where_clause}"
    
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.rowcount > 0

def get_order_by_tg_id(conn, user_tg_id):
    # --- НОВЫЕ ОТЛАДОЧНЫЕ СООБЩЕНИЯ ---
    print(f"DEBUG: Searching for active order for TG ID: {user_tg_id}") 
    # -----------------------------------
    
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
            # --- НОВЫЕ ОТЛАДОЧНЫЕ СООБЩЕНИЯ ---
            print(f"DEBUG: Order found (Token: {result['order_token']}, Status: {result['status']})")
            # -----------------------------------
            return result
        
        # --- НОВЫЕ ОТЛАДОЧНЫЕ СООБЩЕНИЯ ---
        print(f"DEBUG: No active order found for TG ID: {user_tg_id}") 
        # -----------------------------------
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
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Error sending message to {chat_id}: {e}")

def generate_admin_order_message(order_data):
    cart_data_raw = order_data['cart_data']
    if isinstance(cart_data_raw, str):
        cart_items = json.loads(cart_data_raw)
    else:
        cart_items = cart_data_raw

    # Форматирование списка товаров
    items_list = "\n".join([f"- {item['quantity']} шт. | {item['name']} (Размер: {item['size']}, {item.get('price', 'N/A')} ₽/шт.)" for item in cart_items])
    
    # Кнопка для администратора
    inline_keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Оформить (Админ)", "callback_data": f"admin_process_{order_data['order_token']}"}
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
         print("Warning: TG_ADMIN_GROUP_ID is not set. Cannot send admin notification.")
         return

    try:
        chat_id = int(TG_ADMIN_GROUP_ID)
    except ValueError:
        chat_id = TG_ADMIN_GROUP_ID
        
    message, reply_markup = generate_admin_order_message(order_data)
    send_message(chat_id, message, reply_markup=reply_markup)
    
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
    
# --- TELEGRAM BOT LOGIC (Handle Updates) ---

def handle_telegram_update(conn, update):
    
    if 'callback_query' in update:
        query = update['callback_query']
        chat_id = query['message']['chat']['id']
        message_id = query['message']['message_id']
        data = query['data']

        # --- АДМИНСКАЯ ЛОГИКА ---
        if data.startswith('admin_process_'):
            order_token = data.replace('admin_process_', '')
            order_data = get_order_by_token(conn, order_token)
            
            if order_data and order_data['status'] == STATUS_AWAITING_ADMIN: 
                 
                 response_text = f"✅ Заказ **{order_token}** принят в обработку. \n\n**Введите данные в формате:**\n`ТРЕК_НОМЕР | АДРЕС_ПВЗ | ПРИМЕРНАЯ_ДАТА_ПОЛУЧЕНИЯ`"
                 
                 # Редактируем сообщение, чтобы убрать кнопку
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
        
        # --- ПОЛЬЗОВАТЕЛЬСКАЯ ЛОГИКА ---
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
                keyboard = {
                    "inline_keyboard": [
                        [{"text": "✅ Подтвердить", "callback_data": "confirm_order"}],
                        [{"text": "❌ Заполнить заново", "callback_data": "start_over"}]
                    ]
                }
                
                edit_message_url = TG_API_BASE + 'editMessageText'
                requests.post(edit_message_url, json={
                    'chat_id': chat_id,
                    'message_id': query['message']['message_id'],
                    'text': confirmation_message,
                    'parse_mode': 'Markdown',
                    'reply_markup': json.dumps(keyboard)
                })
                
                return
            
            elif data == 'confirm_order' and order['status'] == STATUS_PENDING_CONFIRMATION:
                update_order(conn, order_token=order_token, status=STATUS_PENDING_PAYMENT)
                
                send_payment_details(chat_id, order)
                
                edit_message_url = TG_API_BASE + 'editMessageText'
                requests.post(edit_message_url, json={
                    'chat_id': chat_id,
                    'message_id': query['message']['message_id'],
                    'text': query['message']['text'] + '\n\n**Статус:** ✅ **Подтверждено.** Ожидаем оплаты.',
                    'parse_mode': 'Markdown',
                    'reply_markup': json.dumps({"inline_keyboard": []})
                })
                return
            
            elif data == 'start_over':
                update_order(
                    conn, 
                    order_token=order_token, 
                    full_name=None, 
                    address=None, 
                    delivery_type=None,
                    status=STATUS_PENDING_FULL_NAME
                )
                
                send_message(chat_id, "🔄 **Начинаем заново.** Введите ваше **ФИО** (Полностью):")
                edit_message_url = TG_API_BASE + 'editMessageText'
                requests.post(edit_message_url, json={
                    'chat_id': chat_id,
                    'message_id': query['message']['message_id'],
                    'text': query['message']['text'] + '\n\n**Статус:** ❌ **Сброшено.**',
                    'parse_mode': 'Markdown',
                    'reply_markup': json.dumps({"inline_keyboard": []})
                })
                return

        return
        
    if 'message' not in update: return
    
    message = update['message']
    chat_id = message['chat']['id']
    text = message.get('text', '')
    
    order = get_order_by_tg_id(conn, str(chat_id))
    
    # 1. ОБРАБОТКА КОНТАКТА
    # --- Логика будет работать, только если order не None и статус PENDING_AUTH ---
    if 'contact' in message and order and order['status'] == STATUS_PENDING_AUTH:
        
        phone = message['contact']['phone_number']
        
        # Попытка обновления данных и проверка успеха
        if update_order(conn, order_token=order['order_token'], phone_number=phone, status=STATUS_PENDING_FULL_NAME):
            
            print(f"DEBUG: Order {order['order_token']} updated successfully with phone {phone}. Sending next prompt.") 
            
            remove_keyboard = {"remove_keyboard": True}
            send_message(chat_id, "✅ Телефон принят! Теперь введите ваше **ФИО** (Полностью):", reply_markup=remove_keyboard)
        else:
            
            print(f"DEBUG: Order update FAILED for {order['order_token']} in PENDING_AUTH.") 
            
            send_message(chat_id, "⚠️ Ошибка обновления заказа. Попробуйте начать заново с сайта.", reply_markup={"remove_keyboard": True})
        
        return

    # 2. ОБРАБОТКА КОМАНДЫ START
    if text.startswith('/start'):
        params = text.split()
        if len(params) > 1 and params[1].startswith('auth_'):
            order_token = params[1].replace('auth_', '')
            
            if update_order(conn, order_token=order_token, user_tg_id=str(chat_id), status=STATUS_PENDING_AUTH):
                keyboard = {
                    "keyboard": [[{"text": "📱 Отправить номер телефона", "request_contact": True}]],
                    "one_time_keyboard": True,
                    "resize_keyboard": True
                }
                send_message(chat_id, "👋 Привет! Мы получили ваш заказ.\nДля продолжения, пожалуйста, нажмите кнопку ниже, чтобы **подтвердить номер телефона**.", reply_markup=keyboard)
            else:
                send_message(chat_id, "⚠️ Ошибка: Заказ не найден или уже обработан.")
        else:
            send_message(chat_id, "Используйте кнопку 'Оформить заказ' на сайте.")
        return
        
    # 3. ОБРАБОТКА ТЕКСТА (Диалог с пользователем)
    
    if order:
        order_token = order['order_token']
        current_status = order['status']
        
        # --- 3.1 ОЖИДАНИЕ ФИО ---
        if current_status == STATUS_PENDING_FULL_NAME:
            full_name = text.strip()
            if len(full_name) < 5 or len(full_name.split()) < 2:
                 send_message(chat_id, "⚠️ Пожалуйста, введите полное **ФИО** (минимум Имя и Фамилия).")
                 return
                 
            update_order(conn, order_token=order_token, full_name=full_name, status=STATUS_PENDING_ADDRESS)
            send_message(chat_id, "Спасибо, **ФИО** принято!\n\nВведите ваш **адрес** (например: *город, улица, дом, квартира*). \n\n*❗ Обратите внимание: Мы будем использовать этот адрес для выбора ближайшего пункта выдачи СДЭК или Почты России.*")
            return
            
        # --- 3.2 ОЖИДАНИЕ АДРЕСА ---
        elif current_status == STATUS_PENDING_ADDRESS:
            address = text.strip()
            if len(address) < 10:
                 send_message(chat_id, "⚠️ Пожалуйста, введите более полный и точный адрес.")
                 return
            
            update_order(conn, order_token=order_token, address=address, status=STATUS_PENDING_DELIVERY_TYPE)
            
            keyboard = {
                "inline_keyboard": [
                    [{"text": "🚚 СДЭК", "callback_data": "delivery_sdek"}],
                    [{"text": "📬 Почта России", "callback_data": "delivery_russian_post"}]
                ]
            }
            send_message(chat_id, "✅ Адрес принят.\n\n**Выберите удобный способ получения заказа:**", reply_markup=keyboard)
            return
            
        # --- 3.3 ОЖИДАНИЕ ВЫБОРА ДОСТАВКИ (КНОПКА) ---
        elif current_status == STATUS_PENDING_DELIVERY_TYPE:
             keyboard = {
                 "inline_keyboard": [
                     [{"text": "🚚 СДЭК", "callback_data": "delivery_sdek"}],
                     [{"text": "📬 Почта России", "callback_data": "delivery_russian_post"}]
                 ]
             }
             send_message(chat_id, "⚠️ Не удалось распознать способ. Выберите **СДЭК** или **Почта России**.", reply_markup=keyboard)
             return

        # --- 3.4 ОЖИДАНИЕ ФАЙЛА (ЧЕКА) ---
        elif current_status == STATUS_PENDING_PAYMENT:
            # Принимаем любой файл (photo, document) или текст как подтверждение
            if 'photo' in message or 'document' in message or text:
                 update_order(conn, order_token=order_token, status=STATUS_AWAITING_ADMIN)
                 
                 order_data_full = get_order_by_tg_id(conn, str(chat_id))
                 if order_data_full:
                    send_admin_order_notification(order_data_full)
                    
                 send_message(chat_id, "✨ **Отлично!** Мы получили ваше подтверждение оплаты.\n\nПередаем заказ администратору для оформления доставки и трек-номера. Это займет некоторое время.")
                 return
                 
        # --- 3.5 ОБРАБОТКА ОТВЕТА АДМИНИСТРАТОРА В ГРУППЕ ---
        
        global TG_ADMIN_GROUP_ID, ADMIN_SUPPORT_USERNAME
        
        # Проверяем, что сообщение пришло именно из группы администратора
        if str(chat_id) == TG_ADMIN_GROUP_ID:
            
            try:
                # Ожидаемый формат: ТРЕК_НОМЕР | АДРЕС_ПВЗ | ПРИМЕРНАЯ_ДАТА
                parts = [x.strip() for x in text.split('|')]
                if len(parts) != 3:
                     raise ValueError("Incorrect input format.")
                     
                track_number, pvz_address, delivery_date_str = parts

                # Ищем последний активный заказ в ожидании админа
                order_to_update = None
                with conn.cursor() as cur:
                     cur.execute(f"SELECT order_token, user_tg_id, full_name, cart_data FROM {ORDERS_TABLE_NAME} WHERE status = %s ORDER BY updated_at DESC LIMIT 1;", (STATUS_AWAITING_ADMIN,))
                     row = cur.fetchone()
                     if row:
                         columns = [desc[0] for desc in cursor.description]
                         order_to_update = dict(zip(columns, row))

                if order_to_update:
                    update_order(
                        conn, 
                        order_token=order_to_update['order_token'], 
                        admin_track_number=track_number, 
                        delivery_address_data=pvz_address,
                        admin_delivery_date=delivery_date_str, 
                        status=STATUS_COMPLETED
                    )

                    client_message = f"""
✅ **Ваш заказ оформлен!** (Токен: `{order_to_update['order_token']}`)

Вот **трек-номер**: `{track_number}`

Пункт выдачи: 
*{pvz_address}*

🕰️ Примерная дата получения:
**{delivery_date_str}**

---
🔗 По всем вопросам к администратору: {ADMIN_SUPPORT_USERNAME}
"""
                    send_message(int(order_to_update['user_tg_id']), client_message)
                    
                    send_message(chat_id, f"✅ Сообщение о доставке отправлено пользователю **{order_to_update['full_name']}** (Токен: {order_to_update['order_token']})")
                    return

                else:
                    send_message(chat_id, "⚠️ Не найден активный заказ в статусе 'Ожидает ввода'.")
                    return


            except Exception as e:
                print(f"Admin input parsing error: {e}")
                send_message(chat_id, "⚠️ **Неверный формат ввода.** Пожалуйста, используйте: \n`ТРЕК_НОМЕР | АДРЕС_ПВЗ | ПРИМЕРНАЯ_ДАТА_ПОЛУЧЕНИЯ`")
                return


# --- MAIN APPLICATION (WSGI) ---

def application(environ, start_response):
    method = environ.get('REQUEST_METHOD', 'GET')
    path = environ.get('PATH_INFO', '/')
    
    # Health check
    if path == '/' and method in ['GET', 'HEAD']:
        start_response('200 OK', [('Content-type', 'text/plain')])
        return [b"Server is running"]

    conn = None
    try:
        conn = create_psql_connection()
        init_db(conn) 

        # CORS
        if method == 'OPTIONS':
            start_response('200 OK', CORS_HEADERS)
            return [b'']

        # 1. INIT AUTH (Сайт -> Сервер)
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

        # 2. WEBHOOK (Telegram -> Сервер)
        if method == 'POST' and path == '/webhook':
            try:
                size = int(environ.get('CONTENT_LENGTH', 0))
                body = environ['wsgi.input'].read(size)
                update = json.loads(body)
                
                handle_telegram_update(conn, update)
                
                start_response('200 OK', [('Content-Type', 'text/plain')])
                return [b'OK']
            except Exception as e:
                # В случае ошибки всегда возвращаем 200 OK, чтобы Telegram не переотправлял
                print(f"Webhook processing error: {e}") 
                start_response('200 OK', [('Content-Type', 'text/plain')]) 
                return [b'OK']

        # 404 для всего остального
        start_response('404 Not Found', [('Content-Type', 'text/plain')])
        return [b'Not Found']

    except Exception as e:
        print(f"Critical Error: {e}")
        start_response('500 Internal Server Error', [('Content-Type', 'text/plain')])
        return [f"Critical Server Error: {str(e)}".encode('utf-8')]
    finally:
        if conn: conn.close()
