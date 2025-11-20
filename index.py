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

# !!! ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ДЛЯ БИЗНЕС-ЛОГИКИ !!!
TG_ADMIN_GROUP_ID = os.environ.get('TG_ADMIN_GROUP_ID') # ID группы для заказов
ADMIN_SUPPORT_USERNAME = os.environ.get('ADMIN_SUPPORT_USERNAME', '@oopssupport') # Имя саппорта

# ПЕРЕМЕННЫЕ ДЛЯ ОПЛАТЫ
SBERBANK_CARD = os.environ.get('SBERBANK_CARD', 'XXXX XXXX XXXX XXXX')
TBANK_CARD = os.environ.get('TBANK_CARD', 'YYYY YYYY YYYY YYYY')
ALFABANK_CARD = os.environ.get('ALFABANK_CARD', 'ZZZZ ZZZZ ZZZZ ZZZZ') # <-- НОВАЯ КАРТА

ORDERS_TABLE_NAME = 'orders'
TG_API_BASE = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/'

# CORS заголовки (разрешают запросы с вашего сайта)
CORS_HEADERS = [
    ('Access-Control-Allow-Origin', '*'), 
    ('Access-Control-Allow-Methods', 'POST, GET, OPTIONS'), 
    ('Access-Control-Allow-Headers', 'Content-Type')
]

# --- МАШИНА СОСТОЯНИЙ ДЛЯ БОТА ---
# Эти статусы будут использоваться в поле 'status' таблицы orders для отслеживания прогресса
STATUS_PENDING_AUTH = 'pending_phone_auth'
STATUS_PENDING_FULL_NAME = 'pending_full_name'
STATUS_PENDING_ADDRESS = 'pending_address'
STATUS_PENDING_DELIVERY_TYPE = 'pending_delivery_type'
STATUS_PENDING_CONFIRMATION = 'pending_confirmation'
STATUS_PENDING_PAYMENT = 'pending_payment'
STATUS_AWAITING_ADMIN = 'awaiting_admin_input'
STATUS_COMPLETED = 'completed'


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
                    delivery_type VARCHAR(50) DEFAULT NULL,    -- НОВОЕ: Тип доставки (СДЭК/Почта)
                    delivery_address_data TEXT DEFAULT NULL, -- НОВОЕ: Адрес ПВЗ или точный адрес
                    admin_track_number VARCHAR(50) DEFAULT NULL, -- НОВОЕ: Трек-номер от админа
                    admin_delivery_date TIMESTAMP WITH TIME ZONE DEFAULT NULL, -- НОВОЕ: Дата доставки от админа
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            # Проверка и добавление новых колонок (если таблица существовала)
            def add_column_if_not_exists(col_name, col_type):
                try:
                    cur.execute(f"ALTER TABLE {ORDERS_TABLE_NAME} ADD COLUMN {col_name} {col_type};")
                except psycopg2.errors.DuplicateColumn:
                    pass 

            add_column_if_not_exists('total_amount', 'NUMERIC(10, 2) NOT NULL DEFAULT 0.00')
            add_column_if_not_exists('delivery_type', 'VARCHAR(50) DEFAULT NULL')
            add_column_if_not_exists('delivery_address_data', 'TEXT DEFAULT NULL')
            add_column_if_not_exists('admin_track_number', 'VARCHAR(50) DEFAULT NULL')
            add_column_if_not_exists('admin_delivery_date', 'TIMESTAMP WITH TIME ZONE DEFAULT NULL')
            
    except Exception as e:
        print(f"Database initialization error: {e}")

def save_order_draft(conn, order_token, cart_data, total_amount):
    with conn.cursor() as cursor:
        query = f"""
        INSERT INTO {ORDERS_TABLE_NAME} (order_token, status, cart_data, total_amount)
        VALUES (%s, %s, %s, %s);
        """
        cursor.execute(query, (order_token, STATUS_PENDING_AUTH, json.dumps(cart_data), total_amount))

