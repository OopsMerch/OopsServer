import os
import json
import uuid
import psycopg2 
import requests 
import re 
import hmac
import hashlib 
from urllib.parse import parse_qsl 
from typing import Dict, Any

# --- CONFIGURATION ---
DATABASE_URL = os.environ.get('DATABASE_URL')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
ADMIN_CHAT_ID = os.environ.get('ADMIN_CHAT_ID') 
TELEGRAM_BOT_USERNAME = os.environ.get('TELEGRAM_BOT_USERNAME', 'oopsmerchbot') 
SITE_BASE_URL = os.environ.get('SITE_BASE_URL', 'https://oops-merch.ru') 

ORDERS_TABLE_NAME = 'orders'
TG_API_BASE = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/'
CORS_HEADERS = [
    ('Access-Control-Allow-Origin', '*'), 
    ('Access-Control-Allow-Methods', 'POST, GET, OPTIONS'), 
    ('Access-Control-Allow-Headers', 'Content-Type')
]

# --- TELEGRAM AUTH FUNCTION ---

def verify_telegram_authorization(auth_data: Dict[str, str]) -> bool:
    if not auth_data or 'hash' not in auth_data or not TELEGRAM_BOT_TOKEN:
        return False
    data_list = []
    for key, value in auth_data.items():
        if key != 'hash':
            data_list.append(f"{key}={value}")
    data_list.sort()
    data_check_string = '\n'.join(data_list)
    secret_key = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode('utf-8')).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return calculated_hash == auth_data['hash']

# --- DATABASE FUNCTIONS ---

def create_psql_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is not set.")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn

def init_db(conn):
    cursor = conn.cursor()
    
    # 1. Создание таблицы users
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_tg_id BIGINT PRIMARY KEY,
            username VARCHAR(255),
            first_name VARCHAR(255),
            last_name VARCHAR(255),
            phone_number VARCHAR(50),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 2. Создание таблицы orders
    # NOTE: Используем text[] для items.
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {ORDERS_TABLE_NAME} (
            id SERIAL PRIMARY KEY,
            order_token VARCHAR(32) UNIQUE NOT NULL,
            status VARCHAR(50) NOT NULL,
            user_tg_id BIGINT,
            phone_number VARCHAR(50),
            cart_data JSONB NOT NULL,
            full_name VARCHAR(255),
            email VARCHAR(255),
            delivery_type VARCHAR(50),
            post_index VARCHAR(20),
            city VARCHAR(255),
            address_line TEXT,
            pvz_id VARCHAR(50),
            payment_amount DECIMAL(10, 2),
            payment_method_details VARCHAR(255),
            check_file_id VARCHAR(255),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 3. Добавление колонки total_amount, если её нет (исправление предыдущей ошибки)
    try:
        # PostgreSQL function to check if a column exists
        cursor.execute(f"SELECT 1 FROM information_schema.columns WHERE table_name='{ORDERS_TABLE_NAME}' AND column_name='total_amount';")
        if not cursor.fetchone():
            cursor.execute(f"ALTER TABLE {ORDERS_TABLE_NAME} ADD COLUMN total_amount DECIMAL(10, 2);")
            print(f"Added column 'total_amount' to {ORDERS_TABLE_NAME} table.")
    except Exception as e:
        # Ignore if column addition fails for non-existence check reasons
        print(f"Error during check/add 'total_amount' column: {e}")
        pass 
    
    conn.commit()
    cursor.close()
    print("Database initialized/checked successfully.")


def save_order_draft(conn, order_token, cart_data):
    cursor = conn.cursor()
    # Измененный запрос для сохранения total_amount
    total_amount = cart_data.get('total_amount', 0)
    items_data = cart_data.get('items', [])
    
    # Измененный запрос
    query = f"""
    INSERT INTO {ORDERS_TABLE_NAME} (order_token, status, cart_data, total_amount)
    VALUES (%s, %s, %s, %s);
    """
    cursor.execute(query, (order_token, "pending_phone_auth", json.dumps(items_data), total_amount))
    cursor.close()

def update_order_status_and_user(conn, order_token, status, user_tg_id=None, phone_number=None, check_file_id=None):
    cursor = conn.cursor()
    updates = ["updated_at = CURRENT_TIMESTAMP"]
    params = []
    
    if user_tg_id is not None:
        updates.append("user_tg_id = %s")
        params.append(user_tg_id)
    if phone_number is not None:
        updates.append("phone_number = %s")
        params.append(phone_number)
    if check_file_id is not None:
        updates.append("check_file_id = %s")
        params.append(check_file_id)
    
    updates.append("status = %s")
    params.append(status)
    params.append(order_token)

    query = f"""
    UPDATE {ORDERS_TABLE_NAME}
    SET {', '.join(updates)}
    WHERE order_token = %s;
    """
    cursor.execute(query, params)
    cursor.close()
    return cursor.rowcount > 0

def update_order_full_details(conn, data):
    cursor = conn.cursor()
    query = f"""
    UPDATE {ORDERS_TABLE_NAME}
    SET 
        status = %s,
        full_name = %s,
        email = %s,
        delivery_type = %s,
        post_index = %s,
        city = %s,
        address_line = %s,
        pvz_id = %s,
        payment_amount = %s,
        payment_method_details = %s,
        updated_at = CURRENT_TIMESTAMP
    WHERE order_token = %s;
    """
    params = (
        'pending_check_submission', 
        data.get('full_name'),
        data.get('email'),
        data.get('delivery_type'),
        data.get('post_index'),
        data.get('city'),
        data.get('address_line'),
        data.get('pvz_id'),
        data.get('payment_amount'),
        data.get('payment_method_details'),
        data.get('order_token')
    )
    cursor.execute(query, params)
    cursor.close()
    return cursor.rowcount > 0

def get_order_by_tg_id(conn, user_tg_id):
    cursor = conn.cursor()
    # NOTE: total_amount добавлен в выборку
    query = f"""
    SELECT order_token, status, cart_data, phone_number, user_tg_id, check_file_id, total_amount
    FROM {ORDERS_TABLE_NAME} 
    WHERE user_tg_id = %s AND status NOT IN ('completed', 'cancelled')
    ORDER BY created_at DESC LIMIT 1;
    """
    cursor.execute(query, (user_tg_id,))
    result = cursor.fetchone()
    cursor.close()
    return result

def get_order_by_token(conn, order_token):
    cursor = conn.cursor()
    # NOTE: total_amount добавлен в выборку (индекс 15)
    query = f"""
    SELECT 
        order_token, status, cart_data, phone_number, user_tg_id, check_file_id, 
        full_name, delivery_type, post_index, city, address_line, pvz_id, 
        payment_amount, payment_method_details, email, total_amount
    FROM {ORDERS_TABLE_NAME} 
    WHERE order_token = %s;
    """
    cursor.execute(query, (order_token,))
    result = cursor.fetchone()
    cursor.close()
    return result

# --- TELEGRAM HELPERS ---

def send_tg_request(method, payload):
    url = TG_API_BASE + method
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Telegram API Error ({method}): {e}")
        return None

def send_message(chat_id, text, reply_markup=None):
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}
    if reply_markup:
        payload['reply_markup'] = reply_markup
    return send_tg_request('sendMessage', payload)

