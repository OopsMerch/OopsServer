import os
import json
import uuid
import re
import requests
import psycopg2
from psycopg2.extras import RealDictCursor

# --- CONFIGURATION ---
# Получаем переменные окружения
DATABASE_URL = os.environ.get('DATABASE_URL')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
ADMIN_CHAT_ID = os.environ.get('ADMIN_CHAT_ID') 
TELEGRAM_BOT_USERNAME = os.environ.get('TELEGRAM_BOT_USERNAME', 'oopsmerchbot')
SITE_BASE_URL = os.environ.get('SITE_BASE_URL', 'https://oops-merch.ru') 

TG_API_BASE = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/'

# CORS заголовки для ответов сайту
CORS_HEADERS = [
    ('Access-Control-Allow-Origin', '*'), 
    ('Access-Control-Allow-Methods', 'POST, GET, OPTIONS'), 
    ('Access-Control-Allow-Headers', 'Content-Type')
]

# --- DATABASE UTILS ---

def get_db_connection():
    """Создает подключение к PostgreSQL."""
    if not DATABASE_URL:
        raise Exception("DATABASE_URL не установлена в Environment Variables.")
    
    # Исправление для Render: psycopg2 требует 'postgresql://', а Render иногда дает 'postgres://'
    conn_url = DATABASE_URL.replace("postgres://", "postgresql://")
    return psycopg2.connect(conn_url, cursor_factory=RealDictCursor)

def init_db():
    """Создает таблицу orders, если она не существует."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_token VARCHAR(50) PRIMARY KEY,
                    status VARCHAR(50) NOT NULL,
                    cart_data JSONB,
                    total_amount NUMERIC(10, 2),
                    
                    user_tg_id BIGINT,
                    phone_number VARCHAR(30),
                    full_name TEXT,
                    address TEXT,
                    
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
        conn.commit()
        print("Database initialized/checked successfully.")
    except Exception as e:
        print(f"DB Init Error: {e}")
    finally:
        conn.close()

# Запускаем инициализацию таблицы при старте скрипта (один раз)
init_db()


# --- TELEGRAM API UTILITIES ---

def send_message(chat_id, text, reply_markup=None):
    """Отправляет сообщение в Telegram."""
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'Markdown'
    }
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)
        
    try:
        requests.post(TG_API_BASE + 'sendMessage', json=payload)
    except Exception as e:
        print(f"Telegram Send Error: {e}")

def send_admin_notification(order_token, text):
    """Отправляет уведомление админу."""
    if ADMIN_CHAT_ID:
        msg = f"🔔 **Заказ #{order_token}**\n{text}"
        send_message(ADMIN_CHAT_ID, msg)


# --- REAL DATABASE LOGIC ---

def save_order_draft(conn, items, total_amount):
    """Сохраняет новый заказ из корзины."""
    order_token = uuid.uuid4().hex[:8] # Генерируем короткий токен
    items_json = json.dumps(items)
    
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO orders (order_token, status, cart_data, total_amount, created_at, updated_at)
            VALUES (%s, 'pending_phone', %s, %s, NOW(), NOW())
        """, (order_token, items_json, total_amount))
    conn.commit()
    return order_token

