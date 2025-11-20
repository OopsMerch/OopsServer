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
# Используйте переменные окружения, установленные на Render
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

# --- TELEGRAM API UTILITIES (Примерная реализация) ---
def send_message(chat_id: int, text: str, reply_markup=None, parse_mode='Markdown'):
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': parse_mode
    }
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)
        
    try:
        requests.post(TG_API_BASE + 'sendMessage', data=payload)
    except Exception as e:
        print(f"Error sending Telegram message: {e}")

# Функции verify_telegram_authorization и send_admin_notification (оставлены как заглушки)
def verify_telegram_authorization(auth_data: Dict[str, str]) -> bool:
    # ... (Логика проверки hash)
    return True

def send_admin_notification(order_token: str, subject: str):
    if ADMIN_CHAT_ID:
        message = f"**Новое событие по заказу {order_token}:**\n{subject}"
        send_message(ADMIN_CHAT_ID, message)

# --- DATABASE STUBS (ОБЯЗАТЕЛЬНО К РЕАЛИЗАЦИИ) ---
def get_db_connection():
    """Возвращает соединение с базой данных."""
    if not DATABASE_URL:
        raise Exception("DATABASE_URL environment variable is not set.")
    return psycopg2.connect(DATABASE_URL)

def save_order_draft(conn, items: list, total_amount: float) -> str:
    """СОХРАНЯЕТ черновик заказа со статусом 'pending_phone' и возвращает order_token."""
    order_token = str(uuid.uuid4())[:8] 
    items_json = json.dumps(items)
    status = 'pending_phone'
    
    # !!! Здесь должна быть РЕАЛЬНАЯ ЛОГИКА вставки в базу данных !!!
    print(f"STUB: Saving order draft {order_token} with status {status}")
    # Пример SQL (нужно адаптировать):
    # with conn.cursor() as cursor:
    #     cursor.execute(
    #         f"INSERT INTO {ORDERS_TABLE_NAME} (token, items_json, total_amount, status) VALUES (%s, %s, %s, %s)",
    #         (order_token, items_json, total_amount, status)
    #     )
    # conn.commit()
    
    return order_token

def update_order_status_and_user(conn, order_token: str, new_status: str, tg_id: int = None, phone_number: str = None, full_name: str = None, address: str = None) -> bool:
    """ОБНОВЛЯЕТ статус заказа и добавляет данные пользователя по токену."""
    
    # !!! Здесь должна быть РЕАЛЬНАЯ ЛОГИКА обновления в базе данных !!!
    print(f"STUB: Updating order {order_token} to status {new_status}. Data: TG={tg_id}, Phone={phone_number}, Name={full_name}, Address={address}")
    # Пример SQL (нужно адаптировать):
    # updates = []
    # params = []
    # if tg_id: updates.append("tg_id=%s"); params.append(tg_id)
    # if phone_number: updates.append("phone_number=%s"); params.append(phone_number)
    # if full_name: updates.append("full_name=%s"); params.append(full_name)
    # if address: updates.append("address=%s"); params.append(address)
    # updates.append("status=%s"); params.append(new_status)
    # params.append(order_token)
    
    # if updates:
    #     with conn.cursor() as cursor:
    #         cursor.execute(f"UPDATE {ORDERS_TABLE_NAME} SET {', '.join(updates)} WHERE token=%s", params)
    #     conn.commit()
    
    return True

def get_order_by_token(conn, order_token: str):
    """ВОЗВРАЩАЕТ order_data по токену."""
    # !!! Здесь должна быть РЕАЛЬНАЯ ЛОГИКА получения данных из БД !!!
    print(f"STUB: Fetching order data for token {order_token}")
    # Пример: return (token, status, tg_id, items_json, ...)
    return None 

def get_user_state(conn, tg_id: int):
    """ВОЗВРАЩАЕТ order_token и status текущего активного заказа пользователя или None."""
    # !!! Здесь должна быть РЕАЛЬНАЯ ЛОГИКА получения состояния пользователя !!!
    # Пример: return ('1a2b3c4d', 'pending_phone')
    return None 

# --- ХЕНДЛЕРЫ ЛОГИКИ ТЕЛЕГРАМ ---

