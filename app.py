import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")

GRAPH_API_VERSION = "v26.0"

user_sessions = {}

@app.route("/", methods=["GET"])
def home():
    return """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Arenz | Servicios Inmobiliarios</title>
    </head>
    <body style="font-family: Arial, sans-serif; max-width: 850px; margin: 60px auto; padding: 20px; line-height: 1.6;">
        <h1>Arenz</h1>
        <h2>Servicios Inmobiliarios</h2>

        <p>
            Arenz brinda servicios de orientación y atención inmobiliaria
            para personas interesadas en comprar, vender o alquilar propiedades.
        </p>

        <p>
            Nuestro asistente de WhatsApp facilita la atención inicial
            y permite canalizar las consultas hacia un asesor.
        </p>

        <h3>Contacto</h3>
        <p>Lima, Perú</p>

        <hr>

        <p>
            <a href="/privacy">Política de Privacidad</a>
        </p>

        <p>© 2026 Arenz</p>
    </body>
    </html>
    """, 200

@app.route("/privacy", methods=["GET"])
def privacy():
    return """
    <html>
    <head>
        <title>Política de Privacidad - ARENZ</title>
    </head>
    <body>
        <h1>Política de Privacidad de ARENZ</h1>

        <p>ARENZ utiliza WhatsApp para atender consultas inmobiliarias,
        brindar información sobre propiedades y gestionar solicitudes de clientes.</p>

        <p>Los datos proporcionados por los usuarios, como nombre, número de teléfono
        y contenido de sus mensajes, se utilizan únicamente para atender sus consultas
        y mejorar la experiencia de atención.</p>

        <p>ARENZ no vende ni comercializa la información personal de sus usuarios.</p>

        <p>Los usuarios pueden solicitar la eliminación de sus datos personales
        contactando directamente a ARENZ.</p>

        <p>Última actualización: agosto de 2026.</p>
    </body>
    </html>
    """, 200


@app.route("/data-deletion", methods=["GET"])
def data_deletion():
    return """
    <html>
    <head>
        <title>Eliminación de Datos - ARENZ</title>
    </head>
    <body>
        <h1>Solicitud de eliminación de datos</h1>

        <p>Si deseas solicitar la eliminación de tus datos personales asociados
        a ARENZ, puedes comunicarte con nosotros indicando tu nombre y número
        de teléfono utilizado en WhatsApp.</p>

        <p>Una vez recibida la solicitud, ARENZ procederá a revisar y eliminar
        los datos correspondientes cuando resulte aplicable.</p>

        <p>Correo de contacto: arenz2026@gmail.com</p>
    </body>
    </html>
    """, 200

def generate_reply(sender, text):
    text = text.lower().strip()

    session = user_sessions.get(sender, {
        "step": "start",
        "intent": None,
        "district": None,
        "budget": None,
        "bedrooms": None,
    })

    step = session["step"]

    if step == "start":
        if any(word in text for word in ["hola", "buenos días", "buenas tardes", "buenas noches"]):
            session["step"] = "intent"
            user_sessions[sender] = session

            return (
                "Hola 👋 Soy ARENZ AI, asistente inmobiliario de ARENZ.\n\n"
                "¿Qué estás buscando?\n"
                "1️⃣ Comprar un departamento\n"
                "2️⃣ Alquilar un departamento\n"
                "3️⃣ Vender una propiedad\n"
                "4️⃣ Hablar con un asesor"
            )

        session["step"] = "intent"
        user_sessions[sender] = session

        return (
            "Hola 👋 Soy ARENZ AI.\n\n"
            "Para ayudarte mejor, dime si deseas comprar, alquilar, vender "
            "una propiedad o hablar con un asesor."
        )

    if step == "intent":
        if "1" in text or "compr" in text:
            session["intent"] = "compra"
            session["step"] = "district"

        elif "2" in text or "alquil" in text:
            session["intent"] = "alquiler"
            session["step"] = "district"

        elif "3" in text or "vend" in text:
            session["intent"] = "venta"
            session["step"] = "district"

        elif "4" in text or "asesor" in text or "humano" in text:
            session["intent"] = "asesor"
            session["step"] = "done"
            user_sessions[sender] = session

            return (
                "Perfecto. Registraré tu solicitud para que un asesor de ARENZ "
                "pueda continuar contigo."
            )

        else:
            return (
                "Por favor indícame una opción:\n"
                "1️⃣ Comprar\n"
                "2️⃣ Alquilar\n"
                "3️⃣ Vender\n"
                "4️⃣ Hablar con un asesor"
            )

        user_sessions[sender] = session

        return "Perfecto. ¿En qué distrito o zona estás interesado?"

    if step == "district":
        session["district"] = text
        session["step"] = "budget"
        user_sessions[sender] = session

        return "¿Cuál es tu presupuesto aproximado?"

    if step == "budget":
        session["budget"] = text
        session["step"] = "bedrooms"
        user_sessions[sender] = session

        return "¿Cuántos dormitorios necesitas?"

    if step == "bedrooms":
        session["bedrooms"] = text
        session["step"] = "summary"
        user_sessions[sender] = session

        return (
            "Perfecto. Tengo estos datos:\n\n"
            f"Operación: {session['intent']}\n"
            f"Zona: {session['district']}\n"
            f"Presupuesto: {session['budget']}\n"
            f"Dormitorios: {session['bedrooms']}\n\n"
            "¿La información es correcta? Responde Sí o No."
        )

    if step == "summary":
        if "sí" in text or "si" == text:
            session["step"] = "done"
            user_sessions[sender] = session

            return (
                "Excelente ✅. Ya tengo tu requerimiento.\n\n"
                "Un asesor de ARENZ podrá continuar contigo con opciones "
                "acordes a tu búsqueda."
            )

        if "no" in text:
            session["step"] = "intent"
            user_sessions[sender] = session

            return (
                "De acuerdo. Empecemos nuevamente.\n\n"
                "¿Deseas comprar, alquilar o vender una propiedad?"
            )

        return "Por favor responde Sí o No."

    return (
        "Tu solicitud ya fue registrada. "
        "Si deseas iniciar una nueva búsqueda, escribe NUEVA BÚSQUEDA."
    )
    
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200

    return "Verification failed", 403


def send_whatsapp_message(to, message):
    url = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    )

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {
            "body": message
        },
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=20
    )

    print("Respuesta de Meta:")
    print(response.status_code)
    print(response.text)

    return response
    
@app.route("/webhook", methods=["POST"])
def receive_webhook():
    data = request.get_json(silent=True)

    print("Webhook recibido:")
    print(data)

    try:
        entry = data["entry"][0]
        change = entry["changes"][0]
        value = change["value"]

        messages = value.get("messages")

        if not messages:
            return jsonify({"status": "ignored"}), 200

        incoming_message = messages[0]

        if incoming_message.get("type") != "text":
            return jsonify({"status": "unsupported_message_type"}), 200

        sender = incoming_message["from"]
        text = incoming_message["text"]["body"]

        print(f"Mensaje recibido de {sender}: {text}")

        reply = generate_reply(sender, text)

        send_whatsapp_message(sender, reply)

        return jsonify({"status": "received"}), 200

    except Exception as error:
        print("Error procesando webhook:")
        print(error)

        return jsonify({"status": "error"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
