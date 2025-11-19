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

# --- КОНФИГУРАЦИЯ (БЕРЕТСЯ ИЗ RENDER) ---
# Все переменные берутся из окружения Render
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

# --- ФУНКЦИИ ПРОВЕРКИ АВТОРИЗАЦИИ TELEGRAM ---

def verify_telegram_authorization(auth_data: Dict[str, str]) -> bool:
    """Проверяет подлинность данных, присланных Telegram."""
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

def save_order_draft(conn, order_token, cart_data):
    """Сохраняет черновик заказа (только корзина) на этапе init-auth."""
    cursor = conn.cursor()
    query = f"""
    INSERT INTO {ORDERS_TABLE_NAME} (order_token, status, cart_data)
    VALUES (%s, %s, %s);
    """
    cursor.execute(query, (order_token, "pending_phone_auth", json.dumps(cart_data)))
    cursor.close()

def update_order_status_and_user(conn, order_token, status, user_tg_id=None, phone_number=None, check_file_id=None):
    """Обновляет статус и основные поля заказа."""
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
    """Обновляет заказ всеми данными доставки с сайта."""
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
        'pending_check_submission', # Новый статус: ждем чек
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
    """Получает последний активный заказ по ID пользователя Telegram."""
    cursor = conn.cursor()
    query = f"""
    SELECT order_token, status, cart_data, phone_number, user_tg_id, check_file_id
    FROM {ORDERS_TABLE_NAME} 
    WHERE user_tg_id = %s AND status != 'completed' AND status != 'cancelled'
    ORDER BY created_at DESC LIMIT 1;
    """
    cursor.execute(query, (user_tg_id,))
    result = cursor.fetchone()
    cursor.close()
    return result

