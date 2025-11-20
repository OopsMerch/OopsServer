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

# --- CONFIGURATION (ВАШИ НАСТРОЙКИ) ---
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

# --- TELEGRAM UTILITY FUNCTIONS ---

def send_message(chat_id, text, reply_markup=None):
    """Отправка сообщения в Telegram."""
    url = TG_API_BASE + 'sendMessage'
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'Markdown',
    }
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)
    
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Error sending TG message: {e}")
        
# --- DB UTILITY STUBS (ДОЛЖНЫ БЫТЬ РЕАЛИЗОВАНЫ С DB) ---

def get_db_conn():
    return psycopg2.connect(DATABASE_URL)

def get_order_by_token(conn, order_token):
    # token, status, cart_data, phone_number, full_name, address_line
    # Ваш код для получения заказа по токену
    # ...
    # ЗАГЛУШКА
    return (order_token, 'DRAFT', '{}', None, None, None) # Пример возвращаемых данных: 6 полей

def insert_draft_order(conn, token, cart_json):
    # Ваш код для вставки нового заказа в статусе DRAFT
    pass

def update_order_status_and_user(conn, token, status, tg_id, phone):
    # Ваш код для обновления статуса, tg_id и номера телефона
    return True # Успех

def get_order_by_tg_id_and_status(conn, tg_id, status):
    # Ваш код для поиска заказа по tg_id и статусу.
    # Должен возвращать: token, status, cart_data, phone_number, full_name, address_line
    # ЗАГЛУШКА:
    return None

def update_order_field(conn, token, field_name, value):
    # Ваш код для обновления одного поля (e.g., full_name, address_line)
    pass

def update_order_status(conn, token, new_status):
    # Ваш код для обновления статуса
    pass

def finalize_order_and_notify_admin(conn, token, cart_data, full_name, phone_number, address_line, user_tg_id):
    # Ваш код для финализации (установка даты, номера заказа) и отправки уведомления администратору
    send_message(
        ADMIN_CHAT_ID, 
        f"🚨 **НОВЫЙ ЗАКАЗ ИЗ БОТА!** 🚨\n\nID: {token}\nФИО: {full_name}\nТелефон: {phone_number}\nАдрес: {address_line}\n\nСостав заказа:\n{cart_data}",
    )

# --- TELEGRAM AUTH FUNCTION (UNCHANGED) ---
def verify_telegram_authorization(auth_data: Dict[str, str]) -> bool:
    # ... (Оставить без изменений) ...
    pass
    

# --- CORE LOGIC ---

# 1. ИЗМЕНЕНИЕ: handle_init_auth (Инициация авторизации с сайта)
def handle_init_auth(conn, env, start_response):
    """Инициация авторизации. УБИРАЕМ обратную ссылку на сайт."""
    try:
        # ... (Код получения данных корзины остается) ...
        try:
            request_body_size = int(env.get('CONTENT_LENGTH', 0))
        except (ValueError):
            request_body_size = 0
            
        request_body = env['wsgi.input'].read(request_body_size)
        data = json.loads(request_body)
        cart_data = data.get('cart')
        
        if not cart_data:
             raise ValueError("Cart data is missing.")

        # Создание нового DRAFT заказа
        order_token = str(uuid.uuid4())
        insert_draft_order(conn, order_token, json.dumps(cart_data))
        
        # СТАЛО: Только ссылка на бота с токеном
        tg_auth_url = f"https://t.me/{TELEGRAM_BOT_USERNAME}?startauth={order_token}"

        resp = json.dumps({'success': True, 'token': order_token, 'telegram_url': tg_auth_url}).encode('utf-8')
        start_response('200 OK', CORS_HEADERS + [('Content-Type', 'application/json')])
        return [resp]
    except Exception as e:
        print(f"Error in handle_init_auth: {e}")
        start_response('400 Bad Request', CORS_HEADERS + [('Content-type', 'application/json')])
        return [json.dumps({'error': str(e)}).encode('utf-8')]

