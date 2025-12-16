_id']}](tg://user?id={order['user_tg_id']})\n"
            f"🚚 **Адрес:** `{order['address'] or 'Нет адреса'}`\n\n"
            f"🛒 **Товары:**\n{BotLogic.generate_cart_text(order['cart_data'])}\n\n"
            f"👇 **ЧЕК НИЖЕ:**"
        )
        kb = {"inline_keyboard": [[{"text": "🛠 Взять в работу", "callback_data": f"admin_process_{order['order_token']}"}]]}
        TelegramClient.send_message(Config.TG_ADMIN_GROUP_ID, txt, kb)

    @staticmethod
    def handle_update(update):
        if 'callback_query' in update:
            BotLogic._process_callback(update['callback_query'])
        elif 'message' in update:
            BotLogic._process_message(update['message'])

    @staticmethod
    def _process_callback(cb):
        chat_id = cb['message']['chat']['id']
        msg_id = cb['message']['message_id']
        data = cb['data']
        
        # Админ берет заказ
        if data.startswith('admin_process_'):
            token = data.split('_')[-1]
            order = Database.get_order(token=token)
            if order and order['status'] == DatabaseParams.ST_AWAIT_ADMIN:
                Database.update_order(order_token=token, status=DatabaseParams.ST_ADMIN_PROC)
                
                # Обновляем сообщение с чеком
                clean_text = cb['message']['text'].split('👇')[0].strip()
                TelegramClient.edit_message(chat_id, msg_id, clean_text + '\n\n✅ **ВЗЯТ В РАБОТУ**', {})
                
                # Инструкция админу
                admin_instr = (
                    f"✅ **В работе** `{token}`\n\n"
                    f"1. **Трек:** `{token} | ТРЕК | АДРЕС ПВЗ | ДАТА`\n"
                    f"2. **Прибыл:** `{token} | ПРИБЫЛ`"
                )
                TelegramClient.send_message(chat_id, admin_instr)
            elif order:
                TelegramClient.edit_message(chat_id, msg_id, cb['message']['text'] + '\n\n⚠️ **УЖЕ В РАБОТЕ**', {})
            return

        # Пользователь забрал заказ
        if data == 'user_received_order':
            order = Database.get_order(tg_id=chat_id)
            if order and order['status'] == DatabaseParams.ST_ARRIVED:
                Database.update_order(order_token=order['order_token'], status=DatabaseParams.ST_REVIEW)
                TelegramClient.edit_message(chat_id, msg_id, cb['message']['text'].split('Пожалуйста')[0] + '\n\n✅ **Получено**', None)
                TelegramClient.send_message(chat_id, "🥳 **Поздравляем!**\nБудем рады вашему отзыву (фото+текст) прямо здесь!")

    @staticmethod
    def _process_message(msg):
        chat_id = msg['chat']['id']
        text = msg.get('text', '').strip()
        
        # --- АДМИН ПАНЕЛЬ ---
        if str(chat_id) == Config.TG_ADMIN_GROUP_ID:
            if '|' in text:
                parts = [x.strip() for x in text.split('|')]
                token = parts[0]
                order = Database.get_order(token=token)
                if not order: return

                if len(parts) == 4 and order['status'] == DatabaseParams.ST_ADMIN_PROC:
                    # Отправка трека
                    track, pvz, date = parts[1], parts[2], parts[3]
                    Database.update_order(token, admin_track_number=track, delivery_address_data=pvz, admin_delivery_date=date, status=DatabaseParams.ST_SHIPPING)
                    
                    user_msg = f"🚀 **Заказ отправлен!**\n\n📦 Трек: `{track}`\n🏢 ПВЗ: {pvz}\n⏳ Дата: {date}\n\nМы сообщим, когда он прибудет!"
                    TelegramClient.send_message(int(order['user_tg_id']), user_msg)
                    TelegramClient.send_message(chat_id, f"✅ Трек отправлен ({token})")
                
                elif len(parts) == 2 and parts[1].upper() in ['ARRIVED', 'ПРИБЫЛ'] and order['status'] == DatabaseParams.ST_SHIPPING:
                    # Прибытие
                    Database.update_order(token, status=DatabaseParams.ST_ARRIVED)
                    user_msg = f"🏃 **Заказ прибыл!**\n\nПВЗ: {order['delivery_address_data']}\nКод в приложении СДЭК.\n\n👇 Нажмите кнопку, когда заберете!"
                    kb = {"inline_keyboard": [[{"text": "📦 Я забрал(а)!", "callback_data": "user_received_order"}]]}
                    TelegramClient.send_message(int(order['user_tg_id']), user_msg, reply_markup=kb)
                    TelegramClient.send_message(chat_id, f"✅ Клиент оповещен ({token})")
            return

        # --- ПОЛЬЗОВАТЕЛЬ ---
        
        # 1. Start / Auth
        if text.startswith('/start'):
            params = text.split()
            if len(params) > 1 and params[1].startswith('auth_'):
                token = params[1].replace('auth_', '')
                if Database.update_order(order_token=token, user_tg_id=str(chat_id), status=DatabaseParams.ST_PENDING_AUTH):
                    kb = {"keyboard": [[{"text": "📱 Отправить номер", "request_contact": True}]], "one_time_keyboard": True, "resize_keyboard": True}
                    TelegramClient.send_message(chat_id, "👋 Привет! Для заказа нужен ваш номер.", reply_markup=kb)
                else:
                    TelegramClient.send_message(chat_id, "⚠️ Ссылка устарела.")
            else:
                TelegramClient.send_message(chat_id, f"👋 Поддержка: {Config.ADMIN_SUPPORT_USERNAME}")
            return

        # Ищем заказ
        order = Database.get_order(tg_id=chat_id)
        if not order:
            if 'contact' in msg or (text and text.replace('+', '').isdigit()):
                 TelegramClient.send_message(chat_id, f"⚠️ Нет активного заказа. Оформите на {Config.SITE_URL}")
            return

        st = order['status']
        token = order['order_token']

        # 2. Телефон
        if st == DatabaseParams.ST_PENDING_AUTH:
            phone = msg.get('contact', {}).get('phone_number') or text
            phone = re.sub(r'[^\d+]', '', phone)
            if len(phone) >= 10:
                Database.update_order(token, phone_number=phone, status=DatabaseParams.ST_PENDING_NAME)
                TelegramClient.send_message(chat_id, "✅ Пришлите **ФИО** одним сообщением.", reply_markup={"remove_keyboard": True})
            else:
                TelegramClient.send_message(chat_id, "⚠️ Нужен корректный номер.")
            return

        # 3. ФИО
        if st == DatabaseParams.ST_PENDING_NAME:
            if len(text.split()) < 2: 
                TelegramClient.send_message(chat_id, "⚠️ Введите Фамилию и Имя.")
                return
            Database.update_order(token, full_name=text, status=DatabaseParams.ST_PENDING_ADDR)
            TelegramClient.send_message(chat_id, "📍 Введите **Адрес** (Город, Улица, Дом).")
            return

        # 4. Адрес -> Оплата
        if st == DatabaseParams.ST_PENDING_ADDR:
            if len(text) < 5: return
            Database.update_order(token, address=text, delivery_type='СДЭК', status=DatabaseParams.ST_PENDING_PAY)
            
            pay_msg = (
                f"💳 **К оплате: {order['total_amount']:.2f} ₽**\n\n"
                f"🟢 Сбер: `{Config.CARDS['sber']}`\n"
                f"🟡 Т-Банк: `{Config.CARDS['tbank']}`\n"
                f"🔴 Альфа: `{Config.CARDS['alfa']}`\n\n"
                f"📎 **Пришлите ФАЙЛ/СКРИН чека.**"
            )
            TelegramClient.send_message(chat_id, pay_msg)
            return

        # 5. Чек
        if st == DatabaseParams.ST_PENDING_PAY:
            if 'document' in msg or 'photo' in msg:
                Database.update_order(token, status=DatabaseParams.ST_AWAIT_ADMIN)
                # Уведомляем админа + форвард чека
                BotLogic.notify_admin_new_order(order)
                TelegramClient.forward_message(chat_id, msg['message_id'], Config.TG_ADMIN_GROUP_ID)
                TelegramClient.send_message(chat_id, "✅ Чек получен! Ждем подтверждения админа.")
            return

        # 6. Отзыв
        if st == DatabaseParams.ST_REVIEW:
            if Config.TG_REVIEWS_CHANNEL_ID:
                TelegramClient.forward_message(chat_id, msg['message_id'], Config.TG_REVIEWS_CHANNEL_ID)
            Database.update_order(token, status=DatabaseParams.ST_COMPLETED)
            TelegramClient.send_message(chat_id, "🖤 Спасибо за заказ!")
            return