# --- TELEGRAM HANDLERS ---

def handle_start(conn, update):
    message = update['message']
    chat_id = message['chat']['id']
    user_tg_id = message['from']['id']
    text = message['text']
    
    # Ищем токен (с префиксом или без)
    match = re.search(r'/start\s+(\w+)', text)
    order_token_full = match.group(1) if match else None
    
    if not order_token_full:
         send_message(chat_id, "Используйте ссылку с сайта, чтобы начать процесс оформления заказа.")
         return

    # Логика обработки токенов с префиксом
    if order_token_full.startswith('pay_'):
        order_token = order_token_full.replace('pay_', '')
        handle_pay_start(conn, chat_id, user_tg_id, order_token) 
    elif order_token_full.startswith('auth_'):
        order_token = order_token_full.replace('auth_', '')
        handle_auth_start(conn, chat_id, user_tg_id, order_token)
    else:
        # Для обратной совместимости или если токен пришел без префикса (первый шаг)
        handle_auth_start(conn, chat_id, user_tg_id, order_token_full)

def handle_auth_start(conn, chat_id, user_tg_id, order_token):
    try:
        # В этом месте order_token должен быть ЧИСТЫМ токеном (без auth_)
        updated = update_order_status_and_user(conn, order_token, 'pending_phone_auth', user_tg_id=user_tg_id)
        if updated:
            keyboard = {
                "keyboard": [[{"text": "Подтвердить номер телефона", "request_contact": True}]],
                "one_time_keyboard": True,
                "resize_keyboard": True
            }
            send_message(
                chat_id, 
                "✅ Заказ найден. Для продолжения, **подтвердите свой номер телефона**:", 
                reply_markup=keyboard
            )
        else:
            send_message(chat_id, "⚠️ Ошибка: Заказ не найден или уже обработан.")
    except Exception as e:
        print(f"Error in handle_auth_start: {e}")
        send_message(chat_id, f"❌ Ошибка сервера.")