# 1. ОБРАБОТКА ИНИЦИАЦИИ ЗАКАЗА С САЙТА (POST /init-auth)
def handle_init_auth(environ, start_response, conn):
    try:
        content_length = int(environ.get('CONTENT_LENGTH', 0))
        request_body = environ['wsgi.input'].read(content_length)
        data = json.loads(request_body)

        items = data.get('items', [])
        total_amount = data.get('total_amount', 0)
        
        # 1. Сохраняем черновик заказа
        order_token = save_order_draft(conn, items, total_amount)
        
        # 2. Генерируем прямую deep-link ссылку
        telegram_bot_url = f"https://t.me/{TELEGRAM_BOT_USERNAME}?start={order_token}"

        response_data = {'order_token': order_token, 'telegram_bot_url': telegram_bot_url}
        resp = json.dumps(response_data).encode('utf-8')
        
        start_response('200 OK', CORS_HEADERS + [('Content-Type', 'application/json')])
        return [resp]
        
    except Exception as e:
        print(f"Error in handle_init_auth: {e}")
        start_response('500 Internal Server Error', CORS_HEADERS + [('Content-Type', 'application/json')])
        return [json.dumps({'error': 'Failed to initialize order.'}).encode('utf-8')]

# 2. ОБРАБОТКА КОМАНДЫ /start
def handle_start_command(conn, update):
    message = update.get('message', {})
    chat_id = message['chat']['id']
    tg_id = message['from']['id']
    
    # Парсим deep-link токен
    text = message.get('text', '')
    match = re.match(r'/start\s+([a-fA-F0-9]{8})', text) # Ищем токен из 8 символов

    if match:
        order_token = match.group(1)
        order_data = get_order_by_token(conn, order_token) # Получаем данные заказа

        if order_data and order_data.get('status') == 'pending_phone':
            # Привязываем Telegram ID к заказу
            update_order_status_and_user(conn, order_token, 'pending_phone', tg_id=tg_id)
            
            # Клавиатура для запроса номера
            reply_markup = {
                "keyboard": [[{"text": "Поделиться контактом", "request_contact": True}]],
                "one_time_keyboard": True,
                "resize_keyboard": True
            }
            send_message(
                chat_id, 
                "Привет! Вы начали оформление заказа. Пожалуйста, подтвердите свой номер телефона, нажав на кнопку ниже:",
                reply_markup=reply_markup
            )
            return

    # Если токена нет или заказ не найден/неактивен
    send_message(
        chat_id, 
        "Добро пожаловать в Oops Merch! Чтобы начать заказ, перейдите в корзину на нашем сайте и нажмите 'Оформить в Telegram'."
    )
    
# 3. ОБРАБОТКА СООБЩЕНИЙ В БОТЕ (ФИО и Адрес)
def handle_text_message(conn, update):
    message = update.get('message', {})
    chat_id = message['chat']['id']
    text = message.get('text', '').strip()
    tg_id = message['from']['id']

    # Проверяем состояние пользователя по его активному заказу
    order_state = get_user_state(conn, tg_id) 

    if order_state:
        order_token, status = order_state
        
        # Шаг 2: Ожидание ФИО
        if status == 'pending_full_name':
            if re.match(r'^[А-ЯЁа-яё\s]{5,}$', text, re.IGNORECASE) and len(text.split()) >= 2:
                # Проверка: 2+ слова, только кириллица/пробелы, мин. длина
                update_order_status_and_user(conn, order_token, 'pending_address', full_name=text)
                
                # Запрос адреса
                send_message(
                    chat_id, 
                    "ФИО сохранено. Введите адрес (улица, дом, квартира). Мы сами найдем ближайший пункт доставки."
                )
            else:
                send_message(chat_id, "⚠️ Пожалуйста, введите корректное ФИО (Иванов Иван Иванович).")
            return
        
        # Шаг 3: Ожидание Адреса
        elif status == 'pending_address':
            if len(text) > 10:
                update_order_status_and_user(conn, order_token, 'finalizing', address=text)
                
                # Переход к финализации/оплате
                send_message(
                    chat_id, 
                    "✅ Адрес получен. Ваш заказ передан менеджеру для расчета доставки. \n\n"
                    "Для уточнения или ускорения оформления, пожалуйста, напишите: **@oopssupport**"
                )
                send_admin_notification(order_token, "Заказ полностью оформлен клиентом через бота. Ожидает расчета доставки.")
                
            else:
                send_message(chat_id, "⚠️ Пожалуйста, введите полный адрес для доставки.")
            return

    # Если это команда /start
    if text.startswith('/start'):
        handle_start_command(conn, update)
    # Если не команда и нет активного заказа
    else:
        send_message(chat_id, "Чтобы оформить заказ, начните с корзины на сайте: [oops-merch.ru](https://oops-merch.ru)")


