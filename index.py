import os
import json
import uuid
import psycopg2 
import requests 
import re 
from typing import Dict, Any

# --- КОНФИГУРАЦИЯ ---
DATABASE_URL = os.environ.get('DATABASE_URL')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
ADMIN_CHAT_ID = os.environ.get('ADMIN_CHAT_ID') 
TELEGRAM_BOT_USERNAME = os.environ.get('TELEGRAM_BOT_USERNAME', 'oopsmerchbot') 

CHECKOUT_URL = "https://oops-merch.ru/checkout"
ORDERS_TABLE_NAME = 'orders'
TG_API_BASE = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/'
CORS_HEADERS = [
    ('Access-Control-Allow-Origin', '*'), 
    ('Access-Control-Allow-Methods', 'POST, OPTIONS'), 
    ('Access-Control-Allow-Headers', 'Content-Type')
]

# --- ФУНКЦИИ БАЗЫ ДАННЫХ ---

def create_psql_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не установлена.")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn

def execute_db_command(query, params, conn, fetch=False):
    cursor = conn.cursor()
    cursor.execute(query, params)
    result = cursor.fetchone() if fetch else None
    cursor.close()
    return result

def save_order_to_db(conn, order_token, cart_data):
    cursor = conn.cursor()
    query = f"""
    INSERT INTO {ORDERS_TABLE_NAME} (order_token, status, cart_data, user_tg_id)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (order_token) DO UPDATE SET
        status = EXCLUDED.status,
        cart_data = EXCLUDED.cart_data;
    """
    cursor.execute(query, (order_token, "pending_phone_auth", json.dumps(cart_data), None))
    cursor.close()

def update_order_status_and_user(conn, order_token, status, user_tg_id=None, phone_number=None, check_file_id=None):
    cursor = conn.cursor()
    updates = []
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

def get_order_by_tg_id(conn, user_tg_id):
    cursor = conn.cursor()
    query = f"""
    SELECT order_token, status, cart_data, phone_number 
    FROM {ORDERS_TABLE_NAME} 
    WHERE user_tg_id = %s AND status != 'completed' AND status != 'cancelled'
    ORDER BY order_token DESC LIMIT 1;
    """
    cursor.execute(query, (user_tg_id,))
    result = cursor.fetchone()
    cursor.close()
    return result

def get_order_by_token(conn, order_token):
    cursor = conn.cursor()
    query = f"""
    SELECT order_token, status, cart_data, phone_number, user_tg_id, check_file_id
    FROM {ORDERS_TABLE_NAME} 
    WHERE order_token = %s;
    """
    cursor.execute(query, (order_token,))
    result = cursor.fetchone()
    cursor.close()
    return result

# --- ПОМОЩНИКИ TELEGRAM ---

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

# --- ОБРАБОТЧИКИ TELEGRAM ---

def handle_start(conn, update):
    message = update['message']
    chat_id = message['chat']['id']
    user_tg_id = message['from']['id']
    text = message['text']
    
    print(f"DEBUG: handle_start получил сообщение от {user_tg_id}: {text}")

    match = re.search(r'/start\s+(\w+)', text)
    order_token = match.group(1) if match else None
    
    if order_token:
        try:
            updated = update_order_status_and_user(conn, order_token, 'pending_phone_auth', user_tg_id=user_tg_id)
            if updated:
                keyboard = {
                    "keyboard": [[{"text": "Подтвердить номер телефона", "request_contact": True}]],
                    "one_time_keyboard": True,
                    "resize_keyboard": True
                }
                send_message(
                    chat_id, 
                    "✅ Заказ найден. Для продолжения оформления заказа, пожалуйста, подтвердите свой номер телефона, нажав на кнопку ниже:", 
                    reply_markup=keyboard
                )
            else:
                send_message(chat_id, "⚠️ Ошибка: Заказ не найден или уже обработан.")
        except Exception as e:
            print(f"!!! Error in handle_start: {e}")
            send_message(chat_id, f"❌ Ошибка: {e}")
    else:
         send_message(chat_id, "Привет! Используйте ссылку с сайта, чтобы начать оформление заказа.")