def handle_pay_start(conn, chat_id, user_tg_id, order_token):
    order_data = get_order_by_token(conn, order_token)
    
    # Индекс 4 это user_tg_id, Индекс 15 это total_amount
    if order_data and order_data[4] == user_tg_id: 
        # Обновляем статус, но не меняем user_tg_id, так как он уже есть
        update_order_status_and_user(conn, order_token, 'pending_check_submission') 
        
        # NOTE: Добавлено отображение суммы
        total_amount = order_data[15] if len(order_data) > 15 and order_data[15] is not None else 'N/A'
        
        send_message(
            chat_id, 
            f"💰 Ваш заказ на сумму **{total_amount} RUB** оформлен.\nПожалуйста, пришлите нам **фотографию или скриншот (чек)** для подтверждения оплаты."
        )
    else:
        send_message(chat_id, "⚠️ Ошибка: Заказ не найден или не связан с вашей учетной записью.")


def handle_contact_share(conn, update):
    message = update['message']
    chat_id = message['chat']['id']
    user_tg_id = message['from']['id']
    phone_number = message['contact']['phone_number']

    order_data = get_order_by_tg_id(conn, user_tg_id)
    
    if order_data:
        order_token = order_data[0]
        update_order_status_and_user(conn, order_token, 'pending_delivery_data', phone_number=phone_number)
        
        # URL для возврата на сайт
        # NOTE: SITE_BASE_URL должен быть корректно настроен
        redirect_url = f"{SITE_BASE_URL}" # Теперь просто возвращаем на главную, т.к. фронтенд сам проверяет.
        
        keyboard = {
            "inline_keyboard": [
                [{"text": "Вернуться к оформлению заказа", "url": redirect_url}]
            ]
        }
        send_message(
            chat_id, 
            "✅ **Номер подтвержден**! Возвращайтесь на сайт, чтобы заполнить данные доставки.", 
            reply_markup=keyboard
        )
    else:
        send_message(chat_id, "⚠️ У вас нет активных заказов для подтверждения.")

def handle_check_submission(conn, update):
    try:
        message = update['message']
        chat_id = message['chat']['id']
        user_tg_id = message['from']['id']
        
        order_data = get_order_by_tg_id(conn, user_tg_id)
        # Индекс 1: status
        if not order_data or order_data[1] != 'pending_check_submission':
            send_message(chat_id, "⚠️ У вас нет активных заказов, ожидающих чек.")
            return

        order_token = order_data[0]
        
        file_id = None
        file_type = None
        if 'photo' in message and message['photo']:
            file_id = message['photo'][-1]['file_id']
            file_type = 'photo'
        elif 'document' in message and message['document']:
            file_id = message['document']['file_id']
            file_type = 'document'
        
        if file_id:
            update_order_status_and_user(conn, order_token, 'awaiting_admin_confirm', check_file_id=file_id)
            full_order_data = get_order_by_token(conn, order_token)
            
            try:
                # Индекс 2: cart_data (JSONB)
                raw_cart = json.loads(full_order_data[2])
                items_text = ""
                # Индекс 15: total_amount
                total_amount = full_order_data[15] if full_order_data[15] is not None else 0

                if isinstance(raw_cart, list):
                    for item in raw_cart:
                        if isinstance(item, dict):
                            name = item.get('name', 'Item')
                            size = item.get('size', '-')
                            qty = item.get('quantity', 1)
                            items_text += f"— {name} ({size}) x{qty}\n"
                        else:
                            items_text += f"— {str(item)}\n"
                else:
                    items_text = "Error reading items."

            except Exception as e:
                print(f"Error parsing data: {e}")
                items_text = "Error reading items."
                total_amount = 0

            admin_message = (
                f"🔔 **NEW ORDER (AWAITING CONFIRMATION)**\n"
                f"Token: `{order_token}`\n"
                f"Amount: **{total_amount} RUB** ({full_order_data[13] or 'N/A'})\n"
                f"--- **CUSTOMER DATA** ---\n"
                f"**Name:** {full_order_data[6] or 'N/A'}\n"
                f"**Phone:** {full_order_data[3] or 'N/A'}\n"
                f"**Email:** {full_order_data[14] or 'N/A'}\n"
                f"--- **DELIVERY ({full_order_data[7] or 'N/A'})** ---\n"
                f"**Index:** {full_order_data[8] or 'N/A'}\n"
                f"**City:** {full_order_data[9] or 'N/A'}\n"
                f"**Address:** {full_order_data[10] or 'N/A'}\n"
                f"**PVZ:** {full_order_data[11] or 'N/A'}\n"
                f"--- **ORDER ITEMS** ---\n{items_text}"
            )
            
            keyboard = {
                "inline_keyboard": [
                    [{"text": "✅ Confirm Payment", "callback_data": f"CONFIRM_{order_token}"}],
                    [{"text": "❌ Reject Payment", "callback_data": f"REJECT_{order_token}"}]
                ]
            }
            
            req_payload = {
                'chat_id': ADMIN_CHAT_ID,
                'caption': admin_message,
                'reply_markup': keyboard,
                'parse_mode': 'Markdown'
            }
            
            # NOTE: sendPhoto/sendDocument может вернуть ошибку, если file_id неверный.
            if file_type == 'photo':
                send_tg_request('sendPhoto', req_payload)
            elif file_type == 'document':
                send_tg_request('sendDocument', req_payload)
            
            send_message(chat_id, "⏳ Ваш чек получен. Ожидание подтверждения администратором.")

        else:
            send_message(chat_id, "⚠️ Пожалуйста, отправьте фотографию или документ (чек).")

    except Exception as e:
        print(f"CRITICAL ERROR in handle_check_submission: {e}")
        try: send_message(chat_id, f"❌ Ошибка сервера.")
        except: pass

