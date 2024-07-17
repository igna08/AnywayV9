from gc import get_count
import uuid
from flask import Flask, request, jsonify, render_template, send_from_directory, session, make_response
from flask_cors import CORS
import openai
import os
import requests
from bs4 import BeautifulSoup
import spacy
import sqlite3
from datetime import datetime, timedelta, timezone
import psycopg2


# Configuración inicial


app = Flask(__name__)
app.secret_key = os.urandom(24)
CORS(app, resources={r"/*": {"origins": "*"}})
app.config['DEBUG'] = True

# Cargar el modelo de lenguaje en español
nlp = spacy.load("es_core_news_md")

# Configuración de tokens de acceso
openai.api_key = os.getenv('OPENAI_API_KEY')  # Asegúrate de configurar tu variable de entorno
access_token = os.getenv('ACCESS_TOKEN')
verify_token = os.getenv('VERIFY_TOKEN')
phone_number_id = os.getenv('PHONE_NUMBER_ID')
WEBHOOK_VERIFY_TOKEN = os.getenv('WEBHOOK_VERIFY_TOKEN')


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
            timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
            end_timestamp TIMESTAMP WITH TIME ZONE
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
    ''', (user_id, datetime.utcnow()))
    
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
    ''', (datetime.utcnow(), conversation_id))
    
    conn.commit()
    cur.close()
    conn.close()

def update_counts():
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Obtén la fecha y hora actual en UTC
    now = datetime.now(timezone.utc)
    today = now.date()
    month = today.month

    # Actualizar conteo diario
    cur.execute('''
        INSERT INTO daily_counts (date, count)
        VALUES (%s, 1)
        ON CONFLICT (date) 
        DO UPDATE SET count = daily_counts.count + 1
    ''', (today,))
    
    # Actualizar conteo mensual
    cur.execute('''
        INSERT INTO monthly_counts ( month, count)
        VALUES (%s, %s, 1)
        ON CONFLICT (year, month) 
        DO UPDATE SET count = monthly_counts.count + 1
    ''', ( month))
    
    conn.commit()
    cur.close()
    conn.close()

def process_message(user_id, message):
    conversation = get_current_conversation(user_id)
    current_time = datetime.utcnow()
    
    if conversation:
        conversation_id, start_time = conversation
        
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
    else:
        session['user_id'] = user_id

@app.after_request
def set_user_id_cookie(response):
    if 'user_id' in session:
        response.set_cookie('user_id', session['user_id'])
    return response

@app.route("/")
def home():
    return render_template("index.html")

@app.route('/webhook', methods=['GET', 'POST', 'OPTIONS'])
def webhook():
    if request.method == 'OPTIONS':
        return '', 200  # Responder exitosamente a las solicitudes OPTIONS
    
    if request.method == 'GET':
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == verify_token:
            return challenge, 200
        else:
            return "Verification token mismatch", 403

    elif request.method == 'POST':
        data = request.get_json()
        if 'object' in data:
            if data['object'] == 'whatsapp_business_account':
                for entry in data['entry']:
                    for change in entry['changes']:
                        if 'messages' in change['value']:
                            for message in change['value']['messages']:
                                handle_whatsapp_message(message)
            elif data['object'] == 'instagram':
                for entry in data['entry']:
                    for change in entry['changes']:
                        if 'messaging' in change['value']:
                            for message in change['value']['messaging']:
                                handle_instagram_message(message)
            elif data['object'] == 'page':
                for entry in data['entry']:
                    for messaging_event in entry['messaging']:
                        handle_messenger_message(messaging_event)
        return "Event received", 200

def handle_whatsapp_message(message):
    user_id = message['from']
    user_text = message['text']['body']
    response_text, products = process_user_input(user_text)
    send_whatsapp_message(user_id, response_text, products)

def handle_instagram_message(message):
    user_id = message['sender']['id']
    user_text = message['message']['text']
    response_text, products = process_user_input(user_text)
    send_instagram_message(user_id, response_text, products)

def handle_messenger_message(message):
    user_id = message['sender']['id']
    user_text = message['message']['text']
    response_text, products = process_user_input(user_text)
    send_messenger_message(user_id, response_text, products)

def send_whatsapp_message(user_id, text, products):
    url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    if products:
        sections = [
            {
                "title": "Productos",
                "rows": [{"id": product['id'], "title": product['title'], "description": product['subtitle']} for product in products]
            }
        ]
        data = {
            "messaging_product": "whatsapp",
            "to": user_id,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": text},
                "action": {
                    "button": "Ver productos",
                    "sections": sections
                }
            }
        }
    else:
        data = {
            "messaging_product": "whatsapp",
            "to": user_id,
            "type": "text",
            "text": {"body": text}
        }
    requests.post(url, headers=headers, json=data)