def get_order_by_token(conn, order_token):
    """Получает данные заказа по токену."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM orders WHERE order_token = %s", (order_token,))
        return cur.fetchone()

def update_order_status_and_user(conn, order_token, new_status, **kwargs):
    """
    Обновляет статус заказа и любые переданные поля (tg_id, phone, name, address).
    Использует **kwargs для гибкости.
    """
    fields = ["status = %s", "updated_at = NOW()"]
    params = [new_status]
    
    if 'tg_id' in kwargs:
        fields.append("user_tg_id = %s")
        params.append(kwargs['tg_id'])
        
    if 'phone_number' in kwargs:
        fields.append("phone_number = %s")
        params.append(kwargs['phone_number'])
        
    if 'full_name' in kwargs:
        fields.append("full_name = %s")
        params.append(kwargs['full_name'])
        
    if 'address' in kwargs:
        fields.append("address = %s")
        params.append(kwargs['address'])
        
    # Добавляем токен в конец параметров для WHERE
    params.append(order_token)
    
    sql = f"UPDATE orders SET {', '.join(fields)} WHERE order_token = %s"
    
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
    conn.commit()
    return True

def get_user_state(conn, tg_id):
    """Находит активный заказ пользователя (не завершенный)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT order_token, status 
            FROM orders 
            WHERE user_tg_id = %s 
              AND status NOT IN ('completed', 'cancelled', 'finalizing')
            ORDER BY created_at DESC 
            LIMIT 1
        """, (tg_id,))
        row = cur.fetchone()
        
    if row:
        return row['order_token'], row['status']
    return None


# --- HANDLERS (ЛОГИКА) ---

def handle_init_auth(environ, start_response, conn):
    """Обработка запроса с сайта: создание заказа."""
    try:
        # Чтение тела запроса
        content_length = int(environ.get('CONTENT_LENGTH', 0))
        body = environ['wsgi.input'].read(content_length)
        data = json.loads(body)

        # Сохранение в БД
        items = data.get('items', [])
        total = data.get('total_amount', 0)
        token = save_order_draft(conn, items, total)
        
        # Генерация ссылки
        bot_url = f"https://t.me/{TELEGRAM_BOT_USERNAME}?start={token}"

        # Ответ
        resp = json.dumps({'telegram_bot_url': bot_url}).encode('utf-8')
        start_response('200 OK', CORS_HEADERS + [('Content-Type', 'application/json')])
        return [resp]
        
    except Exception as e:
        print(f"Init Auth Error: {e}")
        start_response('500 Error', CORS_HEADERS + [('Content-Type', 'application/json')])
        return [json.dumps({'error': str(e)}).encode('utf-8')]

def handle_start_command(conn, chat_id, tg_id, text):
    """Обработка команды /start <token>."""
    # Ищем токен в сообщении (/start abc12345)
    match = re.search(r'/start\s+([a-fA-F0-9]+)', text)
    
    if match:
        token = match.group(1)
        order = get_order_by_token(conn, token)
        
        if order and order['status'] == 'pending_phone':
            # Привязываем юзера к заказу
            update_order_status_and_user(conn, token, 'pending_phone', tg_id=tg_id)
            
            kb = {"keyboard": [[{"text": "📱 Поделиться номером", "request_contact": True}]], "resize_keyboard": True, "one_time_keyboard": True}
            send_message(chat_id, "👋 Привет! Мы нашли ваш заказ.\nДля продолжения, пожалуйста, подтвердите номер телефона:", kb)
        else:
            send_message(chat_id, "⚠️ Заказ не найден или уже оформлен.")
    else:
        send_message(chat_id, "🛒 Чтобы оформить заказ, начните с корзины на сайте.")

def handle_contact(conn, chat_id, tg_id, contact):
    """Обработка получения контакта."""
    if contact.get('user_id') != tg_id:
        send_message(chat_id, "⛔️ Пожалуйста, отправьте СВОЙ контакт.")
        return

    state = get_user_state(conn, tg_id)
    if state and state[1] == 'pending_phone':
        token = state[0]
        phone = contact.get('phone_number')
        
        # Обновляем БД -> переходим к имени
        update_order_status_and_user(conn, token, 'pending_full_name', phone_number=phone)
        
        send_message(chat_id, "✅ Номер принят!\n✍️ Теперь напишите ваше **ФИО** (например: Иванов Иван Иванович).", {"remove_keyboard": True})
    else:
        send_message(chat_id, "⚠️ Не найден активный заказ на этапе ввода номера.")

def handle_text(conn, chat_id, tg_id, text):
    """Обработка текстовых сообщений (ФИО, Адрес)."""
    if text.startswith('/start'):
        handle_start_command(conn, chat_id, tg_id, text)
        return

    state = get_user_state(conn, tg_id)
    if not state:
        send_message(chat_id, "🤷‍♂️ Нет активного заказа. Перейдите на сайт.")
        return

    token, status = state

    # Логика ФИО
    if status == 'pending_full_name':
        if len(text.split()) < 2 or len(text) < 5:
            send_message(chat_id, "⚠️ Введите корректное ФИО (минимум 2 слова).")
            return
        
        # Обновляем БД -> переходим к адресу
        update_order_status_and_user(conn, token, 'pending_address', full_name=text)
        send_message(chat_id, "👍 ФИО сохранено.\n🚚 Введите **полный адрес доставки** (Город, улица, дом, кв).")

    # Логика АДРЕСА
    elif status == 'pending_address':
        if len(text) < 10:
            send_message(chat_id, "⚠️ Адрес слишком короткий. Напишите подробнее.")
            return
            
        # Обновляем БД -> Финал
        update_order_status_and_user(conn, token, 'finalizing', address=text)
        
        final_msg = (
            "✅ **Данные приняты!**\n\n"
            "Менеджер скоро проверит стоимость доставки и свяжется с вами для оплаты.\n"
            "Если нужно что-то уточнить, пишите: @oopssupport"
        )
        send_message(chat_id, final_msg)
        send_admin_notification(token, f"Новый заказ завершен ботом!\nКлиент: {tg_id}\nАдрес: {text}")


# --- WSGI ENTRY POINT ---

def application(environ, start_response):
    """Главная точка входа для Gunicorn."""
    conn = None
    try:
        path = environ.get('PATH_INFO', '/')
        method = environ.get('REQUEST_METHOD', 'GET')
        
        # OPTIONS (CORS preflight)
        if method == 'OPTIONS':
            start_response('200 OK', CORS_HEADERS)
            return [b'']
        
        # Подключаемся к БД для обработки запроса
        conn = get_db_connection()

        # 1. Инициация заказа с сайта
        if path == '/init-auth' and method == 'POST':
            return handle_init_auth(environ, start_response, conn)

        # 2. Telegram Webhook
        if path == f'/webhook/{TELEGRAM_BOT_TOKEN}' and method == 'POST':
            try:
                length = int(environ.get('CONTENT_LENGTH', 0))
                data = json.loads(environ['wsgi.input'].read(length))
                
                if 'message' in data:
                    msg = data['message']
                    chat_id = msg['chat']['id']
                    tg_id = msg['from']['id']
                    
                    if 'contact' in msg:
                        handle_contact(conn, chat_id, tg_id, msg['contact'])
                    elif 'text' in msg:
                        handle_text(conn, chat_id, tg_id, msg['text'])
                        
            except Exception as e:
                print(f"Webhook Logic Error: {e}")
            
            # Всегда 200 OK для Телеграма
            start_response('200 OK', [('Content-Type', 'text/plain')])
            return [b'OK']

        # 404
        start_response('404 Not Found', [('Content-Type', 'text/plain')])
        return [b'Not Found']

    except Exception as e:
        print(f"Critical App Error: {e}")
        start_response('500 Internal Server Error', [('Content-Type', 'text/plain')])
        return [str(e).encode('utf-8')]
        
    finally:
        # Закрываем соединение с БД
        if conn:
            conn.close()
й