def update_order(conn, order_token=None, user_tg_id=None, **kwargs):
    if not kwargs: return False
    
    updates = ["updated_at = CURRENT_TIMESTAMP"]
    params = []
    for key, value in kwargs.items():
        if key == 'cart_data': # Специальная обработка для JSONB
             updates.append(f"{key} = %s::jsonb")
        else:
             updates.append(f"{key} = %s")
        params.append(value)
        
    where_clause = ""
    if order_token:
        where_clause = "order_token = %s"
        params.append(order_token)
    elif user_tg_id:
        # Ищем только "активные" заказы по user_tg_id
        where_clause = f"user_tg_id = %s AND status IN ('{STATUS_PENDING_AUTH}', '{STATUS_PENDING_FULL_NAME}', '{STATUS_PENDING_ADDRESS}', '{STATUS_PENDING_DELIVERY_TYPE}', '{STATUS_PENDING_CONFIRMATION}')"
        params.append(user_tg_id)
    else:
        return False
        
    query = f"UPDATE {ORDERS_TABLE_NAME} SET {', '.join(updates)} WHERE {where_clause}"
    
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.rowcount > 0

def get_order_by_tg_id(conn, user_tg_id):
    query = f"""
    SELECT 
        order_token, status, total_amount, cart_data, phone_number, full_name, address, delivery_type, delivery_address_data
    FROM 
        {ORDERS_TABLE_NAME}
    WHERE 
        user_tg_id = %s 
        AND status IN ('{STATUS_PENDING_AUTH}', '{STATUS_PENDING_FULL_NAME}', '{STATUS_PENDING_ADDRESS}', '{STATUS_PENDING_DELIVERY_TYPE}', '{STATUS_PENDING_CONFIRMATION}', '{STATUS_PENDING_PAYMENT}')
    ORDER BY 
        created_at DESC
    LIMIT 1;
    """
    with conn.cursor() as cursor:
        cursor.execute(query, (user_tg_id,))
        row = cursor.fetchone()
        if row:
            # Преобразование кортежа в словарь для удобства
            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, row))
        return None
        
def get_order_by_token(conn, order_token):
    query = f"""
    SELECT 
        id, order_token, status, total_amount, cart_data, user_tg_id, phone_number, full_name, address, delivery_type, delivery_address_data
    FROM 
        {ORDERS_TABLE_NAME}
    WHERE 
        order_token = %s 
    LIMIT 1;
    """
    with conn.cursor() as cursor:
        cursor.execute(query, (order_token,))
        row = cursor.fetchone()
        if row:
            # Преобразование кортежа в словарь для удобства
            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, row))
        return None


# --- TELEGRAM UTILS ---

def send_message(chat_id, text, reply_markup=None):
    url = TG_API_BASE + 'sendMessage'
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}
    if reply_markup:
        # Убедимся, что reply_markup корректно сериализован, если он уже не строка
        if isinstance(reply_markup, dict):
            payload['reply_markup'] = json.dumps(reply_markup)
        else:
            payload['reply_markup'] = reply_markup
            
    try:
        # print(f"Sending message to {chat_id}: {text}") # Debug
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Error sending message: {e}")

def generate_admin_order_message(order_data):
    cart_items = json.loads(order_data['cart_data'])
    items_list = "\n".join([f"- {item['quantity']} шт. | {item['name']} (Размер: {item['size']}, {item['price']} ₽/шт.)" for item in cart_items])
    
    # Кнопка для администратора
    inline_keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Оформить (Админ)", "callback_data": f"admin_process_{order_data['order_token']}"}
            ]
        ]
    }

    message = f"""
🚨 **НОВЫЙ ЗАКАЗ** 🚨
Токен: `{order_data['order_token']}`
Сумма: **{order_data['total_amount']:.2f} ₽**

---
**Пользовательские данные:**
👤 ФИО: {order_data['full_name']}
📱 Телефон: {order_data['phone_number']}
📧 TG ID: `{order_data['user_tg_id']}`

---
**Доставка:**
🚚 Тип: **{order_data['delivery_type']}**
📍 Адрес/Пункт: {order_data['delivery_address_data'] or 'Не указан'}

---
**Содержимое заказа:**
{items_list}
"""
    return message, inline_keyboard