# 4. ОБРАБОТКА ПОДТВЕРЖДЕНИЯ НОМЕРА (КНОПКА contact)
def handle_contact_share(conn, update):
    message = update.get('message', {})
    chat_id = message['chat']['id']
    contact = message.get('contact', {})
    tg_id = message['from']['id']

    # Проверяем, что контакт принадлежит текущему пользователю и он соответствует ожидаемому шагу
    if contact.get('user_id') != tg_id:
        send_message(chat_id, "⚠️ Пожалуйста, нажмите кнопку 'Поделиться контактом' сами, а не пересылайте чужой контакт.")
        return

    order_state = get_user_state(conn, tg_id) 

    if order_state and order_state[1] == 'pending_phone':
        order_token = order_state[0]
        phone_number = contact.get('phone_number')

        if phone_number:
            # Обновляем статус на pending_full_name
            update_order_status_and_user(conn, order_token, 'pending_full_name', phone_number=phone_number)
            
            # Переход к запросу ФИО
            send_message(
                chat_id, 
                f"✅ **Номер +{phone_number} подтвержден!** Прекрасно, для продолжения введите свое ФИО (например, *Иванов Иван Иванович*)."
            )
        else:
            send_message(chat_id, "⚠️ Не удалось получить ваш номер телефона.")
    else:
        send_message(chat_id, "⚠️ У вас нет активного заказа для подтверждения номера. Начните заказ на сайте.")

# 5. ОБНОВЛЕННЫЙ WSGI/APP ENTRY POINT
def application(environ, start_response):
    conn = None
    try:
        conn = get_db_connection()
        path = environ.get('PATH_INFO', '/')
        request_method = environ.get('REQUEST_METHOD', 'GET')

        # Обработка OPTIONS (CORS)
        if request_method == 'OPTIONS':
            start_response('200 OK', CORS_HEADERS)
            return [b'']
        
        # 1. ХЕНДЛЕР ИНИЦИАЦИИ ЗАКАЗА С САЙТА
        if path == '/init-auth' and request_method == 'POST':
            return handle_init_auth(environ, start_response, conn)
            
        # 2. ОБРАБОТКА ВЕБХУКА ТЕЛЕГРАМ (ИСПРАВЛЕНИЕ IndentationError)
        if path == f'/webhook/{TELEGRAM_BOT_TOKEN}' and request_method == 'POST':
            try:
                content_length = int(environ.get('CONTENT_LENGTH', 0))
                request_body = environ['wsgi.input'].read(content_length)
                update = json.loads(request_body)

                # Диспетчеризация сообщения:
                if 'message' in update:
                    message = update['message']
                    if 'contact' in message:
                        handle_contact_share(conn, update)
                    elif 'text' in message:
                        handle_text_message(conn, update)
                
            except Exception as e:
                # Логирование, но всегда возвращаем 200, чтобы Телеграм не повторял запрос
                print(f"Error handling Telegram webhook: {e}")
            
            # Телеграм должен всегда получать 200 ОК
            start_response('200 OK', [('Content-type', 'text/plain')])
            return [b'OK'] 
            
        # 3. УДАЛЕННЫЕ МАРШРУТЫ (STATUS, CDEK)
        # if path.startswith('/status/'):
        #     ... (логика удалена)

        # 4. 404
        start_response('404 Not Found', [('Content-type', 'text/plain')])
        return [b'Not Found']
        
    except Exception as e:
        print(f"CRITICAL: {e}")
        start_response('500 Error', [('Content-type', 'application/json')])
        return [json.dumps({'error': str(e)}).encode('utf-8')]
    finally:
        if conn: conn.close()
