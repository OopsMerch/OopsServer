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

# --- CONFIGURATION (Без изменений) ---
DATABASE_URL = os.environ.get('DATABASE_URL')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
ADMIN_CHAT_ID = os.environ.get('ADMIN_CHAT_ID') 
TELEGRAM_BOT_USERNAME = os.environ.get('TELEGRAM_BOT_USERNAME', 'oopsmerchbot') # Убедитесь, что это верное имя бота
SITE_BASE_URL = os.environ.get('SITE_BASE_URL', 'https://oops-merch.ru') 

ORDERS_TABLE_NAME = 'orders'
TG_API_BASE = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/'
CORS_HEADERS = [
    ('Access-Control-Allow-Origin', '*'), 
    ('Access-Control-Allow-Methods', 'POST, GET, OPTIONS'), 
    ('Access-Control-Allow-Headers', 'Content-Type')
]

# --- TELEGRAM API UTILITIES (Без изменений) ---
def send_message(chat_id: int, text: str, reply_markup=None):
    # ... (Обычная реализация send_message)
    pass
# ... (verify_telegram_authorization)

# --- DATABASE STUBS (ОБЯЗАТЕЛЬНО К РЕАЛИЗАЦИИ) ---
# Предполагаемые статусы: 'draft', 'pending_phone', 'pending_full_name', 'pending_address', 'finalizing', 'paid'
def get_db_connection():
    # ... (Логика подключения к базе данных)
    return psycopg2.connect(DATABASE_URL)

def save_order_draft(conn, items: list, total_amount: float) -> str:
    # СОХРАНЯЕТ черновик заказа со статусом 'pending_phone' и возвращает order_token
    order_token = str(uuid.uuid4())[:8] 
    # cursor.execute("INSERT INTO orders (...) VALUES (...) RETURNING token", (order_token, ...))
    return order_token

def update_order_status_and_user(conn, order_token: str, new_status: str, phone_number: str = None, full_name: str = None, address: str = None) -> bool:
    # ОБНОВЛЯЕТ статус заказа и добавляет данные пользователя по токену
    # cursor.execute("UPDATE orders SET status=%s, phone=%s, full_name=%s, address=%s WHERE token=%s", (new_status, ...))
    return True

def get_order_by_token(conn, order_token: str):
    # ВОЗВРАЩАЕТ order_data
    # cursor.execute("SELECT * FROM orders WHERE token=%s", (order_token,))
    return None 

def get_user_state(conn, tg_id: int):
    # ВОЗВРАЩАЕТ статус текущего заказа пользователя или None
    # cursor.execute("SELECT order_token, status FROM orders WHERE tg_id=%s AND status NOT IN ('paid', 'cancelled')", (tg_id,))
    return None 

# --- НОВЫЕ/ИЗМЕНЕННЫЕ ХЕНДЛЕРЫ ДЛЯ ЛОГИКИ ТЕЛЕГРАМ ---

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

# 2. ОБРАБОТКА СООБЩЕНИЙ В БОТЕ (Включая FIO и Address)
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
            if re.match(r'^[А-ЯЁа-яё\s]{5,}$', text) and len(text.split()) >= 2:
                # Предполагаемая проверка: 2+ слова, только кириллица/пробелы, мин. длина
                update_order_status_and_user(conn, order_token, 'pending_address', full_name=text)
                
                # НОВОЕ: Запрос адреса
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
                
                # НОВОЕ: Переход к финализации/оплате
                send_message(
                    chat_id, 
                    "✅ Адрес получен. Ваш заказ передан менеджеру для расчета доставки. \n\n"
                    "Для уточнения или ускорения оформления, пожалуйста, напишите: **@oopssupport**"
                )
                # + Отправить уведомление админу (send_admin_notification)
                # + Предложить кнопку оплаты (если она есть)
            else:
                send_message(chat_id, "⚠️ Пожалуйста, введите полный адрес для доставки.")
            return

    # ... (Остальная логика, включая обработку команды /start с токеном)

# 3. ОБРАБОТКА ПОДТВЕРЖДЕНИЯ НОМЕРА (КНОПКА)
def handle_contact_share(conn, update):
    message = update.get('message', {})
    chat_id = message['chat']['id']
    contact = message.get('contact', {})
    tg_id = message['from']['id']

    order_state = get_user_state(conn, tg_id) 

    if order_state and order_state[1] == 'pending_phone':
        order_token = order_state[0]
        phone_number = contact.get('phone_number')

        if phone_number:
            # Обновляем статус на pending_full_name
            update_order_status_and_user(conn, order_token, 'pending_full_name', phone_number=phone_number)
            
            # НОВОЕ: Переход к запросу ФИО
            send_message(
                chat_id, 
                f"✅ **Номер +{phone_number} подтвержден!** Прекрасно, для продолжения введите свое ФИО (например, *Иванов Иван Иванович*)."
            )
        else:
            send_message(chat_id, "⚠️ Не удалось получить ваш номер телефона.")
    else:
        send_message(chat_id, "⚠️ У вас нет активного заказа для подтверждения номера. Начните заказ на сайте.")

# 4. ОБНОВЛЕННЫЙ WSGI/APP ENTRY POINT
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
        
        # 1. НОВЫЙ ХЕНДЛЕР ИНИЦИАЦИИ ЗАКАЗА
        if path == '/init-auth' and request_method == 'POST':
            return handle_init_auth(environ, start_response, conn)
            
        # 2. УДАЛЕНО: Хендлер /submit-full-order
        
        # 3. ОБРАБОТКА ВЕБХУКА ТЕЛЕГРАМ
        if path == f'/webhook/{TELEGRAM_BOT_TOKEN}' and request_method == 'POST':
            # ... (Код обработки вебхука Telegram)
            # Внутри этой функции должен быть вызов handle_text_message и handle_contact_share
            
        # 4. УДАЛЕНО: ORDER STATUS POLLING (больше не нужно)
        # if path.startswith('/status/'):
        # ... (логика удалена)

        start_response('404 Not Found', [('Content-type', 'text/plain')])
        return [b'Not Found']
        
    except Exception as e:
        # ... (Обработка ошибок)
        pass
    finally:
        if conn: conn.close()
