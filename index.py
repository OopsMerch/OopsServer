import os
import json
import uuid
import psycopg2 
import psycopg2.errors
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

# --- DATABASE FUNCTIONS ---

def create_psql_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is not set.")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn

def init_db(conn):
    try:
        with conn.cursor() as cur:
            # Создаем таблицу, если ее нет
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
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            # Пытаемся добавить колонки, если таблица уже существует (для обратной совместимости)
            columns_to_add = [
                ("total_amount", "NUMERIC(10, 2) NOT NULL DEFAULT 0.00"),
                ("full_name", "VARCHAR(255) DEFAULT NULL"),
                ("address", "TEXT DEFAULT NULL")
            ]
            
            for col_name, col_def in columns_to_add:
                try:
                    cur.execute(f"ALTER TABLE {ORDERS_TABLE_NAME} ADD COLUMN {col_name} {col_def};")
                    print(f"Added column {col_name}")
                except psycopg2.errors.DuplicateColumn:
                    pass # Колонка уже есть
                except Exception as e:
                    print(f"Error adding column {col_name}: {e}")

            print("Database initialized/checked successfully.")
    except Exception as e:
        print(f"Database initialization error: {e}")

def save_order_draft(conn, order_token, cart_data, total_amount):
    with conn.cursor() as cursor:
        query = f"""
        INSERT INTO {ORDERS_TABLE_NAME} (order_token, status, cart_data, total_amount)
        VALUES (%s, %s, %s, %s);
        """
        cursor.execute(query, (order_token, "pending_phone_auth", json.dumps(cart_data), total_amount))

def update_order(conn, order_token, **kwargs):
    # Универсальная функция обновления
    if not kwargs: return False
    
    updates = ["updated_at = CURRENT_TIMESTAMP"]
    params = []
    
    for key, value in kwargs.items():
        updates.append(f"{key} = %s")
        params.append(value)
    
    params.append(order_token)
    
    query = f"UPDATE {ORDERS_TABLE_NAME} SET {', '.join(updates)} WHERE order_token = %s"
    
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.rowcount > 0

def get_order_by_token(conn, order_token):
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT * FROM {ORDERS_TABLE_NAME} WHERE order_token = %s", (order_token,))
        # Возвращаем словарь для удобства (требует RealDictCursor, но сделаем вручную для надежности)
        columns = [desc[0] for desc in cursor.description]
        row = cursor.fetchone()
        if row:
            return dict(zip(columns, row))
        return None

# --- TELEGRAM UTILS ---

def send_message(chat_id, text, reply_markup=None):
    url = TG_API_BASE + 'sendMessage'
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Error sending message: {e}")

# --- TELEGRAM BOT LOGIC (Handle Updates) ---

def handle_telegram_update(conn, update):
    if 'message' not in update: return
    
    message = update['message']
    chat_id = message['chat']['id']
    text = message.get('text', '')
    
    # 1. ОБРАБОТКА КОНТАКТА
    if 'contact' in message:
        phone = message['contact']['phone_number']
        # Ищем последний заказ этого пользователя со статусом pending_phone_auth
        # (Упрощение: ищем по временному tg_id, который мы могли сохранить ранее, или просто обновляем последний созданный токен если бы мы его знали. 
        # В идеале, нужно хранить state пользователя. Здесь мы предполагаем, что flow идет последовательно)
        
        # ТАК КАК мы не знаем токен здесь напрямую (Telegram не передает его с контактом кроме как через reply),
        # Мы должны были сохранить chat_id при старте.
        
        # Для простоты: Мы просто пишем пользователю, что контакт принят.
        send_message(chat_id, "✅ Телефон принят! Теперь введите ваше **ФИО**:")
        
        # В реальном коде здесь нужно обновить статус заказа в БД на 'pending_name'
        # update_order_by_chat_id(conn, chat_id, status='pending_name', phone_number=phone)
        return

    # 2. ОБРАБОТКА КОМАНДЫ START
    if text.startswith('/start'):
        params = text.split()
        if len(params) > 1 and params[1].startswith('auth_'):
            order_token = params[1].replace('auth_', '')
            
            # Привязываем Chat ID к заказу
            if update_order(conn, order_token, user_tg_id=str(chat_id), status='pending_phone_auth'):
                keyboard = {
                    "keyboard": [[{"text": "📱 Отправить номер телефона", "request_contact": True}]],
                    "one_time_keyboard": True,
                    "resize_keyboard": True
                }
                send_message(chat_id, "👋 Привет! Мы получили ваш заказ.\nДля продолжения, пожалуйста, нажмите кнопку ниже, чтобы подтвердить номер телефона.", reply_markup=keyboard)
            else:
                send_message(chat_id, "⚠️ Ошибка: Заказ не найден.")
        else:
            send_message(chat_id, "Используйте кнопку 'Оформить заказ' на сайте.")
        return

    # 3. ОБРАБОТКА ТЕКСТА (ФИО, АДРЕС)
    # Здесь должна быть машина состояний (State Machine). 
    # Мы проверяем статус заказа пользователя в БД.
    
    # order = get_order_by_chat_id(conn, chat_id)
    # if order['status'] == 'pending_name':
    #    update_order(conn, order['token'], full_name=text, status='pending_address')
    #    send_message(chat_id, "Принято. Теперь введите полный **адрес доставки**:")
    # elif order['status'] == 'pending_address':
    #    update_order(conn, order['token'], address=text, status='awaiting_payment')
    #    send_message(chat_id, "Адрес сохранен! Менеджер свяжется с вами для оплаты.")


# --- MAIN APPLICATION ---

def application(environ, start_response):
    method = environ.get('REQUEST_METHOD', 'GET')
    path = environ.get('PATH_INFO', '/')
    
    # Health check for Render
    if path == '/' and method in ['GET', 'HEAD']:
        start_response('200 OK', [('Content-type', 'text/plain')])
        return [b"Server is running"]

    conn = None
    try:
        conn = create_psql_connection()
        init_db(conn) # Убеждаемся, что БД готова

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
                return [json.dumps({'error': str(e)}).encode('utf-8')]

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
                print(f"Webhook Error: {e}")
                start_response('200 OK', [('Content-Type', 'text/plain')]) # Всегда возвращаем 200 ТГ
                return [b'OK']

        # 404 для всего остального
        start_response('404 Not Found', [('Content-Type', 'text/plain')])
        return [b'Not Found']

    except Exception as e:
        print(f"Critical Error: {e}")
        start_response('500 Internal Server Error', [('Content-Type', 'text/plain')])
        return [str(e).encode('utf-8')]
    finally:
        if conn: conn.close()