def send_admin_order_notification(order_data):
    # ! Используем глобальную переменную, определенную в КОНФИГУРАЦИИ !
    global TG_ADMIN_GROUP_ID 
    
    if not TG_ADMIN_GROUP_ID or TG_ADMIN_GROUP_ID == 'YOUR_ADMIN_GROUP_ID':
         print("Warning: TG_ADMIN_GROUP_ID is not set. Cannot send admin notification.")
         return

    # Предполагаем, что TG_ADMIN_GROUP_ID - это числовой ID группы
    try:
        chat_id = int(TG_ADMIN_GROUP_ID)
    except ValueError:
        chat_id = TG_ADMIN_GROUP_ID # Оставляем как строку, если это username
        
    message, reply_markup = generate_admin_order_message(order_data)
    send_message(chat_id, message, reply_markup=reply_markup)
    
def send_payment_details(chat_id, order_data):
    # Используем глобальные переменные, определенные в секции КОНФИГУРАЦИЯ
    global SBERBANK_CARD, TBANK_CARD, ALFABANK_CARD

    total = order_data['total_amount']

    message = f"""
🎉 **Прекрасно! Данные собраны!** 🎉

Осталось только **оплатить заказ** на сумму:
💰 **{total:.2f} ₽**

Выберите удобный способ оплаты:

1.  **Сбербанк** (Для РФ):
    `{SBERBANK_CARD}`

2.  **Тинькофф** (Для РФ):
    `{TBANK_CARD}`
    
3.  **Альфа-Банк** (Для РФ):
    `{ALFABANK_CARD}`
    
---
**❗ После оплаты:**
Пришлите скриншот или **чек-квитанцию файлом** в этот чат для подтверждения оплаты.
Как только оплата будет подтверждена, мы передадим заказ в обработку.

Спасибо за покупку!
"""
    # Удаляем клавиатуру контактов
    remove_keyboard = {"remove_keyboard": True}
    send_message(chat_id, message, reply_markup=remove_keyboard)
    
# --- TELEGRAM BOT LOGIC (Handle Updates) ---