# --- 6. WSGI APPLICATION (Entry Point) ---
def application(environ, start_response):
    # Инициализация БД при первом запуске воркера
    if not Database._pool:
        try:
            Database.initialize()
        except Exception:
            start_response('500 Internal Server Error', Config.CORS_HEADERS)
            return [b'DB Init Failed']

    method = environ.get('REQUEST_METHOD', 'GET')
    path = environ.get('PATH_INFO', '/')

    # Health Check
    if path == '/' and method == 'GET':
        start_response('200 OK', [('Content-Type', 'text/plain')])
        return [b"Bot Running optimized."]

    # OPTIONS (CORS)
    if method == 'OPTIONS':
        start_response('200 OK', Config.CORS_HEADERS)
        return [b'']

    # Инициализация заказа (с сайта)
    if method == 'POST' and path == '/init-auth':
        try:
            body_size = int(environ.get('CONTENT_LENGTH', 0))
            data = json.loads(environ['wsgi.input'].read(body_size))
            
            token = uuid.uuid4().hex[:12]
            # Быстрая запись в БД
            Database.save_draft(token, data.get('items', []), data.get('total_amount', 0))
            
            bot_link = f"https://t.me/{os.environ.get('TELEGRAM_BOT_USERNAME', 'bot')}?start=auth_{token}"
            resp = json.dumps({'success': True, 'telegram_bot_url': bot_link}).encode('utf-8')
            
            start_response('200 OK', Config.CORS_HEADERS + [('Content-Type', 'application/json')])
            return [resp]
        except Exception as e:
            logger.error(f"/init-auth error: {e}")
            start_response('500 Error', Config.CORS_HEADERS)
            return [b'Server Error']

    # Webhook от Telegram
    if method == 'POST' and path == '/webhook':
        try:
            body_size = int(environ.get('CONTENT_LENGTH', 0))
            update = json.loads(environ['wsgi.input'].read(body_size))
            
            # Вся логика теперь тут
            BotLogic.handle_update(update)
            
            start_response('200 OK', [('Content-Type', 'text/plain')])
            return [b'OK']
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            # Телеграму всегда отвечаем 200, иначе он будет долбить повторами
            start_response('200 OK', [('Content-Type', 'text/plain')])
            return [b'OK']

    start_response('404 Not Found', [('Content-Type', 'text/plain')])
    return [b'Not Found']