def handle_callback_query(conn, update):
    try:
        callback_query = update['callback_query']
        data = callback_query['data']
        admin_id = callback_query['from']['id']
        message = callback_query['message']
        
        match = re.search(r'(CONFIRM|REJECT)_(\w+)', data)
        if not match: return
            
        action = match.group(1)
        order_token = match.group(2)
        new_status = 'completed' if action == 'CONFIRM' else 'cancelled'
        
        if update_order_status_and_user(conn, order_token, new_status):
            # Извлекаем текущий заголовок/подпись, добавляем информацию о статусе
            new_caption = message.get('caption', '') + f"\n\n--- Status ---\n"
            
            order_data = get_order_by_token(conn, order_token)
            user_tg_id = order_data[4] if order_data else None

            if action == 'CONFIRM':
                new_caption += f"✅ ОПЛАЧЕНО. Подтверждено: {admin_id}"
                user_msg = f"✅ **Оплата подтверждена!** Ваш заказ **принят в обработку**."
            else:
                new_caption += f"❌ ОТКЛОНЕНО. Отклонено: {admin_id}"
                user_msg = f"❌ **Оплата отклонена.** Пожалуйста, свяжитесь с менеджером: @{TELEGRAM_BOT_USERNAME}"
            
            # Редактируем сообщение администратору
            send_tg_request('editMessageCaption', {
                'chat_id': message['chat']['id'],
                'message_id': message['message_id'],
                'caption': new_caption,
                'reply_markup': {"inline_keyboard": []}, # Удаляем кнопки
                'parse_mode': 'Markdown'
            })
            
            # Отправляем сообщение пользователю
            if user_tg_id: 
                send_message(user_tg_id, user_msg)
                
            send_tg_request('answerCallbackQuery', {'callback_query_id': callback_query['id'], 'text': 'Статус обновлен.'})
    except Exception as e:
        print(f"Callback error: {e}")

# --- MAIN WSGI APPLICATION ---

