import os
import json
import uuid
import psycopg2 
import requests 
import re 
from typing import Dict, Any

# --- КОНФИГУРАЦИЯ ---
# ЭТИ ПЕРЕМЕННЫЕ ДОЛЖНЫ БЫТЬ УСТАНОВЛЕНЫ НА RENDER
DATABASE_URL = os.environ.get('DATABASE_URL')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
ADMIN_CHAT_ID = os.environ.get('ADMIN_CHAT_ID') # Ваш чат ID для уведомлений
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
    
    print(f"DEBUG: Разобранный токен: {order_token}")
    
    if order_token:
        try:
            updated = update_order_status_and_user(conn, order_token, 'pending_phone_auth', user_tg_id=user_tg_id)
            
            print(f"DEBUG: Обновление БД для {order_token}. Статус: {updated}")

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
                print("DEBUG: Сообщение об успехе отправлено.")
            else:
                send_message(chat_id, "⚠️ Ошибка: Заказ не найден или уже обработан.")
                print("DEBUG: Отправлена ошибка 'Заказ не найден'.")
                
        except Exception as e:
            error_message = f"!!! КРИТИЧЕСКАЯ ОШИБКА В handle_start: {e}"
            print(error_message)
            send_message(chat_id, f"❌ Критическая ошибка сервера: Пожалуйста, свяжитесь с администратором.")
            
    else:
         send_message(chat_id, "Привет! Используйте ссылку с сайта, чтобы начать оформление заказа.")
         print("DEBUG: Отправлено общее сообщение /start.")


def handle_contact_share(conn, update):
    message = update['message']
    chat_id = message['chat']['id']
    user_tg_id = message['from']['id']
    phone_number = message['contact']['phone_number']

    order_data = get_order_by_tg_id(conn, user_tg_id)
    
    if order_data:
        order_token = order_data[0]
        
        update_order_status_and_user(conn, order_token, 'pending_payment_check', phone_number=phone_number)
        
        send_message(
            chat_id, 
            f"✅ Номер **{phone_number}** подтвержден. Теперь пришлите фотографию (чек) об оплате."
        )
    else:
        send_message(chat_id, "⚠️ У вас нет активного заказа для подтверждения.")

def handle_check_submission(conn, update):
    try:
        # Прямой доступ к данным, предполагая, что update — это словарь (проверено в application)
        message: Dict[str, Any] = update['message']
        chat_id = message['chat']['id']
        user_tg_id = message['from']['id']
        
        # 1. Проверяем наличие активного заказа
        order_data = get_order_by_tg_id(conn, user_tg_id)

        if not order_data:
            send_message(chat_id, "⚠️ У вас нет активного заказа, ожидающего оплаты.")
            return

        order_token = order_data[0]
        
        file_id = None
        file_type = None
        
        # 2. Логика парсинга файла
        if 'photo' in message and message['photo']:
            # Получаем самую большую версию фото (последний элемент массива)
            file_id = message['photo'][-1]['file_id']
            file_type = 'photo'
        elif 'document' in message and message['document']:
            file_id = message['document']['file_id']
            file_type = 'document'
        
        if file_id:
            update_order_status_and_user(conn, order_token, 'awaiting_admin_confirm', check_file_id=file_id)
            
            full_order_data = get_order_by_token(conn, order_token)
            cart = json.loads(full_order_data[2])
            total_amount = sum(item.get('price', 0) * item.get('quantity', 1) for item in cart)

            admin_message = (
                f"🔔 *НОВЫЙ ЗАКАЗ*\n"
                f"Токен: `{order_token}`\n"
                f"Телефон: `{full_order_data[3] or 'Нет данных'}`\n"
                f"Сумма: **{total_amount}** руб.\n\n"
                f"*Состав заказа:*\n"
            )
            for item in cart:
                admin_message += f"— {item.get('name', 'Товар')} ({item.get('size', '-')}) x{item.get('quantity', 1)}\n"
            
            keyboard = {
                "inline_keyboard": [
                    [{"text": "✅ Оплата подтверждена", "callback_data": f"CONFIRM_{order_token}"}],
                    [{"text": "❌ Отклонить оплату", "callback_data": f"REJECT_{order_token}"}]
                ]
            }
            
            # Отправка уведомления администратору
            if file_type == 'photo':
                send_tg_request(
                    'sendPhoto',
                    {
                        'chat_id': ADMIN_CHAT_ID,
                        'photo': file_id,
                        'caption': admin_message,
                        'parse_mode': 'Markdown',
                        'reply_markup': keyboard
                    }
                )
            elif file_type == 'document':
                 send_tg_request(
                    'sendDocument',
                    {
                        'chat_id': ADMIN_CHAT_ID,
                        'document': file_id,
                        'caption': admin_message,
                        'parse_mode': 'Markdown',
                        'reply_markup': keyboard
                    }
                )
            
            send_message(chat_id, "⏳ Ваш чек получен. Ожидайте подтверждения оплаты администратором.")

        else:
            # Если пришел не файл (текст, стикер и т.д.)
            send_message(chat_id, "⚠️ Пожалуйста, пришлите фотографию или документ (чек) об оплате.")

    except KeyError as e:
        # Ловим ошибку, если в message нет нужных ключей
        print(f"!!! КРИТИЧЕСКАЯ ОШИБКА (KeyError) в handle_check_submission: {e}")
        try:
             send_message(message['chat']['id'], "⚠️ Пожалуйста, пришлите фотографию или документ (чек) об оплате, а не текст или стикер.")
        except:
             pass
    except Exception as e:
        # Лог с точным местом сбоя
        error_message = f"!!! КРИТИЧЕСКАЯ ОШИБКА В handle_check_submission: {e}"
        print(error_message)
        # Отправляем пользователю сообщение об ошибке
        try:
             send_message(chat_id, f"❌ Ошибка сервера при обработке чека: {e}")
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
        
        new_caption = message['caption'] + f"\n\n--- Статус ---\n"
        
        if action == 'CONFIRM':
            new_caption += f"✅ ОПЛАЧЕНО. Подтвердил: {admin_id}"
            user_msg = f"✅ Оплата подтверждена! Ваш заказ **{order_token}** оформлен и передан на сборку."
        else:
            new_caption += f"❌ ОТКЛОНЕНО. Отклонил: {admin_id}"
            user_msg = f"❌ Оплата отклонена. Пожалуйста, свяжитесь с администратором."
        
        # Обновляем сообщение администратора, убирая кнопки
        send_tg_request('editMessageCaption', {
            'chat_id': message['chat']['id'],
            'message_id': message['message_id'],
            'caption': new_caption,
            'parse_mode': 'Markdown',
            'reply_markup': {"inline_keyboard": []}
        })
        
        # Отправляем уведомление пользователю
        order_data = get_order_by_token(conn, order_token)
        if order_data and order_data[4]: 
            send_message(order_data[4], user_msg)
            
        send_tg_request('answerCallbackQuery', {'callback_query_id': callback_query['id'], 'text': 'Статус обновлен.'})
    else:
        send_tg_request('answerCallbackQuery', {'callback_query_id': callback_query['id'], 'text': 'Ошибка обновления статуса.'})


