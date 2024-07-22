import uuid
from flask import Flask, json, request, jsonify, render_template, send_from_directory, session, make_response
from flask_cors import CORS
import openai
import os
import psycopg2
import requests
from bs4 import BeautifulSoup
import spacy
import sqlite3
from datetime import datetime, timedelta, timezone

# Configuración inicial
app = Flask(__name__)
app.secret_key = os.urandom(24)
CORS(app, resources={r"/*": {"origins": "*"}})
app.config['DEBUG'] = True

# Cargar el modelo de lenguaje en español
nlp = spacy.load("es_core_news_md")

# Configuración de tokens de acceso
openai.api_key = os.getenv('OPENAI_API_KEY')  # Asegúrate de configurar tu variable de entorno
ACCESS_TOKEN = os.getenv('ACCESS_TOKEN')
verify_token = os.getenv('VERIFY_TOKEN')
PHONE_NUMBER_ID = os.getenv('PHONE_NUMBER_ID')
WEBHOOK_VERIFY_TOKEN = os.getenv('WEBHOOK_VERIFY_TOKEN')
WHATSAPP_API_URL= os.getenv('WHATSAPP_API_URL')

@app.route("/")
def home():
    return render_template("index.html")

DATABASE_URL = os.getenv('DATABASE_URL')

def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        print("Conexión a la base de datos exitosa.")
        return conn
    except Exception as e:
        print(f"Error al conectar a la base de datos: {e}")
        raise