def handle_contact_share(conn, update):
    message = update['message']
    chat_id = message['chat']['id']
    user_tg_id = message['from']['id']
    phone_number = message['contact']['phone_number']

    order_data = get_order_by_tg_id(conn, user_tg_id)
    
    if order_data:
        order_token = order_data[0]
        update_order_status_and_user(conn, order_token, 'pending_payment_check', phone_number=phone_number)
        send_message(chat_id, f"✅ Номер **{phone_number}** подтвержден. Теперь пришлите фотографию (чек) об оплате.")
    else:
        send_message(chat_id, "⚠️ У вас нет активного заказа для подтверждения.")

def handle_check_submission(conn, update):
    try:
        message = update['message']
        chat_id = message['chat']['id']
        user_tg_id = message['from']['id']
        
        # 1. Проверяем заказ
        order_data = get_order_by_tg_id(conn, user_tg_id)
        if not order_data:
            send_message(chat_id, "⚠️ У вас нет активного заказа.")
            return

        order_token = order_data[0]
        
        # 2. Ищем файл
        file_id = None
        file_type = None
        if 'photo' in message and message['photo']:
            file_id = message['photo'][-1]['file_id']
            file_type = 'photo'
        elif 'document' in message and message['document']:
            file_id = message['document']['file_id']
            file_type = 'document'
        
        if file_id:
            # Обновляем статус
            update_order_status_and_user(conn, order_token, 'awaiting_admin_confirm', check_file_id=file_id)
            
            # Загружаем данные для админа
            full_order_data = get_order_by_token(conn, order_token)
            
            # === ЗАЩИТА ОТ БИТЫХ ДАННЫХ КОРЗИНЫ (ИСПРАВЛЕНИЕ ОШИБКИ 'str' object has no attribute 'get') ===
            try:
                # Пытаемся найти список товаров. Он может быть в 'items' или прямо в корне, или в 'cart_data'
                raw_cart = json.loads(full_order_data[2])
                cart = []
                
                # Нормализация данных корзины
                if isinstance(raw_cart, list):
                    cart = raw_cart
                elif isinstance(raw_cart, dict):
                    if 'items' in raw_cart and isinstance(raw_cart['items'], list):
                        cart = raw_cart['items']
                    else:
                        cart = [raw_cart] # Если это один словарь
                
                total_amount = 0
                items_text = ""
                
                for item in cart:
                    # КРИТИЧЕСКАЯ ПРОВЕРКА: Является ли item словарем?
                    if isinstance(item, dict):
                        name = item.get('name', 'Товар без названия')
                        size = item.get('size', '-')
                        qty = item.get('quantity', 1)
                        price = item.get('price', 0)
                        
                        total_amount += price * qty
                        items_text += f"— {name} ({size}) x{qty}\n"
                    else:
                        # Если item это строка (что вызывало ошибку), просто пишем её
                        items_text += f"— Некорректные данные товара: {str(item)}\n"

            except Exception as parse_e:
                print(f"Ошибка парсинга корзины: {parse_e}")
                total_amount = 0
                items_text = "Ошибка чтения состава заказа."

            # Формируем сообщение
            admin_message = (
                f"🔔 *НОВЫЙ ЗАКАЗ*\n"
                f"Токен: `{order_token}`\n"
                f"Телефон: `{full_order_data[3] or 'Нет данных'}`\n"
                f"Сумма: **{total_amount}** руб.\n\n"
                f"*Состав заказа:*\n{items_text}"
            )
            
            keyboard = {
                "inline_keyboard": [
                    [{"text": "✅ Оплата подтверждена", "callback_data": f"CONFIRM_{order_token}"}],
                    [{"text": "❌ Отклонить оплату", "callback_data": f"REJECT_{order_token}"}]
                ]
            }
            
            # Отправка админу
            req_payload = {
                'chat_id': ADMIN_CHAT_ID,
                'caption': admin_message,
                'parse_mode': 'Markdown',
                'reply_markup': keyboard
            }
            
            if file_type == 'photo':
                req_payload['photo'] = file_id
                send_tg_request('sendPhoto', req_payload)
            elif file_type == 'document':
                req_payload['document'] = file_id
                send_tg_request('sendDocument', req_payload)
            
            send_message(chat_id, "⏳ Ваш чек получен. Ожидайте подтверждения оплаты администратором.")

        else:
            send_message(chat_id, "⚠️ Пожалуйста, пришлите фотографию или документ (чек).")

    except Exception as e:
        print(f"!!! КРИТИЧЕСКАЯ ОШИБКА В handle_check_submission: {e}")
        try:
             send_message(chat_id, f"❌ Ошибка сервера: {e}")
        except:
             pass

