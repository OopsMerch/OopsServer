import json
import uuid
import os
import ydb 

TELEGRAM_BOT_USERNAME = 'oopsmerchbot' 

YDB_ENDPOINT = os.environ.get('YDB_ENDPOINT') 
YDB_DATABASE = os.environ.get('YDB_DATABASE') 
ORDERS_TABLE_NAME = 'orders'


def create_ydb_driver(context):
    if not YDB_ENDPOINT or not YDB_DATABASE:
        raise ValueError("YDB_ENDPOINT или YDB_DATABASE не установлены.")
        
    # В отличие от Yandex Cloud Function, Render не предоставляет токен
    # Поэтому мы используем неавторизованный драйвер для YDB, 
    # что подходит для Serverless-баз данных, если они разрешены
    driver = ydb.Driver(
        endpoint=YDB_ENDPOINT, 
        database=YDB_DATABASE
    )
    
    driver.wait(timeout=3)
    return driver

def save_order_to_db(driver, order_token, cart_data):
    
    session = driver.table_client.session().create()
    
    query = f"""
    DECLARE $order_token AS Utf8;
    DECLARE $cart_data AS Utf8;
    
    UPSERT INTO {ORDERS_TABLE_NAME} (order_token, status, cart_data)
    VALUES ($order_token, "pending_tg_auth", $cart_data);
    """
    
    prepared_query = session.prepare(query)
    
    session.transaction().execute(
        prepared_query,
        commit_tx=True,
        parameters={
            '$order_token': order_token.encode('utf-8'),
            '$cart_data': json.dumps(cart_data).encode('utf-8')
        }
    )
    session.closing()


def handler(event, context):
    
    cors_headers = {
        'Access-Control-Allow-Origin': '*', 
        'Access-Control-Allow-Methods': 'POST, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type',
    }
    
    # Render использует другой формат для входящего запроса, 
    # но логика остается той же
    if 'httpMethod' not in event:
        # Для Gunicorn/Render это нужно проверить
        return {'statusCode': 400, 'headers': cors_headers, 'body': 'Неверный формат запроса.'}
    
    if event['httpMethod'] == 'OPTIONS':
        return {
            'statusCode': 200,
            'headers': cors_headers,
            'body': ''
        }
    
    if event['httpMethod'] != 'POST':
        return {'statusCode': 405, 'headers': cors_headers, 'body': 'Метод не разрешен.'}

    try:
        cart_data = json.loads(event['body'])
    except Exception:
        return {'statusCode': 400, 'headers': cors_headers, 'body': 'Ошибка: неверный формат данных корзины.'}

    try:
        order_token = str(uuid.uuid4()).replace('-', '')[:16] 

        # На Render мы не можем использовать context для получения токена, 
        # поэтому вызываем драйвер с пустым context
        driver = create_ydb_driver(None) 
        save_order_to_db(driver, order_token, cart_data)
        driver.stop()
        
        deep_link = f'https://t.me/{TELEGRAM_BOT_USERNAME}?start={order_token}'
        
        return {
            'statusCode': 200,
            'headers': cors_headers,
            'body': json.dumps({'deep_link': deep_link})
        }

    except Exception as e:
        print(f"Ошибка выполнения: {e}")
        return {
            'statusCode': 500,
            'headers': cors_headers,
            'body': json.dumps({'error': f'Внутренняя ошибка сервера: {str(e)}'}) 
        }
      