def create_tables_if_not_exists():
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Crear tabla de conversaciones
    cur.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
            id SERIAL PRIMARY KEY,
            user_id UUID NOT NULL,
            timestamp TIMESTAMPTZ NOT NULL,
            end_timestamp TIMESTAMPTZ
        )
    ''')

    # Crear tabla de conteo diario
    cur.execute('''
        CREATE TABLE IF NOT EXISTS daily_counts (
            date DATE PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 0
        )
    ''')

    # Crear tabla de conteo mensual
    cur.execute('''
        CREATE TABLE IF NOT EXISTS monthly_counts (
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (year, month)
        )
    ''')
    
    conn.commit()
    cur.close()
    conn.close()

def create_new_conversation(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Crear una nueva conversación
    cur.execute('''
        INSERT INTO conversations (user_id, timestamp)
        VALUES (%s, %s) RETURNING id
    ''', (user_id, datetime.now(timezone.utc)))
    
    conversation_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    
    return conversation_id

def get_current_conversation(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Buscar la última conversación activa para el usuario
    cur.execute('''
        SELECT id, timestamp FROM conversations 
        WHERE user_id = %s AND end_timestamp IS NULL
        ORDER BY timestamp DESC LIMIT 1
    ''', (user_id,))
    
    conversation = cur.fetchone()
    
    cur.close()
    conn.close()
    
    return conversation

def end_conversation(conversation_id):
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Finalizar la conversación
    cur.execute('''
        UPDATE conversations
        SET end_timestamp = %s
        WHERE id = %s
    ''', (datetime.now(timezone.utc), conversation_id))
    
    conn.commit()
    cur.close()
    conn.close()

def update_counts():
    conn = get_db_connection()
    cur = conn.cursor()
    
    now = datetime.now(timezone.utc)
    today = now.date()
    month = today.month
    year = today.year

    # Actualizar conteo diario
    cur.execute('''
        INSERT INTO daily_counts (date, count)
        VALUES (%s, 1)
        ON CONFLICT (date) 
        DO UPDATE SET count = daily_counts.count + 1
    ''', (today,))
    
    # Actualizar conteo mensual
    cur.execute('''
        INSERT INTO monthly_counts (year, month, count)
        VALUES (%s, %s, 1)
        ON CONFLICT (year, month) 
        DO UPDATE SET count = monthly_counts.count + 1
    ''', (year, month))
    
    conn.commit()
    cur.close()
    conn.close()

def process_message(user_id, message):
    conversation = get_current_conversation(user_id)
    current_time = datetime.now(timezone.utc)  # Asegúrate de que sea offset-aware
    
    if conversation:
        conversation_id, start_time = conversation
        
        # Asegúrate de que start_time sea offset-aware
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        
        # Si la conversación es mayor a 5 minutos, ciérrala y crea una nueva
        if current_time - start_time > timedelta(minutes=5):
            end_conversation(conversation_id)
            conversation_id = create_new_conversation(user_id)
            update_counts()  # Actualiza el conteo al finalizar una conversación
        else:
            print(f"Continuando la conversación con ID: {conversation_id}")
    else:
        conversation_id = create_new_conversation(user_id)
        update_counts()  # Actualiza el conteo al iniciar una nueva conversación
        print(f"Iniciando una nueva conversación con ID: {conversation_id}")
    
    # Aquí procesarías el mensaje según sea necesario
    return f"Mensaje recibido en la conversación {conversation_id}"

@app.before_request
def ensure_user_id():
    user_id = request.cookies.get('user_id')
    if not user_id:
        user_id = str(uuid.uuid4())
        session['user_id'] = user_id
        print(f"Nuevo user_id generado: {user_id}")
    else:
        session['user_id'] = user_id
        print(f"User_id recuperado de la cookie: {user_id}")

@app.after_request
def set_user_id_cookie(response):
    if 'user_id' in session:
        response.set_cookie('user_id', session['user_id'])
    return response


@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')

        if mode == 'subscribe' and token == os.getenv('VERIFY_TOKEN'):
            return challenge, 200
        else:
            return 'Forbidden', 403

    if request.method == 'POST':
        data = request.get_json()
        print(data)  # Depuración

        if data.get('object') == 'whatsapp_business_account':
            for entry in data.get('entry', []):
                for change in entry.get('changes', []):
                    value = change.get('value', {})
                    messages = value.get('messages', [])
                    for message in messages:
                        if message.get('type') == 'text':
                            phone_number = message['from']
                            user_input = message['text']['body']
                            response = process_user_input(user_input)
                            if 'carousel' in response:
                                send_whatsapp_carousel(phone_number, response['carousel'])
                            else:
                                send_whatsapp_message(phone_number, response['response'])
        return 'EVENT_RECEIVED', 200




def send_whatsapp_message(to, message):
    url = f"{WHATSAPP_API_URL}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {
            "body": str(message)  # Asegurarse de que message sea una cadena
        }
    }
    response = requests.post(url, headers=headers, json=data)
    print(f"Enviando mensaje a {to}: {message}")  # Depuración
    print(f"Datos enviados: {data}")  # Depuración
    print(f"Respuesta de la API: {response.json()}")  # Depuración
    return response.json()

def send_whatsapp_carousel(to, products):
    url = f"{WHATSAPP_API_URL}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    elements = [
        {
            "title": product['title'],
            "image_url": f"https:{product['image_url']}",  # Asegúrate de que la URL de la imagen sea completa
            "subtitle": product['subtitle'],
            "default_action": {
                "type": "web_url",
                "url": product['default_action']['url'],
                "webview_height_ratio": "tall",
            },
            "buttons": [
                {
                    "type": "web_url",
                    "url": product['default_action']['url'],
                    "title": "Ver Producto"
                }
            ]
        }
        for product in products
    ]

    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {
                "type": "text",
                "text": "Productos Disponibles"
            },
            "body": {
                "text": "Selecciona un producto para obtener más información."
            },
            "action": {
                "button": "Ver Productos",
                "sections": [
                    {
                        "title": "Productos",
                        "rows": elements
                    }
                ]
            }
        }
    }

    response = requests.post(url, headers=headers, json=data)
    print(f"Datos enviados: {data}")  # Depuración
    print(f"Respuesta de la API: {response.json()}")  # Depuración
    return response.json()



@app.route('/chat', methods=['POST'])
def chatbot():
    try:
        # Recuperar o crear un user_id
        user_id = request.cookies.get('user_id')
        if not user_id:
            user_id = str(uuid.uuid4())
            response = make_response()
            response.set_cookie('user_id', user_id, max_age=60*60*24*365*2)
        else:
            response = make_response()

        # Obtener el input del usuario desde el JSON de la solicitud
        user_input = request.json.get('input')

        # Procesar el input del usuario
        response_data = process_user_input(user_id, user_input)

        # Verificar si la respuesta contiene la clave 'respuesta'
        if 'respuesta' not in response_data:
            return 'Error: la respuesta no contiene la clave esperada.', 500

        # Preparar la respuesta JSON
        response_json = {
            'respuesta': response_data['respuesta']
        }

        # Configurar la respuesta HTTP
        response.data = json.dumps(response_json)
        response.content_type = 'application/json'
        return response
    except KeyError as e:
        print(f"KeyError: {e}")
        return 'Error en la solicitud.', 400
    except Exception as e:
        print(f"Error procesando la solicitud: {e}")
        return 'Error procesando la solicitud.', 500




def get_initial_context():
    return (
        "You are an assistant at Surcan, a Family company located in the heart of Apóstoles, city of Misiones with more than 40 years of experience in the construction field. "
        "Be kind and friendly. Somos una empresa Familiar ubicada en el corazón de Apóstoles, ciudad de Misiones con más de 40 años de experiencia en el rubro de la construcción. "
        "Contamos con equipos capacitados y especializados en distintas áreas para poder asesorar a nuestros clientes de la mejor manera. "
        "Trabajamos con múltiples marcas, Nacionales como Internacionales con un amplio espectro de categorías como Ferreteria, Pintureria, Sanitarios, Cocinas, Baños, Cerámicos y Guardas, Aberturas, Construcción en Seco, Siderúrgicos y otros. "
        "Visítanos o contáctanos para contarnos sobre tus proyectos y poder elaborar un presupuesto en materiales realizado por nuestros especialistas en el tema. "
        "Abierto de lunes a viernes de 7:30hs a 12hs y 15hs a 19hs. Sábados de 7:30hs a 12hs. Domingo Cerrado. "
        "INFORMACIÓN DE CONTACTO ADICIONAL: 03758 42-2637, surcan.compras@gmail.com, surcan.ventas@gmail.com "
        "Normalmente respondemos en el transcurso del día. "
        "Política de privacidad: Surcan S.A. asume la responsabilidad y obligación de las normas de la privacidad respecto a todo tipo de transacción en sus sitios web y en las diferentes espacios y links que lo componen. "
        "Surcan SA tiene como principal estandarte la protección de los datos personales de los usuarios y consumidores que accedan a sus plataformas informáticas, buscando resguardar sus datos como así también evitar violaciones normativas sea dentro de la ley de protección de datos personales, de la ley de defensa del consumidor, como en el manejo de dichos datos, evitar fraudes, estafas, sean estos de cualquier parte, incluso de terceros. "
        "En dicho contexto todo Usuario o Consumidor que voluntariamente acceda a las páginas Web de Surcan SA o cualquiera de sus plataformas vinculadas declaran conocer de manera expresa las presentes políticas de privacidad. "
        "De igual manera se comprometen a brindar sus datos, informaciones personales y todo otro dato relativo a la operatoria o vinculación con la misma de manera fidedigna y real y expresan y otorgan su consentimiento al uso por parte de SURCAN SA de dichos datos conforme se describe en esta Política de Privacidad. "
        "No obstante, en caso de tener consultas o inquietudes al respecto, no dude en contactarnos al siguiente correo: surcan610@gmail.com. "
        "Política de reembolso: Documentación a presentar para realizar el cambio: El cliente deberá presentar la documentación correspondiente de identidad. Sólo se realizarán devoluciones con el mismo método de pago de la compra. "
        "Estado del Producto: El producto no puede estar probado y/o usado (salvo en caso de cambio por falla). Debe tener su embalaje original (incluyendo interiores), Pueden estar abiertos, pero encontrarse en perfectas condiciones, (salvo aquellos productos que tienen envases sellados como Pinturas). "
        "El producto debe estar completo, con todos sus accesorios, manuales, certificados de garantía correspondientes y con sus productos bonificados que hayan estado asociados a la compra. No debe estar vencido. "
        "Cambio por Falla: En caso de devolución/cambio por falla, el producto debe haberse utilizado correctamente. No se aceptarán devoluciones/cambios de constatarse mal uso del producto. "
        "Para herramientas eléctricas, se realizarán cambios directos dentro de las 72 hs de entregado el producto. En caso de haber pasado el plazo establecido, el cliente se debe contactar directamente con el servicio técnico oficial del producto. "
        "Plazos: Plazo Máximo: 15 días de corrido. Productos con vencimiento: 7 días de corrido. Los plazos para generar una devolución/cambio comienzan a correr a partir del día de la entrega del producto. "
        "Política de envío. Zona de Envios y Tiempos de Entrega Zonas de Envio: Las zonas cubiertas para envios de compras realizas a través de nuestro e -commerce esta limitada a Misiones y Corrientes. "
        "Los envios se realizaran através de Correo Argentino, Via Cargo, o nuestro servicio de Logística privada, de acuerdo al tipo de producto, lo seleccionado y disponible al momento de realizar el check out. "
        "Tiempos de Entrega: El tiempo de entrega planificado será informado en el checkout de acuerdo al tipo de producto seleccionado. El mismo empezará a correr a partir de haberse hecho efectivo el pago. "
        "El tiempo de aprobación del pago varía según el medio utilizado. Por último el tiempo de entrega varía dependiendo de la zona en la que usted se encuentre y del tipo de envío seleccionado. "
        "Información Importante: Estamos trabajando de acuerdo a los protocolos de salud establecidos y por razones de público conocimiento contamos con personal reducido. Los tiempos de atención y entrega podrían verse afectados. Hacemos nuestro mayor esfuerzo. "
        "INSTAGRAM: https://www.instagram.com/elijasurcan/ "
        "Datos de Contacto: Teléfono: 03758 42-2637, Consultas: surcan.ventas@gmail.com"
    )

def process_user_input(user_input):
    if 'messages' not in session:
        session['messages'] = []
        session['has_greeted'] = True  # Estado de saludo

    if not session['has_greeted']:
        session['messages'].append({"role": "system", "content": "¡Hola! Soy tu asistente virtual. ¿En qué puedo ayudarte hoy?"})
        session['has_greeted'] = True  # Marcar que se ha saludado

    # Agregar contexto a la conversación
    if len(session['messages']) == 1:  # Solo agregar el contexto si es la primera interacción después del saludo
        context = get_initial_context()
        session['messages'].append({"role": "system", "content": context})

    # Imprimir el mensaje del usuario para depuración
    print(f"Mensaje del usuario: {user_input}")

    session['messages'].append({"role": "user", "content": user_input})

    try:
        if is_product_search_intent(user_input):
            product_name = extract_product_name(user_input)
            print(f"Nombre del producto extraído: {product_name}")  # Verificar nombre del producto
            bot_message = search_product_on_surcansa(product_name)
            print(f"Mensaje del bot después de búsqueda: {bot_message}")  # Verificar el mensaje del bot

            # Verificar que el formato de la respuesta sea el esperado
            if 'carousel' in bot_message:
                return bot_message
            else:
                return {"response": bot_message.get('response', "No se encontraron productos.")}

        else:
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=session['messages'],
                temperature=0.01  # Ajusta la temperatura aquí
            )
            bot_message = {"response": response.choices[0].message['content'].strip()}
            session['messages'].append({"role": "assistant", "content": bot_message['response']})

        # Verificar y asegurar la clave 'response' en la respuesta del bot
        if 'response' not in bot_message:
            bot_message['response'] = "Lo siento, no entendí tu solicitud."

        # Agregar la respuesta del bot a la sesión
        session['messages'].append({"role": "assistant", "content": bot_message['response']})

        # Imprimir la respuesta del chatbot y el estado de la sesión
        print(f"Respuesta del chatbot: {bot_message}")
        print(f"Estado de la sesión: {session}")

        return bot_message
    except Exception as e:
        print(f"Error processing input: {str(e)}")
        return {"response": "Lo siento, hubo un problema al procesar tu solicitud."}

def is_product_search_intent(user_input):
    # Analiza el texto del usuario
    doc = nlp(user_input.lower())
    # Busca patrones en la frase que indiquen una intención de búsqueda
    for token in doc:
        if token.lemma_ in ["buscar", "necesitar", "querer"] and token.pos_ == "VERB":
            return True
    return False

def extract_product_name(user_input):
    # Analiza el texto del usuario
    doc = nlp(user_input.lower())
    product_name = []
    is_searching = False
    for token in doc:
        # Detectar la frase de búsqueda
        if token.lemma_ in ["buscar", "necesitar", "querer"] and token.pos_ == "VERB":
            is_searching = True
        # Extraer sustantivos después del verbo de búsqueda
        if is_searching and token.pos_ in ["NOUN", "PROPN"]:
            product_name.append(token.text)
    return " ".join(product_name)

def search_product_on_surcansa(product_name):
    search_url = f'https://surcansa.com.ar/search?q={product_name}'
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    try:
        response = requests.get(search_url, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        product_elements = soup.find_all('li', class_='grid__item')

        products = []
        base_url = "https://surcansa.com.ar"
        for product in product_elements:
            img_tag = product.find('img')
            img_url = img_tag['src'] if img_tag else 'No image'
            link_tag = product.find('a', class_='full-unstyled-link')
            product_name = link_tag.get_text(strip=True) if link_tag else 'No name'
            product_link = f"{base_url}{link_tag['href']}" if link_tag and link_tag['href'].startswith('/') else link_tag['href']
            price_tag = product.find('span', class_='price-item--regular')
            price = price_tag.get_text(strip=True) if price_tag else 'No price'

            product = {
                'titulo': product_name,
                'link': product_link,
                'imagen': img_url,
                'precio': price
            }
            products.append(product)
            print(f"Producto: {product_name}, Precio: {price}, Enlace: {product_link}, Imagen: {img_url}")
        
        if products:
            productos = products[:5]
            elements = []
            for producto in productos:
                elements.append({
                    "title": producto['titulo'],
                    "image_url": f"https:{producto['imagen']}",  # Asegurarse de que la URL de la imagen sea completa
                    "subtitle": producto['precio'],
                    "default_action": {
                        "type": "web_url",
                        "url": producto['link'],
                        "webview_height_ratio": "tall",
                    },
                    "buttons": [
                        {
                            "type": "web_url",
                            "url": producto['link'],
                            "title": "Ver Producto"
                        }
                    ]
                })
            return {"carousel": elements}

        else:
            return {"response": f"No encontré productos para '{product_name}'."}
    except Exception as e:
        return {"response": f"Ocurrió un error inesperado: {str(e)}"}

# Prueba de la función
#productos = search_product_on_anyway('celular')
#for producto in productos:
    print(producto)
    


@app.route('/search_product', methods=['POST'])
def search_product():
    data = request.json
    product_name = data.get('product_name')
    if not product_name:
        return jsonify({"error": "No se proporcionó el nombre del producto"}), 400

    productos = search_product_on_surcansa(product_name)
    return jsonify(productos)

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, 'static'),
        'favicon.ico',
        mimetype='image/vnd.microsoft.icon'
    )

@app.route('/reset', methods=['POST'])
def reset():
    session.pop('messages', None)
    return jsonify({'status': 'session reset'})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