def get_order_by_token(conn, order_token):
    """Получает заказ по его токену. Возвращает все поля."""
    cursor = conn.cursor()
    # Поля: 0:order_token, 1:status, 2:cart_data, 3:phone_number, 4:user_tg_id, 5:check_file_id, 
    #       6:full_name, 7:delivery_type, 8:post_index, 9:city, 10:address_line, 11:pvz_id, 
    #       12:payment_amount, 13:payment_method_details, 14:email
    query = f"""
    SELECT 
        order_token, status, cart_data, phone_number, user_tg_id, check_file_id, 
        full_name, delivery_type, post_index, city, address_line, pvz_id, 
        payment_amount, payment_method_details, email
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
    
    match = re.search(r'/start\s+(\w+)', text)
    order_token_full = match.group(1) if match else None
    
    if not order_token_full:
         send_message(chat_id, "Привет! Используйте ссылку с сайта, чтобы начать оформление заказа.")
         return

    if order_token_full.startswith('pay_'):
        # Сценарий 2: ПОДТВЕРЖДЕНИЕ ОПЛАТЫ (Пришлите чек)
        order_token = order_token_full.replace('pay_', '')
        handle_pay_start(conn, chat_id, user_tg_id, order_token) 
    else:
        # Сценарий 1: НАЧАЛЬНАЯ АВТОРИЗАЦИЯ (Запрос номера)
        handle_auth_start(conn, chat_id, user_tg_id, order_token_full)

def handle_auth_start(conn, chat_id, user_tg_id, order_token):
    """Обрабатывает начальный /start для авторизации и привязки ID."""
    try:
        # Привязываем user_tg_id к заказу и устанавливаем статус ожидания номера
        updated = update_order_status_and_user(conn, order_token, 'pending_phone_auth', user_tg_id=user_tg_id)
        if updated:
            keyboard = {
                "keyboard": [[{"text": "Подтвердить номер телефона", "request_contact": True}]],
                "one_time_keyboard": True,
                "resize_keyboard": True
            }
            send_message(
                chat_id, 
                "✅ Заказ найден. Для продолжения оформления заказа, пожалуйста, **подтвердите свой номер телефона**, нажав на кнопку ниже:", 
                reply_markup=keyboard
            )
        else:
            send_message(chat_id, "⚠️ Ошибка: Заказ не найден или уже обработан.")
    except Exception as e:
        print(f"!!! Error in handle_auth_start: {e}")
        send_message(chat_id, f"❌ Ошибка: {e}")

def handle_pay_start(conn, chat_id, user_tg_id, order_token):
    """Обрабатывает /start pay_<token> для запроса чека."""
    order_data = get_order_by_token(conn, order_token)
    
    # Проверяем, что заказ существует и ID пользователя совпадает (индекс 4)
    if order_data and order_data[4] == user_tg_id: 
        # Обновляем статус: ждем чек
        update_order_status_and_user(conn, order_token, 'pending_check_submission', user_tg_id=user_tg_id)
        
        send_message(
            chat_id, 
            "💰 **Оплата произведена!** Пожалуйста, пришлите нам **фотографию или скриншот (чек) об оплате** для подтверждения.\n\n_На сайте начнется ожидание подтверждения._"
        )
    else:
        send_message(chat_id, "⚠️ Ошибка: Заказ не найден или не связан с вашим Telegram-аккаунтом.")


def handle_contact_share(conn, update):
    """Обрабатывает получение номера и перенаправляет обратно на сайт."""
    message = update['message']
    chat_id = message['chat']['id']
    user_tg_id = message['from']['id']
    phone_number = message['contact']['phone_number']

    order_data = get_order_by_tg_id(conn, user_tg_id)
    
    if order_data:
        order_token = order_data[0]
        # Обновляем статус: номер получен, ждем заполнения полной формы
        update_order_status_and_user(conn, order_token, 'pending_delivery_data', phone_number=phone_number)
        
        # Ссылка для возврата на сайт для оформления (с токеном и ID)
        redirect_url = f"{SITE_BASE_URL}/checkout?token={order_token}&tg_id={user_tg_id}"
        
        keyboard = {
            "inline_keyboard": [
                [{"text": "Вернуться к оформлению заказа", "url": redirect_url}]
            ]
        }
        send_message(
            chat_id, 
            "✅ **Номер подтвержден**! Все прекрасно, можете возвращаться на сайт, чтобы заполнить данные доставки. Ваш номер уже привязан.", 
            reply_markup=keyboard
        )
    else:
        send_message(chat_id, "⚠️ У вас нет активного заказа для подтверждения.")

def handle_check_submission(conn, update):
    """Обрабатывает получение чека, отправляет полные данные администратору и включает Polling."""
    try:
        message = update['message']
        chat_id = message['chat']['id']
        user_tg_id = message['from']['id']
        
        order_data = get_order_by_tg_id(conn, user_tg_id)
        if not order_data or order_data[1] != 'pending_check_submission':
            send_message(chat_id, "⚠️ У вас нет активного заказа, ожидающего чек.")
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
            # Обновляем статус и ID файла
            update_order_status_and_user(conn, order_token, 'awaiting_admin_confirm', check_file_id=file_id)
            
            # Получаем ВСЕ данные для администратора
            full_order_data = get_order_by_token(conn, order_token)
            
            # --- Форматирование данных для админ-сообщения ---
            try:
                # full_order_data: 2:cart_data, 6:full_name, 7:delivery_type, 8:post_index, 9:city, 10:address_line, 12:payment_amount
                raw_cart = json.loads(full_order_data[2])
                items_text = ""
                total_amount = full_order_data[12] if full_order_data[12] is not None else 0 

                if isinstance(raw_cart, list):
                    for item in raw_cart:
                        if isinstance(item, dict):
                            name = item.get('name', 'Товар')
                            size = item.get('size', '-')
                            qty = item.get('quantity', 1)
                            items_text += f"— {name} ({size}) x{qty}\n"
                        else:
                            items_text += f"— {str(item)}\n"
                else:
                    items_text = "Ошибка чтения товаров."

            except Exception as e:
                print(f"Error parsing data: {e}")
                items_text = "Ошибка чтения товаров."
                total_amount = 0

            admin_message = (
                f"🔔 **НОВЫЙ ЗАКАЗ (ОЖИДАЕТ ПОДТВЕРЖДЕНИЯ)**\n"
                f"Токен: `{order_token}`\n"
                f"Сумма: **{total_amount} руб.** ({full_order_data[13] or 'Н/Д'})\n"
                f"--- **ДАННЫЕ ПОКУПАТЕЛЯ** ---\n"
                f"**ФИО:** {full_order_data[6] or 'Н/Д'}\n"
                f"**Телефон:** {full_order_data[3] or 'Н/Д'}\n"
                f"**Email:** {full_order_data[14] or 'Н/Д'}\n"
                f"--- **ДОСТАВКА ({full_order_data[7] or 'Н/Д'})** ---\n"
                f"**Индекс:** {full_order_data[8] or 'Н/Д'}\n"
                f"**Город:** {full_order_data[9] or 'Н/Д'}\n"
                f"**Адрес:** {full_order_data[10] or 'Н/Д'}\n"
                f"**ПВЗ:** {full_order_data[11] or 'Н/Д'}\n"
                f"--- **СОСТАВ ЗАКАЗА** ---\n{items_text}"
            )
            
            keyboard = {
                "inline_keyboard": [
                    [{"text": "✅ Оплата подтверждена", "callback_data": f"CONFIRM_{order_token}"}],
                    [{"text": "❌ Отклонить оплату", "callback_data": f"REJECT_{order_token}"}]
                ]
            }
            
            req_payload = {
                'chat_id': ADMIN_CHAT_ID,
                'caption': admin_message,
                'reply_markup': keyboard,
                'parse_mode': 'Markdown'
            }
            
            # Отправка чека администратору
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
        print(f"!!! КРИТИЧЕСКАЯ ОШИБКА в handle_check_submission: {e}")
        try: send_message(chat_id, f"❌ Ошибка сервера: {e}")
        except: pass

def handle_callback_query(conn, update):
    """Обрабатывает нажатия кнопок CONFIRM/REJECT от администратора."""
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
            new_caption = message.get('caption', '') + f"\n\n--- Статус ---\n"
            
            order_data = get_order_by_token(conn, order_token)
            user_tg_id = order_data[4] if order_data else None

            if action == 'CONFIRM':
                new_caption += f"✅ ОПЛАЧЕНО. Подтвердил: {admin_id}"
                user_msg = f"✅ **Оплата подтверждена!** Ваш заказ **принят** и скоро будет отправлен. Статус на сайте обновлен."
            else:
                new_caption += f"❌ ОТКЛОНЕНО. Отклонил: {admin_id}"
                user_msg = f"❌ **Оплата отклонена.** Пожалуйста, свяжитесь с менеджером: @{TELEGRAM_BOT_USERNAME}. Статус на сайте обновлен."
            
            # Редактируем сообщение для администратора (убираем кнопки)
            send_tg_request('editMessageCaption', {
                'chat_id': message['chat']['id'],
                'message_id': message['message_id'],
                'caption': new_caption,
                'reply_markup': {"inline_keyboard": []},
                'parse_mode': 'Markdown'
            })
            
            # Уведомляем пользователя
            if user_tg_id: 
                send_message(user_tg_id, user_msg)
                
            send_tg_request('answerCallbackQuery', {'callback_query_id': callback_query['id'], 'text': 'Статус обновлен.'})
    except Exception as e:
        print(f"Ошибка callback: {e}")

# --- ГЛАВНЫЙ WSGI ОБРАБОТЧИК ---

def application(environ, start_response):
    """Главная функция для WSGI-сервера (Render)."""
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

            # A. 1. ИНИЦИАЦИЯ ЗАКАЗА И АВТОРИЗАЦИЯ (САЙТ -> БОТ)
            if path == '/init-auth':
                cart_data = data.get('items', data)
                order_token = str(uuid.uuid4()).replace('-', '')[:16] 
                
                # Сохраняем черновик заказа (статус pending_phone_auth - ждет номер)
                save_order_draft(conn, order_token, cart_data)
                
                tg_link = f"https://t.me/{TELEGRAM_BOT_USERNAME}?start={order_token}"
                
                resp = json.dumps({'success': True, 'telegram_auth_url': tg_link}).encode('utf-8')
                start_response('200 OK', CORS_HEADERS + [('Content-Type', 'application/json')])
                return [resp]

            # B. 2. ФИНАЛЬНАЯ ОТПРАВКА ДАННЫХ ДОСТАВКИ (САЙТ -> СЕРВЕР)
            if path == '/submit-full-order':
                if update_order_full_details(conn, data):
                    # Ссылка для перенаправления в Telegram для отправки чека
                    order_token = data.get('order_token')
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
                
            # C. 3. TELEGRAM WEBHOOK
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
            path = environ.get('PATH_INFO', '/')
            
            # 1. ОБРАБОТЧИК АВТОРИЗАЦИИ TELEGRAM (САЙТ)
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
                    return ["<h1>Ошибка авторизации Telegram.</h1>".encode('utf-8')]
            
            # 2. ПОЛЛИНГ СТАТУСА ЗАКАЗА (САЙТ)
            if path.startswith('/status/'):
                order_token = path.split('/')[-1]
                order_data = get_order_by_token(conn, order_token)
                
                if order_data:
                    status = order_data[1] # order_data[1] - это status
                    resp = json.dumps({'status': status}).encode('utf-8')
                    start_response('200 OK', CORS_HEADERS + [('Content-Type', 'application/json')])
                    return [resp]
                else:
                    start_response('404 Not Found', [('Content-type', 'application/json')])
                    return [json.dumps({'error': 'Order not found'}).encode('utf-8')]
            
            # 3. DEFAULT GET RESPONSE
            start_response('200 OK', [('Content-type', 'text/plain')])
            return [b"OopsServer Running"]
        
    except Exception as e:
        print(f"CRITICAL: {e}")
        start_response('500 Error', [('Content-type', 'application/json')])
        return [json.dumps({'error': str(e)}).encode('utf-8')]
    finally:
        if conn: conn.close()