def send_instagram_message(user_id, text, products):
    url = f"https://graph.facebook.com/v12.0/me/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    elements = [
        {
            "title": product['title'],
            "subtitle": product['subtitle'],
            "image_url": product.get('image_url', 'default-image.jpg'),
            "buttons": [
                {
                    "type": "web_url",
                    "url": product['url'],
                    "title": "Ver más"
                }
            ]
        } for product in products
    ]
    data = {
        "recipient": {"id": user_id},
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "generic",
                    "elements": elements
                }
            }
        }
    }
    requests.post(url, headers=headers, json=data)

def send_messenger_message(user_id, text, products):
    url = f"https://graph.facebook.com/v19.0/me/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    elements = [
        {
            "title": product['title'],
            "subtitle": product['subtitle'],
            "image_url": product.get('image_url', 'default-image.jpg'),
            "buttons": [
                {
                    "type": "web_url",
                    "url": product['url'],
                    "title": "Ver más"
                }
            ]
        } for product in products
    ]
    data = {
        "recipient": {"id": user_id},
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "generic",
                    "elements": elements
                }
            }
        }
    }
    requests.post(url, headers=headers, json=data)

@app.route('/chat', methods=['POST'])
def chatbot():
    data = request.get_json()
    user_input = data.get('message')
    
    if user_input:
        # Obtener el user_id de la sesión
        if 'user_id' not in session:
            session['user_id'] = str(uuid.uuid4())
        user_id = session['user_id']
        
        # Procesar el mensaje del usuario
        response = process_message(user_id, user_input)
        
        # Generar la respuesta del chatbot
        response_data = process_user_input(user_input)
        
        # Combinar la respuesta con el ID de la conversación
        return jsonify({
            'response': response_data['response'],
            'conversation_id': response
        })
    else:
        return jsonify({'error': 'No message provided'}), 400

def process_user_input(user_input):
    if 'messages' not in session:
        session['messages'] = []
        session['has_greeted'] = True  # Estado de saludo



    # Si es la primera interacción y el saludo no ha sido dado
    if  session['has_greeted']:
        session['messages'].append({"role": "system", "content": (
            "Cualquier pregunta especifica de un producto como precios, caracteristicas,variantes ,etc;responde al usuario que escriba el nombre del producto o Estoy buscando....., quiero un.... , necesito..... y que tu te pondras en acción para proveerle los mejores productos a su busqueda"
            "Hello! How can I assist you today?"
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
        )})
        session['has_greeted'] = True  # Marcar que se ha saludado
    
    session['messages'].append({"role": "user", "content": user_input})
    
    try:
        if is_product_search_intent(user_input):
            product_name = extract_product_name(user_input)
            print(f"Nombre del producto extraído: {product_name}")  # Mensaje de depuración
            bot_message = search_product_on_anyway(product_name)
        else:
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo-0125",
                messages=session['messages'],
                temperature=0.1  # Ajusta la temperatura aquí
            )
            bot_message = {"response": response.choices[0].message['content'].strip()}
            session['messages'].append({"role": "assistant", "content": bot_message['response']})
        
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
    product_name_str = " ".join(product_name).strip()
    print(f"Nombre del producto extraído en 'extract_product_name': {product_name_str}")  # Mensaje de depuración
    return product_name_str

def search_product_on_anyway(product_name):
    search_url = f'https://tienda.anywayinsumos.com.ar/busqueda?controller=search&order=product.position.desc&s={product_name}'
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    try:
        response = requests.get(search_url, headers=headers)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')
        productos = []

        # Ajuste de selectores basado en el HTML proporcionado
        product_elements = soup.find_all('article', class_='product-miniature')

        for product in product_elements:
            # Extraer título
            title_elem = product.find('h3', class_='h3 product-title')
            title = title_elem.get_text(strip=True) if title_elem else 'No Title'

            # Extraer precio
            price_elem = product.find('span', class_='product-price')
            price = price_elem.get_text(strip=True) if price_elem else 'No Price'

            # Extraer imagen
            img_elem = product.find('img', class_='img-fluid product-thumbnail-first')
            img_src = img_elem['src'] if img_elem else 'No Image'

            # Extraer link
            link_elem = product.find('a', class_='thumbnail')
            link = link_elem['href'] if link_elem else 'No Link'

            productos.append({
                'titulo': title,
                'precio': price,
                'link': link,
                'imagen': img_src
            })
        
        return productos
    
    except requests.exceptions.HTTPError as err:
        return {"error": str(err)}
    
    except requests.RequestException as e:
        return {"response": f"Error al buscar productos: {e}"}

    except Exception as e:
        return {"response": f"Ocurrió un error inesperado: {str(e)}"}

# Prueba de la función
productos = search_product_on_anyway('celular')
for producto in productos:
    print(producto)
    


@app.route('/search_product', methods=['POST'])
def search_product():
    data = request.json
    product_name = data.get('product_name')
    if not product_name:
        return jsonify({"error": "No se proporcionó el nombre del producto"}), 400

    productos = search_product_on_anyway(product_name)
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
