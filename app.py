import hashlib
import hmac
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("arenz")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
APP_SECRET = os.getenv("APP_SECRET", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
GRAPH_API_VERSION = "v26.0"
user_sessions = {}


class LeadStore:
    def __init__(self, path): self.path = path

    def _connect(self):
        parent = os.path.dirname(self.path)
        if parent: os.makedirs(parent, exist_ok=True)
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE IF NOT EXISTS leads (phone TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, interest TEXT NOT NULL, conversation TEXT NOT NULL, status TEXT NOT NULL, next_action TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS processed_messages (message_id TEXT PRIMARY KEY)")
        return db

    def claim_message(self, message_id):
        if not message_id: return True
        db = self._connect()
        try:
            try:
                db.execute("INSERT INTO processed_messages (message_id) VALUES (?)", (message_id,))
                db.commit()
                return True
            except sqlite3.IntegrityError:
                return False
        finally:
            db.close()

    def upsert_lead(self, phone, session, inbound, reply):
        now = datetime.now(timezone.utc).isoformat()
        interest = " | ".join(v for v in (session.get("intent"), session.get("district"), session.get("budget"), session.get("bedrooms")) if v) or "consulta inmobiliaria"
        status = "pendiente_asesor" if session.get("step") == "done" else "en_calificacion"
        next_action = "Contactar al lead" if status == "pendiente_asesor" else "Continuar la calificación por WhatsApp"
        conversation = json.dumps({"last_user_message": inbound, "last_assistant_message": reply}, ensure_ascii=False)
        db = self._connect()
        try:
            db.execute("INSERT INTO leads (phone,created_at,updated_at,interest,conversation,status,next_action) VALUES (?,?,?,?,?,?,?) ON CONFLICT(phone) DO UPDATE SET updated_at=excluded.updated_at,interest=excluded.interest,conversation=excluded.conversation,status=excluded.status,next_action=excluded.next_action", (phone, now, now, interest, conversation, status, next_action))
            db.commit()
        finally:
            db.close()

    def get_lead(self, phone):
        db = self._connect()
        try: row = db.execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
        finally: db.close()
        return dict(row) if row else None


class SupabaseLeadStore:
    """Small REST adapter for durable MVP lead storage."""
    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def claim_message(self, message_id):
        if not message_id:
            return True
        response = requests.post(
            f"{self.url}/rest/v1/processed_messages?on_conflict=message_id",
            headers={**self.headers, "Prefer": "resolution=ignore-duplicates,return=representation"},
            json={"message_id": message_id},
            timeout=10,
        )
        response.raise_for_status()
        return bool(response.json())

    def upsert_lead(self, phone, session, inbound, reply):
        now = datetime.now(timezone.utc).isoformat()
        interest = " | ".join(v for v in (session.get("intent"), session.get("district"), session.get("budget"), session.get("bedrooms")) if v) or "consulta inmobiliaria"
        status = "pendiente_asesor" if session.get("step") == "done" else "en_calificacion"
        next_action = "Contactar al lead" if status == "pendiente_asesor" else "Continuar la calificación por WhatsApp"
        payload = {"phone": phone, "created_at": now, "updated_at": now, "interest": interest, "conversation": {"last_user_message": inbound, "last_assistant_message": reply}, "status": status, "next_action": next_action}
        response = requests.post(f"{self.url}/rest/v1/leads?on_conflict=phone", headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"}, json=payload, timeout=10)
        response.raise_for_status()

    def get_lead(self, phone):
        response = requests.get(f"{self.url}/rest/v1/leads", headers=self.headers, params={"phone": f"eq.{phone}", "select": "*"}, timeout=10)
        response.raise_for_status()
        rows = response.json()
        return rows[0] if rows else None


def get_lead_store():
    supabase_url, supabase_key = os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")
    if supabase_url and supabase_key:
        return SupabaseLeadStore(supabase_url, supabase_key)
    return LeadStore(os.getenv("LEADS_DB_PATH", "leads.db"))


@app.route("/", methods=["GET"])
def home():
    return '<html lang="es"><head><title>Arenz | Servicios Inmobiliarios</title></head><body><h1>Arenz</h1><h2>Servicios Inmobiliarios</h2><p>Arenz brinda orientación y atención inmobiliaria.</p><p><a href="/privacy">Política de Privacidad</a></p><p>© 2026 Arenz</p></body></html>', 200


@app.route("/privacy", methods=["GET"])
def privacy():
    return '<html><head><title>Política de Privacidad - ARENZ</title></head><body><h1>Política de Privacidad de ARENZ</h1><p>ARENZ utiliza WhatsApp para atender consultas inmobiliarias.</p><p>Los datos se usan únicamente para atender consultas y mejorar la atención.</p><p>ARENZ no vende ni comercializa información personal.</p><p>Los usuarios pueden solicitar eliminación de datos contactando a ARENZ.</p><p>Última actualización: agosto de 2026.</p></body></html>', 200


@app.route("/data-deletion", methods=["GET"])
def data_deletion():
    return '<html><head><title>Eliminación de Datos - ARENZ</title></head><body><h1>Solicitud de eliminación de datos</h1><p>Comunícate con ARENZ indicando tu nombre y número de WhatsApp.</p><p>Correo de contacto: arenz2026@gmail.com</p></body></html>', 200


@app.route("/health", methods=["GET"])
def health():
    try:
        get_lead_store()._connect().close(); db_ok = True
    except sqlite3.Error: db_ok = False
    ok = all((VERIFY_TOKEN, WHATSAPP_TOKEN, PHONE_NUMBER_ID, APP_SECRET)) and db_ok
    return jsonify({"status": "ok" if ok else "degraded"}), 200 if ok else 503


def generate_reply(sender, text):
    """Existing deterministic qualification flow, retained as fallback."""
    text = text.lower().strip()
    session = user_sessions.get(sender, {"step":"start", "intent":None, "district":None, "budget":None, "bedrooms":None})
    if session["step"] == "start":
        session["step"] = "intent"; user_sessions[sender] = session
        return "Hola 👋 Soy ARENZ AI.\n\n¿Qué estás buscando?\n1️⃣ Comprar\n2️⃣ Alquilar\n3️⃣ Vender\n4️⃣ Hablar con un asesor" if any(w in text for w in ("hola", "buenos días", "buenas tardes", "buenas noches")) else "Hola 👋 Soy ARENZ AI. Dime si deseas comprar, alquilar, vender o hablar con un asesor."
    if session["step"] == "intent":
        for intent, words in (("compra",("1","compr")), ("alquiler",("2","alquil")), ("venta",("3","vend"))):
            if any(w in text for w in words):
                session.update(intent=intent, step="district"); user_sessions[sender] = session
                return "Perfecto. ¿En qué distrito o zona estás interesado?"
        if any(w in text for w in ("4","asesor","humano")):
            session.update(intent="asesor", step="done"); user_sessions[sender] = session
            return "Perfecto. Registraré tu solicitud para que un asesor de ARENZ continúe contigo."
        return "Indica una opción: 1 Comprar, 2 Alquilar, 3 Vender o 4 Hablar con un asesor."
    if session["step"] == "district":
        session.update(district=text, step="budget"); user_sessions[sender] = session; return "¿Cuál es tu presupuesto aproximado?"
    if session["step"] == "budget":
        session.update(budget=text, step="bedrooms"); user_sessions[sender] = session; return "¿Cuántos dormitorios necesitas?"
    if session["step"] == "bedrooms":
        session.update(bedrooms=text, step="summary"); user_sessions[sender] = session
        return f"Tengo estos datos:\nOperación: {session['intent']}\nZona: {session['district']}\nPresupuesto: {session['budget']}\nDormitorios: {session['bedrooms']}\n\n¿La información es correcta? Responde Sí o No."
    if session["step"] == "summary":
        if text in ("sí","si"):
            session["step"]="done"; user_sessions[sender]=session; return "Excelente ✅. Un asesor de ARENZ podrá continuar contigo."
        if "no" in text:
            session["step"]="intent"; user_sessions[sender]=session; return "De acuerdo. ¿Deseas comprar, alquilar o vender una propiedad?"
        return "Por favor responde Sí o No."
    return "Tu solicitud ya fue registrada. Si deseas iniciar una nueva búsqueda, escribe NUEVA BÚSQUEDA."


def generate_ai_reply(sender, text, fallback):
    if not OPENAI_API_KEY: return fallback
    payload = {"model":OPENAI_MODEL, "store":False, "max_output_tokens":220, "instructions":"Eres ARENZ AI, asistente inmobiliario en Lima. Responde en español, breve y profesional. No inventes inmuebles, precios ni disponibilidad. Conserva la intención de la respuesta base y pide solo el siguiente dato necesario.", "input":f"Mensaje del usuario: {text}\nRespuesta base: {fallback}"}
    try:
        response = requests.post("https://api.openai.com/v1/responses", headers={"Authorization":f"Bearer {OPENAI_API_KEY}","Content-Type":"application/json"}, json=payload, timeout=15)
        response.raise_for_status()
        return response.json().get("output_text", "").strip() or fallback
    except requests.HTTPError as error:
        logger.warning("AI provider rejected request: status=%s; using deterministic fallback", error.response.status_code if error.response is not None else "unknown")
        return fallback
    except (requests.RequestException, ValueError, AttributeError):
        logger.warning("AI provider unavailable; using deterministic fallback")
        return fallback


def send_whatsapp_message(to, message):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID: raise RuntimeError("WhatsApp credentials are not configured")
    response = requests.post(f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages", headers={"Authorization":f"Bearer {WHATSAPP_TOKEN}","Content-Type":"application/json"}, json={"messaging_product":"whatsapp","recipient_type":"individual","to":to,"type":"text","text":{"body":message}}, timeout=20)
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        try:
            details = response.json().get("error", {})
        except ValueError:
            details = {}
        logger.error("Graph API rejected reply: status=%s code=%s type=%s message=%s", response.status_code, details.get("code"), details.get("type"), details.get("message"))
        raise error
    logger.info("WhatsApp reply accepted by Graph API")
    return response


def valid_meta_signature(raw_body, signature):
    if not APP_SECRET or not signature or not signature.startswith("sha256="): return False
    expected = "sha256=" + hmac.new(APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    if not VERIFY_TOKEN: return "Webhook not configured", 503
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.challenge") and hmac.compare_digest(request.args.get("hub.verify_token") or "", VERIFY_TOKEN): return request.args["hub.challenge"], 200
    return "Verification failed", 403


def iter_incoming_messages(data):
    if not isinstance(data, dict) or data.get("object") != "whatsapp_business_account": return []
    return [message for entry in data.get("entry", []) for change in entry.get("changes", []) for message in change.get("value", {}).get("messages", []) if message.get("type") == "text" and message.get("from") and message.get("text", {}).get("body")]


@app.route("/webhook", methods=["POST"])
def receive_webhook():
    raw = request.get_data(cache=True)
    if not valid_meta_signature(raw, request.headers.get("X-Hub-Signature-256", "")):
        logger.warning("Rejected webhook with invalid signature"); return jsonify({"status":"invalid_signature"}), 401
    messages = iter_incoming_messages(request.get_json(silent=True))
    if not messages: return jsonify({"status":"ignored"}), 200
    store = get_lead_store()
    try:
        processed = 0
        for message in messages:
            if not store.claim_message(message.get("id")): continue
            sender, text = message["from"], message["text"]["body"]
            reply = generate_ai_reply(sender, text, generate_reply(sender, text))
            store.upsert_lead(sender, user_sessions.get(sender, {}), text, reply)
            send_whatsapp_message(sender, reply)
            processed += 1
        return jsonify({"status":"received" if processed else "duplicate", "processed":processed}), 200
    except (requests.RequestException, RuntimeError, sqlite3.Error):
        logger.exception("Webhook processing failed"); return jsonify({"status":"error"}), 500


if __name__ == "__main__": app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