def application(environ, start_response):
    
    # 0. Инициализация базы данных (выполняется при каждом запуске Gunicorn/Render)
    try:
        conn = create_psql_connection()
        init_db(conn) 
        conn.close() # Закрываем соединение, используемое только для init
    except Exception as e:
        print(f"DB Initialization Error: {e}")
        # Продолжаем работу, но в случае фатальной ошибки DB, запросы будут падать

    method = environ.get('REQUEST_METHOD', 'GET')
    path = environ.get('PATH_INFO', '/')
    conn = None
    
    # GUNICORN HEALTH CHECK FIX: Handle base path without touching DB first
    if path == '/' and method in ('GET', 'HEAD'):
        start_response('200 OK', [('Content-type', 'text/plain')])
        return [b"OopsServer Running - Health OK"]
    
    try:
        conn = create_psql_connection() # Connect only for non-trivial requests
        
        if method == 'OPTIONS':
            start_response('200 OK', CORS_HEADERS)
            return [b'']
        
        elif method == 'POST':
            length = int(environ.get('CONTENT_LENGTH', '0'))
            body = environ['wsgi.input'].read(length)
            data = {}
            try:
                data = json.loads(body.decode('utf-8'))
                if not isinstance(data, dict): data = {}
            except: pass

            # 1. ORDER INITIATION (SITE -> BOT)
            if path == '/init-auth':
                # cart_data теперь включает total_amount
                cart_data = data 
                order_token = str(uuid.uuid4()).replace('-', '')[:16] 
                
                save_order_draft(conn, order_token, cart_data)
                
                # ИСПРАВЛЕНИЕ: Добавлен префикс 'auth_' для четкого разделения команд
                tg_link = f"https://t.me/{TELEGRAM_BOT_USERNAME}?start=auth_{order_token}"
                
                # NOTE: Возвращаем telegram_bot_url, как ожидает фронтенд
                resp = json.dumps({'success': True, 'telegram_bot_url': tg_link}).encode('utf-8')
                start_response('200 OK', CORS_HEADERS + [('Content-Type', 'application/json')])
                return [resp]

            # 2. SUBMIT FULL DELIVERY DATA (SITE -> SERVER)
            if path == '/submit-full-order':
                if update_order_full_details(conn, data):
                    order_token = data.get('order_token')
                    # Префикс 'pay_' для второго шага
                    tg_check_link = f"https://t.me/{TELEGRAM_BOT_USERNAME}?start=pay_{order_token}"

                    resp = json.dumps({
                        'success': True, 
                        'message': 'Data saved. Redirect to Telegram for check.',
                        'tg_check_url': tg_check_link,
                        'order_token': order_token
                    }).encode('utf-8')
                    start_response('200 OK', CORS_HEADERS + [('Content-Type', 'application/json')])
                    return [resp]
                else:
                    start_response('400 Bad Request', CORS_HEADERS + [('Content-Type', 'application/json')])
                    return [json.dumps({'error': 'Order not found for update'}).encode('utf-8')]
                
            # 3. TELEGRAM WEBHOOK
            if path.startswith('/newhook') and data: 
                if 'message' in data:
                    msg = data['message']
                    if 'text' in msg and msg['text'].startswith('/start'):
                        handle_start(conn, data)
                    elif 'contact' in msg:
                        handle_contact_share(conn, data)
                    elif any(k in msg for k in ['photo', 'document']):
                        handle_check_submission(conn, data)
                elif 'callback_query' in data:
                    handle_callback_query(conn, data)
                
                start_response('200 OK', [('Content-type', 'text/plain')])
                return [b'OK']

            start_response('404 Not Found', [('Content-type', 'text/plain')])
            return [b'Not Found']

        elif method == 'GET':
            
            # 1. TELEGRAM LOGIN CALLBACK (SITE)
            if path == '/tg-login-callback':
                query_string = environ.get('QUERY_STRING', '')
                params = dict(parse_qsl(query_string))

                if verify_telegram_authorization(params):
                    user_id = params.get('id')
                    success_html = f"""
                        <!DOCTYPE html>
                        <html>
                        <body>
                            <script>
                                localStorage.setItem('telegram_user_id', '{user_id}'); 
                                window.location.replace('{SITE_BASE_URL}'); 
                            </script>
                        </body>
                        </html>
                    """
                    start_response('200 OK', [('Content-Type', 'text/html')])
                    return [success_html.encode('utf-8')]
                else:
                    start_response('401 Unauthorized', [('Content-Type', 'text/html')])
                    return ["<h1>Telegram authorization failed.</h1>".encode('utf-8')]
            
            # 2. ORDER STATUS POLLING (SITE)
            if path.startswith('/status/'):
                order_token = path.split('/')[-1]
                order_data = get_order_by_token(conn, order_token)
                
                if order_data:
                    status = order_data[1] 
                    resp = json.dumps({'status': status}).encode('utf-8')
                    start_response('200 OK', CORS_HEADERS + [('Content-Type', 'application/json')])
                    return [resp]
                else:
                    start_response('404 Not Found', [('Content-type', 'application/json')])
                    return [json.dumps({'error': 'Order not found'}).encode('utf-8')]
            
            start_response('404 Not Found', [('Content-type', 'text/plain')])
            return [b'Not Found']
        
    except Exception as e:
        print(f"CRITICAL: {e}")
        start_response('500 Error', [('Content-type', 'application/json')])
        return [json.dumps({'error': str(e)}).encode('utf-8')]
    finally:
        if conn: conn.close()
