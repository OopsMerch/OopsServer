import os
import json
import uuid
import psycopg2 
import requests 
import re 
import hmac
import hashlib 
from urllib.parse import parse_qsl 
from typing import Dict, Any, Any
from datetime import datetime

# --- CONFIGURATION ---
DATABASE_URL = os.environ.get('DATABASE_URL')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
ADMIN_CHAT_ID = os.environ.get('ADMIN_CHAT_ID') 
TELEGRAM_BOT_USERNAME = os.environ.get('TELEGRAM_BOT_USERNAME', 'oopsmerchbot') 
SITE_BASE_URL = os.environ.get('SITE_BASE_URL', 'https://oops-merch.ru') 

ORDERS_TABLE_NAME = 'orders'
# Ваш токен уже встроен в URL благодаря переменной окружения
TG_API_BASE = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/' 
CORS_HEADERS = [
    ('Access-Control-Allow-Origin', '*'), 
    ('Access-Control-Allow-Methods', 'POST, GET, OPTIONS'), 
    ('Access-Control-Allow-Headers', 'Content-Type')
]

# --- UTILITY FUNCTIONS ---

# (Предполагается, что здесь есть вспомогательные функции для DB: init_db, get_db_connection, get_order_by_token, update_order_status)

# --- TELEGRAM AUTH FUNCTION (EXISTING) ---
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
    
    # HMAC-SHA256
    h = hmac.new(secret_key, data_check_string.encode('utf-8'), hashlib.sha256)
    
    return h.hexdigest() == auth_data['hash']

# [НОВЫЙ БЛОК: TELEGRAM UTILITY]
def send_telegram_message(chat_id: int, text: str, reply_markup: Dict[str, Any] = None) -> None:
    """Отправка сообщения через Telegram API."""
    url = TG_API_BASE + 'sendMessage'
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML',
    }
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)
        
    try:
        # Убедитесь, что TELEGRAM_BOT_TOKEN установлен в окружении Render
        response = requests.post(url, data=payload)
        response.raise_for_status() 
        # print(f"Message sent successfully: {response.json()}")
    except Exception as e:
        print(f"Error sending message to {chat_id}: {e}")

# [НОВЫЙ БЛОК: БОТ-ЛОГИКА]
def handle_telegram_update(conn: Any, update: Dict[str, Any]) -> None:
    """
    Основная логика обработки всех входящих обновлений от Telegram.
    Это заглушка, которую нужно будет реализовать полностью.
    """
    if 'message' in update:
        message = update['message']
        chat_id = message['chat']['id']
        text = message.get('text', '')
        
        print(f"[{datetime.now().isoformat()}] Received message from {chat_id}: {text}")

        # Проверка на команду /start с токеном
        if text.startswith('/start auth_'):
            order_token = text.split('_')[-1]
            
            # TODO: Реализовать:
            # 1. Найти заказ по order_token в БД.
            # 2. Если найден: Обновить запись: установить chat_id пользователя в поле telegram_id.
            # 3. Обновить статус заказа.
            # 4. Отправить пользователю первый вопрос (например, "Введите ФИО").
            
            # Ответ-заглушка
            send_telegram_message(
                chat_id, 
                f"✅ **Заказ найден!** Ваш токен: `{order_token}`\n\n"
                f"Отправьте ваше **ФИО** для оформления доставки."
            )
        
        elif text == '/start':
            send_telegram_message(
                chat_id, 
                "Добро пожаловать! Чтобы начать оформление заказа, вернитесь на сайт, добавьте товары в корзину и нажмите 'Оформить заказ в Telegram'."
            )
            
        else:
            # TODO: Реализовать:
            # Логику диалога (проверка текущего этапа заказа и ожидаемого ответа)
            send_telegram_message(
                chat_id, 
                f"Спасибо, я получил: *{text}*.\n\n"
                "Ваша логика обработки ФИО/адреса пока не реализована. Продолжайте работу над функцией `handle_telegram_update`."
            )
        
        # !!! ВАША ОСНОВНАЯ ЛОГИКА БУДЕТ ЗДЕСЬ !!!

# --- WSGI APPLICATION ---
def application(environ: Dict[str, Any], start_response: Any) -> Any:
    method = environ.get('REQUEST_METHOD')
    path = environ.get('PATH_INFO')
    conn = None
    
    try:
        # Инициализация подключения к БД
        # (Предполагается, что здесь вызов get_db_connection и init_db)
        # conn = get_db_connection()
        # if conn: init_db(conn)
        
        # 0. CORS OPTIONS (EXISTING)
        if method == 'OPTIONS':
            start_response('200 OK', CORS_HEADERS)
            return [b'']

        # 1. INIT AUTH (POST /init-auth) (EXISTING)
        if method == 'POST' and path == '/init-auth':
            # ... Ваш код инициализации заказа и возврата токена ...
            # ... (Пример: возвращает {'auth_token': '...'})
            pass 
        
        # 2. TELEGRAM AUTH SUCCESS (GET /auth/) (EXISTING)
        if path.startswith('/auth/'):
            # ... Ваш код проверки авторизации и редиректа ...
            pass
            
        # 3. ORDER STATUS POLLING (GET /status/) (EXISTING)
        if path.startswith('/status/'):
            order_token = path.split('/')[-1]
            # ... Ваш код получения статуса ...
            pass

        # [НОВЫЙ БЛОК: WEBHOOK ENDPOINT]
        if method == 'POST' and path == '/webhook':
            try:
                # Читаем тело запроса, отправленное Telegram
                request_body_size = int(environ.get('CONTENT_LENGTH', 0))
                request_body = environ['wsgi.input'].read(request_body_size)
                update = json.loads(request_body)
                
                # Передаем обновление в нашу логику бота
                # Не забудьте передать conn, если он используется
                # handle_telegram_update(conn, update) 
                
                # ОБЯЗАТЕЛЬНО: Возвращаем 200 OK. 
                # Это самый важный шаг для работы Webhook!
                start_response('200 OK', [('Content-Type', 'application/json')])
                return [json.dumps({'status': 'ok'}).encode('utf-8')]
            
            except Exception as e:
                print(f"Webhook processing error: {e}")
                # Даже при ошибке возвращаем 200 OK, чтобы Telegram не спамил
                start_response('200 OK', [('Content-type', 'application/json')]) 
                return [json.dumps({'error': 'Internal server error during webhook handling'}).encode('utf-8')]
                
        # 4. DEFAULT 404 (EXISTING)
        start_response('404 Not Found', [('Content-type', 'text/plain')])
        return [b'Not Found']
        
    except Exception as e:
        print(f"CRITICAL: {e}")
        start_response('500 Error', [('Content-type', 'application/json')])
        return [json.dumps({'error': str(e)}).encode('utf-8')]
    finally:
        if conn: conn.close()
