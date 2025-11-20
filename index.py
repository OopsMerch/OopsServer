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

ORDERS_TABLE_NAME = 'orders'
TG_API_BASE = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/'

# CORS заголовки (разрешают запросы с вашего сайта)
CORS_HEADERS = [
    ('Access-Control-Allow-Origin', '*'), 
    ('Access-Control-Allow-Methods', 'POST, GET, OPTIONS'), 
    ('Access-Control-Allow-Headers', 'Content-Type')
]

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
            # Создаем таблицу со всеми нужными полями, если ее нет
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
            
            # Проверка и добавление колонки total_amount (если таблица существовала)
            try:
                cur.execute(f"ALTER TABLE {ORDERS_TABLE_NAME} ADD COLUMN total_amount NUMERIC(10, 2) NOT NULL DEFAULT 0.00;")
            except psycopg2.errors.DuplicateColumn:
                pass 
            
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
        send_message(chat_id, "✅ Телефон принят! Теперь введите ваше **ФИО**:")
        return

    # 2. ОБРАБОТКА КОМАНДЫ START
    if text.startswith('/start'):
        params = text.split()
        if len(params) > 1 and params[1].startswith('auth_'):
            order_token = params[1].replace('auth_', '')
            
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
    
    # 3. ОБРАБОТКА ТЕКСТА (ФИО, АДРЕС) - ЛОГИКА ДИАЛОГА НЕ ПОЛНАЯ, НО НЕ КРИТИЧНА ДЛЯ ЗАПУСКА
    # На этом этапе бот просто игнорирует сообщения, кроме контактов и /start.
    
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
            except:
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