# 2. ИЗМЕНЕНИЕ: handle_auth_callback (Коллбэк после авторизации)
def handle_auth_callback(conn, env, start_response):
    """Коллбэк после авторизации. Изменяем статус и отправляем первый вопрос."""
    try:
        qs = env.get('QUERY_STRING', '')
        auth_data_list = parse_qsl(qs)
        auth_data = {k: v for k, v in auth_data_list}
        
        # ... (Проверка auth_data остается) ...
        if not verify_telegram_authorization(auth_data):
             start_response('401 Unauthorized', [('Content-Type', 'text/html')])
             return ["<h1>Telegram authorization failed. Hash mismatch.</h1>".encode('utf-8')]
            
        user_tg_id = str(auth_data['id']) # Важно: хранить как строку
        order_token = auth_data.get('state') 
        phone_number = auth_data.get('phone_number', 'Номер не указан')
        chat_id = user_tg_id 
        
        order_data = get_order_by_token(conn, order_token)
        
        # --- ИЗМЕНЕНИЕ ЛОГИКИ ПОСЛЕ УСПЕШНОГО ПОДТВЕРЖДЕНИЯ НОМЕРА ---
        
        if order_data and order_data[1] == 'DRAFT': 
            
            # Обновляем статус заказа на 'AWAITING_FULL_NAME', сохраняем TG ID и phone
            if update_order_status_and_user(conn, order_token, 'AWAITING_FULL_NAME', user_tg_id, phone_number): 
                
                # Отправляем первое сообщение в новой цепочке
                send_message(
                    chat_id, 
                    "✅ **Номер подтвержден!** \n\nДля продолжения оформления заказа, пожалуйста, введите ваше **ФИО** (Фамилия Имя Отчество).",
                    reply_markup=None 
                )

                # Возвращаем на сайт HTML-страницу с сообщением об успехе (без перенаправления)
                success_html = """
                    <!DOCTYPE html>
                    <html>
                    <head><title>Успешно</title></head>
                    <body>
                        <script>
                            // Закрыть окно (если открывалось как всплывающее)
                            window.onload = function() {
                                document.body.innerHTML = '<h1>Номер подтвержден!</h1><p>Пожалуйста, вернитесь в Telegram-бот для завершения заказа.</p>';
                                setTimeout(function() {
                                    if(window.opener) { window.close(); }
                                }, 3000);
                            }
                        </script>
                    </body>
                    </html>
                """
                start_response('200 OK', [('Content-Type', 'text/html')])
                return [success_html.encode('utf-8')]
            else:
                raise Exception("Failed to update order status and user data.")
            
        else:
             # Неверный статус или токен
             start_response('400 Bad Request', [('Content-Type', 'text/html')])
             return ["<h1>Order not found or already processed.</h1>".encode('utf-8')]

    except Exception as e:
        print(f"Error in handle_auth_callback: {e}")
        start_response('500 Error', [('Content-type', 'application/json')])
        return [json.dumps({'error': str(e)}).encode('utf-8')]