# --- ГЛАВНЫЙ WSGI ОБРАБОТЧИК ---

def application(environ, start_response):
    
    method = environ.get('REQUEST_METHOD', 'GET')
    path = environ.get('PATH_INFO', '/')
    conn = None
    
    try:
        conn = create_psql_connection()
        
        # 1. ОБРАБОТКА OPTIONS (CORS)
        if method == 'OPTIONS':
            start_response('200 OK', CORS_HEADERS)
            return [b'']
        
        # 2. ОБРАБОТКА POST
        elif method == 'POST':
            
            content_length = int(environ.get('CONTENT_LENGTH', '0'))
            request_body = environ['wsgi.input'].read(content_length)
            data = {}
            
            try:
                # ОЧЕНЬ ВАЖНО: Пытаемся декодировать JSON. Если не получается, data остается пустым словарем {}.
                data = json.loads(request_body.decode('utf-8'))
            except json.JSONDecodeError:
                print("DEBUG: Не удалось декодировать тело запроса в JSON.")
            except UnicodeDecodeError as e:
                print(f"DEBUG: Ошибка декодирования Юникода: {e}")


            # --- A. ОБРАБОТКА ЗАКАЗА С САЙТА (POST /) ---
            if path == '/':
                # Проверяем, похоже ли data на корзину (содержит ключи, связанные с заказом)
                if data and any(key in data for key in ['items', 'cart_data', 'total']):
                    
                    cart_data = data
                    order_token = str(uuid.uuid4()).replace('-', '')[:16] 

                    save_order_to_db(conn, order_token, cart_data)
                    
                    deep_link = f'https://t.me/{TELEGRAM_BOT_USERNAME}?start={order_token}'
                    response_data = json.dumps({'deep_link': deep_link}).encode('utf-8')
                    
                    response_headers = CORS_HEADERS + [('Content-Type', 'application/json'), 
                                                       ('Content-Length', str(len(response_data)))]

                    start_response('200 OK', response_headers)
                    return [response_data]

            # --- B. ОБРАБОТКА TELEGRAM WEBHOOK (POST /tgwebhook) ---
            # Обрабатывает запросы, пришедшие на /tgwebhook, или запросы с Telegram на /
            if path.startswith('/tgwebhook') or ('update_id' in data and 'message' in data): 
                update = data 
                
                if 'message' in update:
                    message = update['message']
                    if 'text' in message and message['text'].startswith('/start'):
                        handle_start(conn, update)
                    elif 'contact' in message:
                        handle_contact_share(conn, update)
                    # Финальная проверка для обработки контента (чек, текст, стикер)
                    elif 'photo' in message or 'document' in message or 'text' in message or 'sticker' in message:
                        handle_check_submission(conn, update)

                elif 'callback_query' in update:
                    handle_callback_query(conn, update)
                
                start_response('200 OK', [('Content-type', 'text/plain')])
                return [b'OK']

            # Если это POST-запрос на / или /tgwebhook, который не был обработан выше
            else:
                start_response('404 Not Found', [('Content-type', 'text/plain')])
                return [b'Not Found']

        # 3. ОБРАБОТКА GET
        elif method == 'GET':
            start_response('200 OK', [('Content-type', 'text/plain')])
            return [b"OopsServer is running. Send a POST request to / to start the order flow."]

        else:
            start_response('405 Method Not Allowed', [('Content-type', 'text/plain')])
            return [b'Method Not Allowed']

    except Exception as e:
        print(f"Критическая ошибка выполнения: {e}")
        error_msg = json.dumps({'error': f'Критическая ошибка сервера: {str(e)}'}).encode('utf-8')
        
        response_headers = CORS_HEADERS + [('Content-Type', 'application/json'), ('Content-Length', str(len(error_msg)))]
        start_response('500 Internal Server Error', response_headers)
        return [error_msg]
        
    finally:
        if conn:
            conn.close()