def handle_callback_query(conn, update):
    callback_query = update['callback_query']
    data = callback_query['data']
    admin_id = callback_query['from']['id']
    message = callback_query['message']
    
    match = re.search(r'(CONFIRM|REJECT)_(\w+)', data)
    if not match:
        send_tg_request('answerCallbackQuery', {'callback_query_id': callback_query['id'], 'text': 'Неизвестная команда.'})
        return
        
    action = match.group(1)
    order_token = match.group(2)
    new_status = 'completed' if action == 'CONFIRM' else 'cancelled'
    
    if update_order_status_and_user(conn, order_token, new_status):
        new_caption = message.get('caption', '') + f"\n\n--- Статус ---\n"
        if action == 'CONFIRM':
            new_caption += f"✅ ОПЛАЧЕНО. Подтвердил: {admin_id}"
            user_msg = f"✅ Оплата подтверждена! Ваш заказ **{order_token}** оформлен."
        else:
            new_caption += f"❌ ОТКЛОНЕНО. Отклонил: {admin_id}"
            user_msg = f"❌ Оплата отклонена. Свяжитесь с администратором."
        
        send_tg_request('editMessageCaption', {
            'chat_id': message['chat']['id'],
            'message_id': message['message_id'],
            'caption': new_caption,
            'parse_mode': 'Markdown',
            'reply_markup': {"inline_keyboard": []}
        })
        
        order_data = get_order_by_token(conn, order_token)
        if order_data and order_data[4]: 
            send_message(order_data[4], user_msg)
            
        send_tg_request('answerCallbackQuery', {'callback_query_id': callback_query['id'], 'text': 'Статус обновлен.'})


# --- ГЛАВНЫЙ WSGI ОБРАБОТЧИК ---

def application(environ, start_response):
    method = environ.get('REQUEST_METHOD', 'GET')
    path = environ.get('PATH_INFO', '/')
    conn = None
    
    try:
        conn = create_psql_connection()
        
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

            # A. ЗАКАЗ С САЙТА
            if path == '/':
                # Сохраняем заказ, даже если структура не идеальна, чтобы получить токен
                cart_data = data.get('items', data) # Пытаемся найти items или берем всё
                order_token = str(uuid.uuid4()).replace('-', '')[:16] 
                save_order_to_db(conn, order_token, cart_data)
                
                deep_link = f'https://t.me/{TELEGRAM_BOT_USERNAME}?start={order_token}'
                resp = json.dumps({'deep_link': deep_link}).encode('utf-8')
                start_response('200 OK', CORS_HEADERS + [('Content-Type', 'application/json')])
                return [resp]

            # B. TELEGRAM WEBHOOK (/newhook)
            if path.startswith('/newhook') and data: 
                if 'message' in data:
                    msg = data['message']
                    if 'text' in msg and msg['text'].startswith('/start'):
                        handle_start(conn, data)
                    elif 'contact' in msg:
                        handle_contact_share(conn, data)
                    elif any(k in msg for k in ['photo', 'document', 'text', 'sticker']):
                        handle_check_submission(conn, data)
                elif 'callback_query' in data:
                    handle_callback_query(conn, data)
                
                start_response('200 OK', [('Content-type', 'text/plain')])
                return [b'OK']

            start_response('404 Not Found', [])
            return [b'Not Found']

        elif method == 'GET':
            start_response('200 OK', [('Content-type', 'text/plain')])
            return [b"OopsServer Running"]

    except Exception as e:
        print(f"CRITICAL: {e}")
        start_response('500 Error', [])
        return [json.dumps({'error': str(e)}).encode('utf-8')]
    finally:
        if conn: conn.close()