# 3. ДОБАВЛЕНИЕ: handle_message (Логика состояний бота)
def handle_message(conn, update: Dict[str, Any]):
    """Обрабатывает входящие текстовые сообщения для сбора ФИО и Адреса."""
    try:
        message = update.get('message', {})
        text = message.get('text', '').strip()
        
        if not text or 'chat' not in message or 'from' not in message:
            return
            
        chat_id = message['chat']['id']
        user_tg_id = str(message['from']['id']) # Важно: telegram ID как строка
        
        # --- 1. Обработка ввода ФИО (AWAITING_FULL_NAME) ---
        order_data = get_order_by_tg_id_and_status(conn, user_tg_id, 'AWAITING_FULL_NAME')
        if order_data:
            order_token, status, cart_data, phone_number, _, _ = order_data
            
            # Проверка ФИО: минимум два слова и длиннее 5 символов
            if len(text.split()) >= 2 and len(text) > 5: 
                update_order_field(conn, order_token, 'full_name', text)
                update_order_status(conn, order_token, 'AWAITING_ADDRESS')
                send_message(
                    chat_id, 
                    "👍 **ФИО принято!**\n\nТеперь, пожалуйста, введите **полный адрес доставки** (город, улица, дом, квартира).",
                )
            else:
                send_message(chat_id, "⚠️ **Некорректный ввод ФИО.** Пожалуйста, введите ваше полное ФИО (Фамилия Имя Отчество).")
            return
        
        # --- 2. Обработка ввода Адреса (AWAITING_ADDRESS) ---
        order_data = get_order_by_tg_id_and_status(conn, user_tg_id, 'AWAITING_ADDRESS')
        if order_data:
            # order_data: token, status, cart_data, phone_number, full_name, address_line (здесь address_line будет None)
            order_token, status, cart_data, phone_number, full_name, _ = order_data
            
            # Проверка: адрес должен быть достаточно информативным
            if len(text) > 10: 
                update_order_field(conn, order_token, 'address_line', text)
                
                # Финализация заказа и уведомление администратора
                finalize_order_and_notify_admin(conn, order_token, cart_data, full_name, phone_number, text, user_tg_id)

                # Уведомление Пользователя
                send_message(
                    chat_id, 
                    "🎉 **Заказ успешно оформлен!** \n\nВся информация о доставке и оплате будет уточнена менеджером. \n\nДля любых вопросов по заказу, пожалуйста, напишите в службу поддержки: **@oopssupport**\n\nСпасибо за ваш заказ! Ваш менеджер: **@oopssupport**",
                )
                # Устанавливаем финальный статус
                update_order_status(conn, order_token, 'PLACED_TG')
            else:
                send_message(chat_id, "⚠️ **Адрес слишком короткий.** Пожалуйста, введите полный адрес доставки (город, улица, дом, квартира).")
            return
        
        # --- 3. Обработка стандартных команд ---
        if text.lower() == '/start':
             send_message(
                chat_id, 
                "Здравствуйте! Чтобы начать оформление заказа, перейдите в корзину на нашем сайте и нажмите кнопку **'Перейти в Telegram-бот для оформления'**.",
            )
             return
             
    except Exception as e:
        print(f"Error in handle_message: {e}")
        # В случае ошибки, отправляем пользователю общее сообщение
        send_message(chat_id, "Произошла внутренняя ошибка при обработке вашего запроса. Попробуйте начать заново или обратитесь в @oopssupport.")


# 4. ИЗМЕНЕНИЕ: application (Добавление маршрута для Webhook)
def application(env, start_response):
    # ... (Оставить без изменений до секции с PATH_INFO)
    try:
        conn = get_db_conn()
        path = env.get('PATH_INFO', '')
        method = env.get('REQUEST_METHOD', '')

        # --- TELEGRAM WEBHOOK HANDLER (ДОБАВЛЕНИЕ ЭТОГО БЛОКА) ---
        if path == f'/webhook/tg/{TELEGRAM_BOT_TOKEN}' and method == 'POST':
            try:
                request_body_size = int(env.get('CONTENT_LENGTH', 0))
                request_body = env['wsgi.input'].read(request_body_size)
                update = json.loads(request_body)
                handle_message(conn, update)
                start_response('200 OK', [('Content-type', 'application/json')])
                return [b'{}']
            except Exception as e:
                print(f"Webhook Error: {e}")
                start_response('500 Error', [('Content-type', 'application/json')])
                return [json.dumps({'error': 'Webhook processing error'}).encode('utf-8')]
        # -------------------------------------------------------------
        
        # 1. API - INITIATE AUTH (Site request)
        if path == '/init-auth' and method == 'POST':
            return handle_init_auth(conn, env, start_response)

        # 2. API - TELEGRAM AUTH CALLBACK (Telegram redirects here)
        if path == '/auth-callback' and method == 'GET':
            return handle_auth_callback(conn, env, start_response)
            
        # 3. API - SUBMIT FULL ORDER (УДАЛЯЕМ ЭТОТ МАРШРУТ, Т.К. ВСЕ В БОТЕ)
        # if path == '/submit-full-order' and method == 'POST':
        #    return handle_submit_full_order(conn, env, start_response) # <-- УДАЛИТЬ

        # 4. API - CALCULATE DELIVERY (УДАЛЯЕМ ЭТОТ МАРШРУТ)
        # if path == '/calculate-delivery' and method == 'POST':
        #    return handle_calculate_delivery(conn, env, start_response) # <-- УДАЛИТЬ

        # 5. ORDER STATUS POLLING (SITE) - ОСТАВЛЯЕМ ДЛЯ ПРОВЕРКИ СТАТУСА
        if path.startswith('/status/'):
             order_token = path.split('/')[-1]
             # ... (Код обработки статуса остается) ...
             # ...
             
        # ... (Остальной код остается) ...

    except Exception as e:
        # ...
        pass
    finally:
        if conn: conn.close()