def handle_telegram_update(conn, update):
    
    # 1. ОБРАБОТКА CALLBACK_QUERY (Кнопки в чате админа)
    if 'callback_query' in update:
        query = update['callback_query']
        chat_id = query['message']['chat']['id']
        message_id = query['message']['message_id']
        data = query['data']

        # Простая заглушка для админской кнопки
        if data.startswith('admin_process_'):
            order_token = data.replace('admin_process_', '')
            order_data = get_order_by_token(conn, order_token)
            
            # ! Проверка: Убедитесь, что это администратор, если ID группы совпадает !
            
            if order_data and order_data['status'] == STATUS_PENDING_PAYMENT:
                 # Меняем статус и запрашиваем ввод данных от админа
                 update_order(conn, order_token=order_token, status=STATUS_AWAITING_ADMIN)
                 
                 response_text = f"✅ Заказ **{order_token}** принят в обработку. \n\n**Введите данные в формате:**\nТРЕК_НОМЕР | АДРЕС_ПВЗ | ПРИМЕРНАЯ_ДАТА_ПОЛУЧЕНИЯ"
                 
                 # Редактируем сообщение, чтобы убрать кнопку
                 edit_message_url = TG_API_BASE + 'editMessageText'
                 requests.post(edit_message_url, json={
                    'chat_id': chat_id,
                    'message_id': message_id,
                    'text': query['message']['text'] + '\n\n**Статус:** 🔄 Ожидает ввода данных от администратора',
                    'parse_mode': 'Markdown',
                    'reply_markup': json.dumps({"inline_keyboard": []}) # Убираем кнопку
                 })
                 
                 # Отправляем отдельное сообщение с запросом ввода
                 send_message(chat_id, response_text, reply_markup=None)
            
        return
        
    if 'message' not in update: return
    
    message = update['message']
    chat_id = message['chat']['id']
    text = message.get('text', '')
    
    # Получаем или создаем контекст/состояние заказа
    order = get_order_by_tg_id(conn, str(chat_id))
    
    # 1. ОБРАБОТКА КОНТАКТА
    if 'contact' in message and order and order['status'] == STATUS_PENDING_AUTH:
        phone = message['contact']['phone_number']
        # Обновляем заказ, меняем статус
        update_order(conn, order_token=order['order_token'], phone_number=phone, status=STATUS_PENDING_FULL_NAME)
        # Отправляем следующее сообщение
        remove_keyboard = {"remove_keyboard": True}
        send_message(chat_id, "✅ Телефон принят! Теперь введите ваше **ФИО** (Полностью):", reply_markup=remove_keyboard)
        return

    # 2. ОБРАБОТКА КОМАНДЫ START
    if text.startswith('/start'):
        params = text.split()
        if len(params) > 1 and params[1].startswith('auth_'):
            order_token = params[1].replace('auth_', '')
            
            # Пытаемся привязать TG ID к заказу и перейти в статус ожидания контакта
            if update_order(conn, order_token=order_token, user_tg_id=str(chat_id), status=STATUS_PENDING_AUTH):
                keyboard = {
                    "keyboard": [[{"text": "📱 Отправить номер телефона", "request_contact": True}]],
                    "one_time_keyboard": True,
                    "resize_keyboard": True
                }
                send_message(chat_id, "👋 Привет! Мы получили ваш заказ.\nДля продолжения, пожалуйста, нажмите кнопку ниже, чтобы **подтвердить номер телефона**.", reply_markup=keyboard)
            else:
                send_message(chat_id, "⚠️ Ошибка: Заказ не найден или уже обработан.")
        else:
            send_message(chat_id, "Используйте кнопку 'Оформить заказ' на сайте.")
        return
        
    # 3. ОБРАБОТКА ТЕКСТА (Диалог с пользователем)
    
    if order:
        order_token = order['order_token']
        current_status = order['status']
        
        # --- 3.1 ОЖИДАНИЕ ФИО ---
        if current_status == STATUS_PENDING_FULL_NAME:
            full_name = text.strip()
            if len(full_name) < 5 or len(full_name.split()) < 2:
                 send_message(chat_id, "⚠️ Пожалуйста, введите полное **ФИО** (минимум Имя и Фамилия).")
                 return
                 
            update_order(conn, order_token=order_token, full_name=full_name, status=STATUS_PENDING_ADDRESS)
            send_message(chat_id, "Спасибо, **ФИО** принято!\n\nВведите ваш **адрес** (например: *город, улица, дом, квартира*). \n\n*❗ Обратите внимание: Мы будем использовать этот адрес для выбора ближайшего пункта выдачи СДЭК или Почты России.*")
            return
            
        # --- 3.2 ОЖИДАНИЕ АДРЕСА ---
        elif current_status == STATUS_PENDING_ADDRESS:
            address = text.strip()
            if len(address) < 10:
                 send_message(chat_id, "⚠️ Пожалуйста, введите более полный и точный адрес.")
                 return
            
            update_order(conn, order_token=order_token, address=address, status=STATUS_PENDING_DELIVERY_TYPE)
            
            keyboard = {
                "inline_keyboard": [
                    [{"text": "🚚 СДЭК", "callback_data": "delivery_sdek"}],
                    [{"text": "📬 Почта России", "callback_data": "delivery_russian_post"}]
                ]
            }
            send_message(chat_id, "✅ Адрес принят.\n\n**Выберите удобный способ получения заказа:**", reply_markup=keyboard)
            return
            
        # --- 3.3 ОЖИДАНИЕ ВЫБОРА ДОСТАВКИ (КНОПКА) ---
        # Эта логика обрабатывается через callback_query, но в рамках упрощения,
        # если пользователь пришлет текст, просто повторим вопрос с кнопками.
        elif current_status == STATUS_PENDING_DELIVERY_TYPE:
             # Проверяем, если это не нажатие inline-кнопки
             if 'callback_query' not in update:
                # Пытаемся обработать выбор из текста (для простоты)
                delivery_type_text = text.lower()
                delivery_type = None
                delivery_info = ""
                
                if 'сдэк' in delivery_type_text:
                    delivery_type = 'СДЭК'
                    delivery_info = f"Для **СДЭК** будет выбран ближайший пункт выдачи (ПВЗ) к указанному вами адресу: *{order['address']}*."
                elif 'почта' in delivery_type_text:
                    delivery_type = 'Почта России'
                    delivery_info = f"Для **Почты России** будет использован полный адрес для доставки до почтового отделения: *{order['address']}*."
                
                if delivery_type:
                    # Обновляем заказ, переходим к подтверждению
                    update_order(conn, order_token=order_token, delivery_type=delivery_type, delivery_address_data=order['address'], status=STATUS_PENDING_CONFIRMATION)
                    
                    confirmation_message = f"""
✅ Способ получения: **{delivery_type}** выбран! 
{delivery_info}

---
**Проверьте ваши данные:**
👤 **ФИО:** {order['full_name']}
📱 **Телефон:** {order['phone_number']}
📍 **Адрес:** {order['address']}
🚚 **Способ:** {delivery_type}

---
**Подтвердите оформление заказа?**
"""
                    keyboard = {
                        "inline_keyboard": [
                            [{"text": "✅ Подтвердить", "callback_data": "confirm_order"}],
                            [{"text": "❌ Заполнить заново", "callback_data": "start_over"}]
                        ]
                    }
                    send_message(chat_id, confirmation_message, reply_markup=keyboard)
                    return
                else:
                    # Повторяем запрос
                    keyboard = {
                        "inline_keyboard": [
                            [{"text": "🚚 СДЭК", "callback_data": "delivery_sdek"}],
                            [{"text": "📬 Почта России", "callback_data": "delivery_russian_post"}]
                        ]
                    }
                    send_message(chat_id, "⚠️ Не удалось распознать способ. Выберите **СДЭК** или **Почта России**.", reply_markup=keyboard)
                    return

        # --- 3.4 ОЖИДАНИЕ ФАЙЛА (ЧЕКА) ---
        elif current_status == STATUS_PENDING_PAYMENT:
            # Если прислали фотографию, документ или просто текст - считаем, что это подтверждение оплаты.
            # В реальной системе тут нужно более строго проверять тип файла (document, photo)
            if 'photo' in message or 'document' in message or text:
                 # Меняем статус на ожидание админа
                 update_order(conn, order_token=order_token, status=STATUS_AWAITING_ADMIN)
                 
                 # Отправляем уведомление администратору
                 order_data_full = get_order_by_tg_id(conn, str(chat_id))
                 if order_data_full:
                    send_admin_order_notification(order_data_full)
                    
                 send_message(chat_id, "✨ **Отлично!** Мы получили ваше подтверждение оплаты.\n\nПередаем заказ администратору для оформления доставки и трек-номера. Это займет некоторое время.")
                 return
                 
        # --- 3.5 ОБРАБОТКА ОТВЕТА АДМИНИСТРАТОРА В ГРУППЕ ---
        # Логика для админской группы (должна быть отдельной)
        global TG_ADMIN_GROUP_ID, ADMIN_SUPPORT_USERNAME
        
        # Проверяем, если сообщение пришло от администратора в группу и мы ожидаем ввод
        if str(chat_id) == TG_ADMIN_GROUP_ID or (TG_ADMIN_GROUP_ID and chat_id == int(TG_ADMIN_GROUP_ID)):
            # Ищем любой заказ в статусе STATUS_AWAITING_ADMIN
            # В идеале нужно привязать ответ к конкретному заказу (например, через Reply)
            # Но для упрощения возьмем последний заказ в этом статусе.
            
            # ! Для масштабирования: Здесь нужна более сложная логика привязки ответа к заказу, 
            # ! например, через проверку, что это ответ на сообщение бота с кнопкой "Оформить"
            
            if order and order['status'] == STATUS_AWAITING_ADMIN: # Логика не совсем верна, но для демо - подойдет
                
                # Ищем заказ, который ожидает ввода данных администратора
                # В этом упрощенном примере мы просто обрабатываем ввод, предполагая, что это ответ на последний запрос
                
                try:
                    # Ожидаемый формат: ТРЕК_НОМЕР | АДРЕС_ПВЗ | ПРИМЕРНАЯ_ДАТА
                    track_number, pvz_address, delivery_date_str = [x.strip() for x in text.split('|')]
                    
                    # Ищем клиента
                    order_to_update = get_order_by_token(conn, order['order_token'])
                    
                    if order_to_update and order_to_update['status'] == STATUS_AWAITING_ADMIN:
                        # Обновляем заказ
                        update_order(
                            conn, 
                            order_token=order_to_update['order_token'], 
                            admin_track_number=track_number, 
                            delivery_address_data=pvz_address, # Переписываем адрес на ПВЗ
                            admin_delivery_date=delivery_date_str, # Сохраняем дату как строку/datetime
                            status=STATUS_COMPLETED
                        )

                        # Отправляем сообщение пользователю
                        
                        client_message = f"""
✅ **Ваш заказ оформлен!**

Вот **трек-номер**: `{track_number}`

Благодарим за покупку, {order_to_update['full_name']}!

Ваш заказ:
{generate_order_summary_list(order_to_update)}

Пункт выдачи: 
*{pvz_address}*

🕰️ Примерная дата получения:
**{delivery_date_str}**

---
📦 Если заканчивается срок хранения посылки на пункте выдачи - напишите нам для продления. Иначе за возврат удерживается сумма (за доставку к вам и обратно).

🔗 По всем вопросам к администратору: {ADMIN_SUPPORT_USERNAME}
"""
                        send_message(int(order_to_update['user_tg_id']), client_message)
                        
                        send_message(chat_id, f"✅ Сообщение о доставке отправлено пользователю **{order_to_update['full_name']}** (Токен: {order_to_update['order_token']})")
                        return

                except Exception as e:
                    print(f"Admin input parsing error: {e}")
                    # Если не удалось распарсить, напоминаем формат
                    send_message(chat_id, "⚠️ **Неверный формат ввода.** Пожалуйста, используйте: \n`ТРЕК_НОМЕР | АДРЕС_ПВЗ | ПРИМЕРНАЯ_ДАТА_ПОЛУЧЕНИЯ`")
                    return


    # 4. ОБРАБОТКА CALLBACK_QUERY (Кнопки в чате пользователя - Выбор доставки/Подтверждение)
    if 'callback_query' in update:
        query = update['callback_query']
        chat_id = query['message']['chat']['id']
        data = query['data']
        
        # Получаем контекст заказа
        order = get_order_by_tg_id(conn, str(chat_id))
        
        if order and order['status'] in [STATUS_PENDING_DELIVERY_TYPE, STATUS_PENDING_CONFIRMATION]:
            order_token = order['order_token']
            
            # --- Выбор способа доставки ---
            if data == 'delivery_sdek' and order['status'] == STATUS_PENDING_DELIVERY_TYPE:
                delivery_type = 'СДЭК'
                delivery_info = f"Для **СДЭК** будет выбран ближайший пункт выдачи (ПВЗ) к указанному вами адресу: *{order['address']}*."
            elif data == 'delivery_russian_post' and order['status'] == STATUS_PENDING_DELIVERY_TYPE:
                delivery_type = 'Почта России'
                delivery_info = f"Для **Почты России** будет использован полный адрес для доставки до почтового отделения: *{order['address']}*."
            
            # Переход к подтверждению
            if 'delivery_type' in locals():
                # Обновляем заказ, переходим к подтверждению
                update_order(conn, order_token=order_token, delivery_type=delivery_type, delivery_address_data=order['address'], status=STATUS_PENDING_CONFIRMATION)
                
                confirmation_message = f"""
✅ Способ получения: **{delivery_type}** выбран! 
{delivery_info}

---
**Проверьте ваши данные:**
👤 **ФИО:** {order['full_name']}
📱 **Телефон:** {order['phone_number']}
📍 **Адрес:** {order['address']}
🚚 **Способ:** {delivery_type}

---
**Подтвердите оформление заказа?**
"""
                keyboard = {
                    "inline_keyboard": [
                        [{"text": "✅ Подтвердить", "callback_data": "confirm_order"}],
                        [{"text": "❌ Заполнить заново", "callback_data": "start_over"}]
                    ]
                }
                
                # Редактируем сообщение, чтобы избежать спама
                edit_message_url = TG_API_BASE + 'editMessageText'
                requests.post(edit_message_url, json={
                    'chat_id': chat_id,
                    'message_id': query['message']['message_id'],
                    'text': confirmation_message,
                    'parse_mode': 'Markdown',
                    'reply_markup': json.dumps(keyboard)
                })
                
                return
            
            # --- Подтверждение/Сброс ---
            elif data == 'confirm_order' and order['status'] == STATUS_PENDING_CONFIRMATION:
                # Обновляем статус на ожидание оплаты
                update_order(conn, order_token=order_token, status=STATUS_PENDING_PAYMENT)
                
                # Отправляем реквизиты
                send_payment_details(chat_id, order)
                
                # Редактируем сообщение для фиксации выбора
                edit_message_url = TG_API_BASE + 'editMessageText'
                requests.post(edit_message_url, json={
                    'chat_id': chat_id,
                    'message_id': query['message']['message_id'],
                    'text': query['message']['text'] + '\n\n**Статус:** ✅ **Подтверждено.** Ожидаем оплаты.',
                    'parse_mode': 'Markdown',
                    'reply_markup': json.dumps({"inline_keyboard": []}) # Убираем кнопку
                })
                return
            
            elif data == 'start_over':
                # Сбрасываем заказ в начальный статус (ожидание ФИО)
                update_order(
                    conn, 
                    order_token=order_token, 
                    full_name=None, 
                    address=None, 
                    delivery_type=None,
                    status=STATUS_PENDING_FULL_NAME
                )
                
                send_message(chat_id, "🔄 **Начинаем заново.** Введите ваше **ФИО** (Полностью):")
                # Редактируем сообщение для фиксации сброса
                edit_message_url = TG_API_BASE + 'editMessageText'
                requests.post(edit_message_url, json={
                    'chat_id': chat_id,
                    'message_id': query['message']['message_id'],
                    'text': query['message']['text'] + '\n\n**Статус:** ❌ **Сброшено.**',
                    'parse_mode': 'Markdown',
                    'reply_markup': json.dumps({"inline_keyboard": []})
                })
                return

    
def generate_order_summary_list(order_data):
    cart_items = json.loads(order_data.get('cart_data', '[]'))
    items_list = "\n".join([
        f"{item['quantity']} шт. | {item['name']} (Размер: {item['size']})" 
        for item in cart_items
    ])
    return items_list

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
                # Сохраняем в статусе STATUS_PENDING_AUTH
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
                
                # ! ДОБАВЛЕНИЕ ЛОГИКИ ОБРАБОТКИ CALLBACK QUERY !
                if 'callback_query' in update:
                    handle_telegram_update(conn, update)
                elif 'message' in update:
                    handle_telegram_update(conn, update)
                
                start_response('200 OK', [('Content-Type', 'text/plain')])
                return [b'OK']
            except Exception as e:
                # В случае ошибки всегда возвращаем 200 OK, чтобы Telegram не переотправлял
                print(f"Webhook processing error: {e}")
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
