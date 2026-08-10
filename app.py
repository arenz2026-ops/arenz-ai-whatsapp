import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")

GRAPH_API_VERSION = "v26.0"


@app.route("/", methods=["GET"])
def home():
    return "ARENZ AI WhatsApp Webhook activo", 200


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

        reply = (
            "Hola 👋 Soy ARENZ AI, asistente inmobiliario de ARENZ. "
            "He recibido tu mensaje correctamente. "
            "En breve podré ayudarte a encontrar el departamento "
            "que mejor se adapte a lo que estás buscando."
        )

        send_whatsapp_message(sender, reply)

        return jsonify({"status": "received"}), 200

    except Exception as error:
        print("Error procesando webhook:")
        print(error)

        return jsonify({"status": "error"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
