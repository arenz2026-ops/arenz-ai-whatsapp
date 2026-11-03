import copy
import difflib
import hashlib
import hmac
import json
import logging
import os
import re
import unicodedata
import sqlite3
import time
import uuid

from text_normalizer import closest_district, is_known_district, normalize_client_text
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, request

import advisor_notify
import embedded_signup
import inventory_staging

app = Flask(__name__)
app.register_blueprint(embedded_signup.bp)
app.register_blueprint(inventory_staging.bp)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("arenz")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
WHATSAPP_TOKEN = embedded_signup.server_token()
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
APP_SECRET = os.getenv("APP_SECRET", "").strip().strip("\"'").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
# gpt-4.1-nano was shut down by OpenAI on 2026-10-23; a retired model must not
# silently drop the bot to the scripted flow, so a model_not_found retries here.
OPENAI_FALLBACK_MODEL = os.getenv("OPENAI_FALLBACK_MODEL", "gpt-4.1-mini")
GRAPH_API_VERSION = "v26.0"
# Render injects RENDER_GIT_COMMIT. Without it a deploy can only be assumed, never
# verified, so /health reports the short SHA it is actually running.
SERVICE_COMMIT = os.getenv("RENDER_GIT_COMMIT", "")[:7]


def conversacion_ia_activa():
    """Interruptor CONVERSACION_IA (on/off, por defecto off). En off el bot conserva el comportamiento anterior."""
    return os.getenv("CONVERSACION_IA", "off").strip().casefold() in {"on", "1", "true", "si", "sí"}


logger.info("CONVERSACION_IA=%s", "on" if conversacion_ia_activa() else "off")
ASSISTANT_NAME = os.getenv("ASSISTANT_NAME", "Cecilia")
COMMERCIAL_BRAND = os.getenv("COMMERCIAL_BRAND", "C2 Inmobiliaria")
INTRODUCTION = f"Hola, soy {ASSISTANT_NAME}, asesora de {COMMERCIAL_BRAND} 😊 Te ayudo a encontrar tu departamento ideal 🏡 ¿En qué distrito o distritos lo buscas?"
user_sessions = {}

OBSERVATION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "intent": {"type": "string", "enum": ["property_search", "new_search", "general_question", "change_criteria", "human_handoff", "greeting", "confirmation", "unknown"]},
        "slot_updates": {"type": "object", "additionalProperties": False, "properties": {"operation": {"type": ["string", "null"]}, "districts": {"type": "array", "items": {"type": "string"}}, "budget_max": {"type": ["number", "null"]}, "currency": {"type": ["string", "null"]}, "bedrooms": {"type": ["integer", "null"]}, "property_type": {"type": ["string", "null"]}, "parking_required": {"type": ["boolean", "null"]}, "preferences": {"type": "array", "items": {"type": "string"}}}, "required": ["operation", "districts", "budget_max", "currency", "bedrooms", "property_type", "parking_required", "preferences"]},
        "criteria_change": {"type": "boolean"},
        "criteria_actions": {"type": "array", "items": {"type": "object", "additionalProperties": False, "properties": {"action": {"type": "string", "enum": ["ADD", "UPDATE", "REMOVE"]}, "field": {"type": "string", "enum": ["districts", "budget_max", "currency", "bedrooms", "property_type", "parking_required", "preferences"]}, "values": {"type": "array", "items": {"type": ["string", "number", "integer", "boolean"]}}}, "required": ["action", "field", "values"]}},
        "user_question": {"type": ["string", "null"]},
        "next_action": {"type": "string", "enum": ["reply", "ask_clarification", "confirm", "search_inventory", "handoff"]},
        "handoff": {"type": "boolean"},
        "assistant_reply": {"type": "string"}
    },
    "required": ["intent", "slot_updates", "criteria_change", "criteria_actions", "user_question", "next_action", "handoff", "assistant_reply"]
}

# ARENZ commercial qualification: what, where, budget, payment, and timing.
COMMERCIAL_FIELDS = {
    "payment_method": {"type": ["string", "null"], "enum": ["contado", "credito", "mixto", "por_definir", "prefiere_no_indicar", None]},
    "credit_status": {"type": ["string", "null"], "enum": ["aprobado", "en_tramite", "por_evaluar", "prefiere_no_indicar", None]},
    "purchase_timeline": {"type": ["string", "null"]},
    "area_min_m2": {"type": ["number", "null"]},
    "bathrooms_min": {"type": ["number", "null"]},
}
_slot_contract = OBSERVATION_SCHEMA["properties"]["slot_updates"]
_slot_contract["properties"].update(COMMERCIAL_FIELDS)
_slot_contract["required"].extend(COMMERCIAL_FIELDS)
OBSERVATION_SCHEMA["properties"]["criteria_actions"]["items"]["properties"]["field"]["enum"].extend(COMMERCIAL_FIELDS)

# CONVERSACION_IA=on: the same single call also classifies the reply to the bot's last question.
CUSTOMER_REPLY_TYPES = ["acepta", "rechaza", "pide_otra_cosa", "queja_repeticion", "cierra", "otra"]
CUSTOMER_REQUESTS = ["ninguno", "otras_opciones", "fotos", "opcion", "cambiar_criterios", "dato_unidad", "asesor"]
UNIT_FACTS_ASKED = ["ninguno", "precio", "area", "dormitorios", "banos", "piso", "entrega", "estacionamiento",
                    "ubicacion", "areas_comunes", "otro"]
OBSERVATION_SCHEMA_IA = copy.deepcopy(OBSERVATION_SCHEMA)
OBSERVATION_SCHEMA_IA["properties"]["respuesta_cliente"] = {
    "type": "object", "additionalProperties": False,
    "properties": {"tipo": {"type": "string", "enum": CUSTOMER_REPLY_TYPES},
                   "pedido": {"type": "string", "enum": CUSTOMER_REQUESTS},
                   "opcion": {"type": ["integer", "null"]},
                   "dato": {"type": "string", "enum": UNIT_FACTS_ASKED}},
    "required": ["tipo", "pedido", "opcion", "dato"]}
OBSERVATION_SCHEMA_IA["required"].append("respuesta_cliente")
CUSTOMER_REPLY_POLICY = (
    " respuesta_cliente: cómo responde el Mensaje a ultima_pregunta_bot. tipo: acepta (sí, dale, ya pe, simón); "
    "rechaza (no, nel, ninguna); pide_otra_cosa (pide algo distinto aunque empiece con no: otra opción, 'mejor la 2', "
    "fotos, otro distrito, 'ta caro', un dato); queja_repeticion (dice que el bot repite: 'ya me lo dijiste', 'otra "
    "vez?'); cierra (agradece o se despide: 'ok gracias', 'lo voy a pensar', 'chau'); otra (lo demás, incluidas "
    "respuestas de distrito, presupuesto, dormitorios, pago o plazo). opcion: número elegido. dato: dato de la unidad "
    "preguntado. Con ficha_proyecto y dato ubicacion, areas_comunes u otro, responde en assistant_reply con la ficha "
    "(máx. 280 caracteres). En queja_repeticion discúlpate y ofrece otro paso (otra opción, fotos o asesor). "
    "pedido es lo que el Mensaje pide, no lo que el bot ofreció: un 'no' o 'todavía no' a una oferta es rechaza con pedido "
    "ninguno. Si pregunta un dato de la unidad ('¿tiene cochera?', '¿cuánto cuesta?') es pide_otra_cosa con pedido dato_unidad. "
    "assistant_reply nunca lleva precios, montos, metrajes, unidad, piso ni disponibilidad, ni repite al bot."
)


COMMERCIAL_POLICY = (
    f" Preséntate como {ASSISTANT_NAME}, asesora de {COMMERCIAL_BRAND}, con trato cálido y sin presión. "
    "Sigue los cinco filtros QUÉ, DÓNDE, CUÁNTO, CÓMO y CUÁNDO de manera progresiva, "
    "con una sola pregunta por mensaje. Responde primero la consulta concreta; no repitas datos ya conocidos. "
    "En el primer saludo explica brevemente que ayudas a orientar su búsqueda, sin un menú numérico. "
    "QUÉ: operation, property_type, bedrooms, area_min_m2, bathrooms_min, parking_required y preferences. "
    "DÓNDE: districts y preferencias de ubicación. CUÁNTO: budget_max y currency. "
    "Aprovecha todo dato espontáneo aunque corresponda a un filtro posterior; conserva los anteriores "
    "y actualiza únicamente lo que el cliente cambie. No completes datos desconocidos por suposición. "
    "CÓMO: registra payment_method y credit_status solo si el cliente los declara; "
    "no deduzcas solvencia, aprobación de crédito ni capacidad de compra. Nunca pidas DNI, "
    "documentos bancarios ni ingresos para iniciar una búsqueda. CUÁNDO: registra purchase_timeline "
    "literalmente, incluyendo 'sin urgencia' o una negativa a indicarlo. Una negativa es válida: no insistas. "
    "Registra área mínima y baños mínimos en area_min_m2 y bathrooms_min cuando se indiquen. "
    "Si quiere consultarlo con su familia o dice que escribirá después, respeta su espacio: "
    "no añadas preguntas, no prometas seguimientos automáticos ni afirmes que hay una cita agendada. "
    "No uses ejemplos de clientes del contexto como inventario ni como datos de otro cliente. "
    "Solo presenta propiedades devueltas por el inventario autorizado; nunca inventes fotos, precios o disponibilidad. "
    "Para una visita o un asesor, registra una solicitud sin prometer una fecha confirmada. "
    "C2 Inmobiliaria solo comercializa departamentos en VENTA (flat, dúplex, loft): la operación siempre es compra y el "
    "tipo siempre es departamento; nunca preguntes si desea comprar, alquilar o vender, ni qué tipo de inmueble busca. "
    "Pide el presupuesto aclarando que puede indicarlo en soles o en dólares. "
    # Estilo ARENZ tomado de las conversaciones con leads de setiembre y octubre de 2026.
    "Escribe como una asesora por WhatsApp: cercana y cordial, con uno o dos emoticons por mensaje (😊 🏡 📍 📷 🙌), "
    "nunca junto a montos. Si el cliente no menciona un inmueble concreto, no supongas que le interesa alguno: pregunta "
    "qué busca y en qué zona. No prometas descuentos, precio de cochera ni condiciones de pago: un asesor los confirma. "
    "Si quien escribe es agente o corredor inmobiliario que busca para un cliente, trátalo como colaboración y pregunta "
    "qué departamento o zona busca su cliente. Si la respuesta a la última pregunta parece una opción mal escrita "
    "('ahorq' = 'ahora', 'contdo' = 'contado'), interprétala así; si de verdad no se entiende, dilo con amabilidad. "
    "No preguntes si necesita cochera o estacionamiento (casi siempre está por confirmar); si el cliente lo pregunta, "
    "un asesor lo confirma."
)


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
        status = "pendiente_asesor" if session.get("step") == "done" else "en_calificacion"
        next_action = "Contactar al lead" if status == "pendiente_asesor" else "Continuar la calificación por WhatsApp"
        existing = self.get_lead(phone) or {}
        incoming = {"intent": session.get("intent"), "district": session.get("district"), "budget": session.get("budget"), "bedrooms": session.get("bedrooms")}
        payload = {"phone": phone, **{field: value if value not in (None, "") else existing.get(field) for field, value in incoming.items()}, "conversation": {"last_user_message": inbound, "last_assistant_message": reply}, "status": status, "next_action": next_action, "updated_at": datetime.now(timezone.utc).isoformat()}
        response = requests.post(f"{self.url}/rest/v1/lead_profiles?on_conflict=phone", headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"}, json=payload, timeout=10)
        response.raise_for_status()

    def get_lead(self, phone):
        response = requests.get(f"{self.url}/rest/v1/lead_profiles", headers=self.headers, params={"phone": f"eq.{phone}", "select": "*"}, timeout=10)
        response.raise_for_status()
        rows = response.json()
        return rows[0] if rows else None


class ConversationMemoryStore:
    """Durable phone session pointer, search state, and turn history."""
    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def load_session(self, phone):
        response = requests.get(f"{self.url}/rest/v1/conversation_sessions", headers=self.headers, params={"phone": f"eq.{phone}", "select": "state,summary,stage"}, timeout=10)
        response.raise_for_status()
        rows = response.json()
        if not rows:
            return None
        session = rows[0]
        active_id = active_search_id(session)
        if active_id:
            search = self.load_search(active_id)
            if search:
                state = dict(session.get("state") or {})
                legacy_searches = state.get("legacy_searches", state.get("searches", {}))
                state["searches"] = dict(legacy_searches) if isinstance(legacy_searches, dict) else {}
                state["searches"][active_id] = search.get("state") or {}
                session["state"] = state
        return session

    def load_search(self, search_id):
        response = requests.get(f"{self.url}/rest/v1/conversation_searches", headers=self.headers, params={"search_id": f"eq.{search_id}", "select": "search_id,phone,operation,state,status,created_at,updated_at,closed_at"}, timeout=10)
        response.raise_for_status()
        rows = response.json()
        return rows[0] if rows else None

    def rpc(self, name, payload):
        response = requests.post(f"{self.url}/rest/v1/rpc/{name}", headers=self.headers, json=payload, timeout=15)
        response.raise_for_status()
        return response.json()

    def claim_work(self, message_id):
        rows = self.rpc("claim_processed_message", {"p_message_id": message_id})
        row = rows[0] if rows else {}
        return row.get("outcome"), row.get("claim_token")

    def finish_work(self, message_id, claim_token):
        rows = self.rpc("finish_processed_message", {"p_message_id": message_id, "p_claim_token": claim_token})
        return bool(rows and rows[0].get("finish_processed_message"))

    def commit_work(self, payload):
        """Invoke the single database transaction; never fall back to REST writes."""
        # A handoff must land in the same transaction as the turn that promised it,
        # so the escalation variant replaces the call rather than adding a write.
        name = "commit_webhook_message_with_handoff" if payload.get("p_handoff") else "commit_webhook_message"
        rows = self.rpc(name, payload)
        return rows

    def fail_work(self, message_id, claim_token, stage):
        try:
            self.rpc("fail_processed_message", {"p_message_id": message_id, "p_claim_token": claim_token, "p_stage": stage})
        except requests.RequestException:
            logger.error("Unable to record internal failure: stage=%s", stage)

    def record_delivery(self, message_id, state, code=None):
        response = requests.patch(f"{self.url}/rest/v1/processed_messages", headers={**self.headers, "Prefer": "return=minimal"}, params={"message_id": f"eq.{message_id}", "status": "eq.processed"}, json={"delivery_state": state, "delivery_attempted_at": datetime.now(timezone.utc).isoformat(), "delivery_failure_code": code}, timeout=10)
        response.raise_for_status()

    def close_search(self, search_id, now):
        response = requests.patch(f"{self.url}/rest/v1/conversation_searches", headers={**self.headers, "Prefer": "return=minimal"}, params={"search_id": f"eq.{search_id}"}, json={"status": "inactive", "closed_at": now, "updated_at": now}, timeout=10)
        response.raise_for_status()

    def save_search(self, phone, search_id, state, now):
        payload = {"search_id": search_id, "phone": phone, "operation": state.get("criteria", {}).get("operation"), "state": state, "status": "active", "closed_at": None, "updated_at": now}
        response = requests.post(f"{self.url}/rest/v1/conversation_searches?on_conflict=search_id", headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"}, json=payload, timeout=10)
        response.raise_for_status()

    def record_turn(self, phone, message_id, inbound, outbound, observation, state, stage, summary, timings=None):
        now = datetime.now(timezone.utc).isoformat()
        active_id = state.get("active_search_id") if isinstance(state, dict) else None
        previous_id = active_search_id(state.get("previous", {})) if isinstance(state, dict) else None
        searches = state.get("searches", {}) if isinstance(state, dict) else {}
        if active_id:
            if previous_id and previous_id != active_id:
                self.close_search(previous_id, now)
            current_search_state = searches.get(active_id, {}) if isinstance(searches, dict) else {}
            self.save_search(phone, active_id, current_search_state, now)
        session_state = {"active_search_id": active_id} if active_id else {}
        legacy_searches = dict(state.get("legacy_searches", {})) if isinstance(state, dict) and isinstance(state.get("legacy_searches", {}), dict) else {}
        legacy_searches.pop(active_id, None)
        if not legacy_searches and isinstance(searches, dict):
            # Preserve only legacy searches in the phone session. The active search
            # is canonical in conversation_searches and is not duplicated here.
            legacy_searches = {search_id: search_state for search_id, search_state in searches.items() if search_id != active_id}
        if legacy_searches:
            session_state["legacy_searches"] = legacy_searches
        session = {"phone": phone, "stage": stage, "state": session_state, "summary": summary, "updated_at": now}
        started_at = time.monotonic()
        response = requests.post(f"{self.url}/rest/v1/conversation_sessions?on_conflict=phone", headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"}, json=session, timeout=10)
        response.raise_for_status()
        if timings is not None:
            timings["session_write_ms"] = round((time.monotonic() - started_at) * 1000)
        messages = [
            {"phone": phone, "search_id": active_id, "message_key": f"in:{message_id}", "direction": "inbound", "content": inbound, "extraction": observation},
            {"phone": phone, "search_id": active_id, "message_key": f"out:{message_id}", "direction": "outbound", "content": outbound, "extraction": None},
        ]
        started_at = time.monotonic()
        response = requests.post(f"{self.url}/rest/v1/conversation_messages?on_conflict=message_key", headers={**self.headers, "Prefer": "resolution=ignore-duplicates,return=minimal"}, json=messages, timeout=10)
        response.raise_for_status()
        if timings is not None:
            timings["messages_write_ms"] = round((time.monotonic() - started_at) * 1000)

    def record_observation(self, phone, message_id, inbound, outbound, observation):
        self.record_turn(phone, message_id, inbound, outbound, observation, {}, "observation", "Observación IA almacenada; no controla el flujo productivo.")


def get_lead_store():
    supabase_url, supabase_key = os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")
    if supabase_url and supabase_key:
        return SupabaseLeadStore(supabase_url, supabase_key)
    return LeadStore(os.getenv("LEADS_DB_PATH", "leads.db"))


def get_conversation_memory_store():
    supabase_url, supabase_key = os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")
    return ConversationMemoryStore(supabase_url, supabase_key) if supabase_url and supabase_key else None


@app.route("/", methods=["GET"])
def home():
    return '<html lang="es"><head><title>Arenz | Servicios Inmobiliarios</title></head><body><h1>Arenz</h1><h2>Servicios Inmobiliarios</h2><p>Arenz brinda orientación y atención inmobiliaria.</p><p><a href="/privacy">Política de Privacidad</a></p><p>© 2026 Arenz</p></body></html>', 200


@app.route("/privacy", methods=["GET"])
def privacy():
    return '<html><head><title>Política de Privacidad - ARENZ</title></head><body><h1>Política de Privacidad de ARENZ</h1><p>ARENZ utiliza WhatsApp para atender consultas inmobiliarias.</p><p>Los datos se usan únicamente para atender consultas y mejorar la atención.</p><p>ARENZ no vende ni comercializa información personal.</p><p>Los usuarios pueden solicitar eliminación de datos contactando a ARENZ.</p><p>Última actualización: agosto de 2026.</p></body></html>', 200


@app.route("/logo.png", methods=["GET"])
def logo():
    response = app.send_static_file("arenz-icono-1024.png")
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.route("/terminos", methods=["GET"])
def terms():
    return '<html lang="es"><head><meta charset="utf-8"><title>Condiciones del servicio - ARENZ</title></head><body><h1>Condiciones del servicio de ARENZ</h1><p>ARENZ ofrece orientación inmobiliaria por WhatsApp, incluida la atención automatizada de consultas.</p><p>La información de inmuebles es referencial y debe confirmarse con un asesor antes de cualquier decisión u operación.</p><p>Al escribirnos aceptas que usemos tus mensajes solo para atender tu consulta, según nuestra <a href="/privacy">Política de Privacidad</a>.</p><p>Puedes pedir hablar con una persona o solicitar la <a href="/data-deletion">eliminación de tus datos</a> en cualquier momento.</p><p>Contacto: arenz2026@gmail.com</p><p>Última actualización: octubre de 2026.</p></body></html>', 200


@app.route("/data-deletion", methods=["GET"])
def data_deletion():
    return '<html><head><title>Eliminación de Datos - ARENZ</title></head><body><h1>Solicitud de eliminación de datos</h1><p>Comunícate con ARENZ indicando tu nombre y número de WhatsApp.</p><p>Correo de contacto: arenz2026@gmail.com</p></body></html>', 200


@app.route("/health", methods=["GET"])
def health():
    try:
        store = get_lead_store()
        if isinstance(store, LeadStore): store._connect().close()
        db_ok = True
    except (sqlite3.Error, requests.RequestException): db_ok = False
    ok = all((VERIFY_TOKEN, WHATSAPP_TOKEN, PHONE_NUMBER_ID, APP_SECRET)) and db_ok
    return jsonify({"status": "ok" if ok else "degraded", "commit": SERVICE_COMMIT or "unknown"}), 200 if ok else 503


def generate_reply(sender, text):
    """Existing deterministic qualification flow, retained as fallback."""
    text = text.lower().strip()
    session = user_sessions.get(sender, {"step":"start", "intent":None, "district":None, "budget":None, "bedrooms":None})
    if session["step"] == "start":
        session.update(step="district", intent="compra"); user_sessions[sender] = session
        return INTRODUCTION
    if session["step"] == "intent":
        for intent, words in (("compra",("1","compr")), ("alquiler",("2","alquil")), ("venta",("3","vend"))):
            if any(w in text for w in words):
                session.update(intent=intent, step="district"); user_sessions[sender] = session
                return "Perfecto. ¿En qué distrito o zona estás interesado?"
        if any(w in text for w in ("4","asesor","humano")):
            session.update(intent="asesor", step="done"); user_sessions[sender] = session
            return f"Perfecto. Registraré tu solicitud para que un asesor de {COMMERCIAL_BRAND} continúe contigo."
        return "Indica una opción: 1 Comprar, 2 Alquilar, 3 Vender o 4 Hablar con un asesor."
    if session["step"] == "district":
        session.update(district=text, step="budget"); user_sessions[sender] = session; return "¿Cuál es tu presupuesto aproximado?"
    if session["step"] == "budget":
        if not re.search(r"\d", text):
            return "Para continuar necesito el monto aproximado, por ejemplo: 100 mil dólares."
        session.update(budget=text, step="bedrooms"); user_sessions[sender] = session; return "¿Cuántos dormitorios necesitas?"
    if session["step"] == "bedrooms":
        count = re.search(r"\b(\d{1,2})\b", text)
        if not count:
            return "¿Cuántos dormitorios necesitas? Responde con un número, por ejemplo: 3."
        session.update(bedrooms=count.group(1), step="summary"); user_sessions[sender] = session
        return f"Tengo estos datos:\nOperación: {session['intent']}\nZona: {session['district']}\nPresupuesto: {session['budget']}\nDormitorios: {session['bedrooms']}\n\n¿La información es correcta? Responde Sí o No."
    if session["step"] == "summary":
        if text in ("sí","si"):
            session["step"]="done"; user_sessions[sender]=session; return f"Excelente ✅. Un asesor de {COMMERCIAL_BRAND} podrá continuar contigo."
        if "no" in text:
            session.update(step="district", intent="compra"); user_sessions[sender]=session; return "De acuerdo, empecemos de nuevo. ¿En qué distrito o distritos buscas tu departamento?"
        return "Por favor responde Sí o No."
    return "Tu solicitud ya fue registrada. Si deseas iniciar una nueva búsqueda, escribe NUEVA BÚSQUEDA."



def post_openai_response(payload):
    """POST to the Responses API; on model_not_found, switch once to the fallback model for good."""
    global OPENAI_MODEL
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    response = requests.post("https://api.openai.com/v1/responses", headers=headers, json=payload, timeout=15)
    if response.status_code == 404 and payload.get("model") != OPENAI_FALLBACK_MODEL:
        try:
            code = response.json().get("error", {}).get("code")
        except ValueError:
            code = None
        if code == "model_not_found":
            logger.warning("OpenAI model %s not found; switching to %s", payload.get("model"), OPENAI_FALLBACK_MODEL)
            OPENAI_MODEL = OPENAI_FALLBACK_MODEL
            payload = {**payload, "model": OPENAI_FALLBACK_MODEL}
            response = requests.post("https://api.openai.com/v1/responses", headers=headers, json=payload, timeout=15)
    return response


def generate_ai_reply(sender, text, fallback):
    if not OPENAI_API_KEY: return fallback
    payload = {"model":OPENAI_MODEL, "store":False, "max_output_tokens":220, "instructions":f"Eres {ASSISTANT_NAME}, asesora de {COMMERCIAL_BRAND} en Lima. Responde en español, breve y profesional. No inventes inmuebles, precios ni disponibilidad. Conserva la intención de la respuesta base y pide solo el siguiente dato necesario.", "input":f"Mensaje del usuario: {text}\nRespuesta base: {fallback}"}
    try:
        response = post_openai_response(payload)
        response.raise_for_status()
        return response.json().get("output_text", "").strip() or fallback
    except requests.HTTPError as error:
        logger.warning("AI provider rejected request: status=%s; using deterministic fallback", error.response.status_code if error.response is not None else "unknown")
        return fallback
    except (requests.RequestException, ValueError, AttributeError):
        logger.warning("AI provider unavailable; using deterministic fallback")
        return fallback


def response_output_text(response_body):
    """Read text from either supported Responses API representation."""
    output_text = response_body.get("output_text", "")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    for output in response_body.get("output", []):
        for content in output.get("content", []):
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                return content["text"]
    return ""


def normalized_json_text(output_text):
    """Accept JSON text or a harmless Markdown JSON fence without logging content."""
    text = output_text.strip()
    if text.startswith("```json") and text.endswith("```"):
        return text[7:-3].strip()
    return text


def structured_output_metadata(output_text):
    """Return safe diagnostics without retaining model or user content."""
    text = output_text if isinstance(output_text, str) else ""
    stripped = text.strip()
    initial_type = "fence" if stripped.startswith("```") else "brace" if stripped.startswith("{") else "other"
    normalized = normalized_json_text(text)
    residual_present = False
    if normalized.startswith("{"):
        try:
            _, end = json.JSONDecoder().raw_decode(normalized)
            residual_present = bool(normalized[end:].strip())
        except json.JSONDecodeError:
            pass
    return len(text), initial_type, residual_present


def parse_structured_json(output_text):
    """Parse exactly one JSON object, allowing only trailing whitespace."""
    normalized = normalized_json_text(output_text)
    if not normalized.startswith("{"):
        raise json.JSONDecodeError("expected JSON object", normalized, 0)
    observation, end = json.JSONDecoder().raw_decode(normalized)
    if normalized[end:].strip():
        raise ValueError("json_residual")
    return observation


def response_usage_metadata(response_body, started_at):
    """Return safe, content-free telemetry for a completed Responses API call."""
    usage = response_body.get("usage") if isinstance(response_body, dict) else None
    details = usage.get("output_tokens_details") if isinstance(usage, dict) else None
    return {
        "model": response_body.get("model") if isinstance(response_body, dict) and isinstance(response_body.get("model"), str) else "unknown",
        "usage_available": isinstance(usage, dict),
        "input_tokens": usage.get("input_tokens") if isinstance(usage, dict) else None,
        "output_tokens": usage.get("output_tokens") if isinstance(usage, dict) else None,
        "reasoning_tokens": details.get("reasoning_tokens") if isinstance(details, dict) else None,
        "total_tokens": usage.get("total_tokens") if isinstance(usage, dict) else None,
        "latency_ms": round((time.monotonic() - started_at) * 1000),
    }


def bounded_turn_content(content, direction):
    content = content.strip()
    if direction != "assistant" or len(content) <= 280:
        return content[:280]
    refs = re.findall(r"\bRef\.\s*[A-Za-z0-9-]+", content)[:3]
    prefix = " · ".join(refs) if refs else content[:60]
    prefix = prefix[:100]
    return prefix + " … " + content[-(277 - len(prefix)):]


def recent_conversation_turns(previous, limit=4, raw=False):
    """Return a small durable dialogue window, without internal observation data."""
    state = previous.get("state", {}) if isinstance(previous, dict) else {}
    searches = state.get("searches", {}) if isinstance(state, dict) else {}
    active = state.get("active_search_id") if isinstance(state, dict) else None
    if isinstance(searches, dict) and isinstance(searches.get(active), dict):
        state = searches[active]
    turns = state.get("recent_turns", []) if isinstance(state, dict) else []
    clean = []
    for turn in turns if isinstance(turns, list) else []:
        if not isinstance(turn, dict):
            continue
        direction, content = turn.get("direction"), turn.get("content")
        if direction in ("user", "assistant") and isinstance(content, str) and content.strip():
            clean.append({"direction": direction, "content": content if raw else bounded_turn_content(content, direction)})
    if not clean and isinstance(state, dict):
        for direction, key in (("user", "last_user_message"), ("assistant", "last_assistant_message")):
            content = state.get(key)
            if isinstance(content, str) and content.strip():
                clean.append({"direction": direction, "content": bounded_turn_content(content, direction)})
    return clean[-limit:]


def last_bot_question(previous):
    """The question that closed the bot's last message (what the client is answering)."""
    turns = recent_conversation_turns(previous, limit=8, raw=True) if isinstance(previous, dict) else []
    last = next((t["content"] for t in reversed(turns) if t["direction"] == "assistant"), "")
    visible = customer_visible_reply(last)
    questions = re.findall(r"¿[^¿?]*\?", visible)
    return (questions[-1] if questions else visible[-200:]).strip()


OBSERVATION_CARD_CHARS = 1800


SHORT_ANSWERS = {"chau", "chao", "bye", "adios", "adiós", "nos vemos", "hasta luego"}


def needs_unit_card(message):
    """A bare yes, no, thanks or goodbye is classified without the project card (saves ~500 tokens)."""
    cleaned = " ".join((message or "").casefold().split()).strip(".!¡ ")
    if "?" in cleaned or len(cleaned.split()) > 4:
        return True
    return not (AFFIRMATIVE_PATTERN.match(cleaned) or THANKS_PATTERN.match(cleaned) or cleaned in SHORT_ANSWERS
                or (NEGATIVE_PATTERN.match(cleaned) and "pero" not in cleaned))


def shown_unit_card(previous):
    """Project card of the single unit on screen, so the same call can answer about it."""
    shown = latest_presented_references(previous)
    if (previous or {}).get("stage") not in ("property_detail", "handoff") or len(shown) != 1:
        return ""
    store = get_inventory_store()
    if not store:
        return ""
    try:
        return clean_card_text(store.project_card(shown[0]))[:OBSERVATION_CARD_CHARS]
    except (requests.RequestException, ValueError):
        logger.warning("Project card unavailable for observation")
        return ""


def build_observation_payload(previous, text, raw_text=None):
    """Build a bounded request from durable criteria and recent dialogue."""
    context = {"stage": previous.get("stage") if isinstance(previous, dict) else None, "active_search_id": active_search_id(previous), "criteria": conversation_state(previous), "recent_turns": recent_conversation_turns(previous)}
    conversational = conversacion_ia_activa()
    if conversational:
        context["ultima_pregunta_bot"] = last_bot_question(previous)
        card = shown_unit_card(previous) if needs_unit_card(raw_text or text) else ""
        if card:
            context["ficha_proyecto"] = card
        # The AI reads the message as typed: the normalizer folds 'ok gracias' into 'Sí'.
        text = raw_text or text
    payload = {
        "model": OPENAI_MODEL, "store": False, "max_output_tokens": 1200,
        "instructions": "Analiza una conversación inmobiliaria en Lima. Los clientes escriben rápido, con faltas de ortografía, sin tildes, con abreviaturas y jerga peruana (depa = departamento, lucas = soles, k = mil, 'cuartos' o 'habitaciones' = dormitorios): interpreta lo que quieren decir según el contexto de la conversación y la última pregunta del asesor, como lo haría una persona. Extrae solo datos explícitos o altamente confiables. No inventes inmuebles, precios ni disponibilidad. Devuelve únicamente el JSON del esquema. NUEVA BÚSQUEDA es intent new_search. Para cambios usa criteria_actions: ADD agrega, UPDATE reemplaza y REMOVE elimina; 'ya no es necesario' es REMOVE, nunca una preferencia negativa. Estacionamiento/cochera se expresa únicamente con parking_required: 'con estacionamiento', 'con cochera', 'necesito estacionamiento' y 'necesito cochera' son true; 'sin estacionamiento', 'sin cochera', 'no necesito estacionamiento' y 'no necesito cochera' son false. Para cualquier valor explícito usa UPDATE parking_required con una lista unitaria; false significa que no es requisito y no una preferencia negativa. property_type debe usar solo valores canónicos: departamento es el valor para departamento y apartamento; nunca devuelvas apartamento. Un cambio explícito entre compra, alquiler y venta inicia un contexto independiente. assistant_reply debe ser natural, útil y máximo 180 caracteres, exclusivamente sobre búsqueda inmobiliaria, criterios, aclaraciones, compra, alquiler, venta o derivación a un asesor. No introduzcas humor, temas generales, recomendaciones no inmobiliarias ni contenido accesorio; nunca afirmes disponibilidad, recomendación ni características de una propiedad concreta. No preguntes un criterio ya presente en Contexto salvo ambigüedad o conflicto. user_question máximo 120 caracteres; máximo tres distritos y tres preferencias.",
        "text": {"format": {"type": "json_schema", "name": "arenz_conversation_observation", "strict": True, "schema": OBSERVATION_SCHEMA_IA if conversational else OBSERVATION_SCHEMA}},
        "input": f"Contexto: {json.dumps(context, ensure_ascii=False)}\nMensaje: {text}",
    }
    payload["instructions"] += COMMERCIAL_POLICY
    if conversational:
        payload["instructions"] += CUSTOMER_REPLY_POLICY
    return payload


def observe_conversation(sender, text, deterministic_reply, timings=None, previous=None, previous_loaded=False, raw_text=None):
    """Extract structured signals only; never controls the user-facing reply."""
    if not OPENAI_API_KEY:
        return None
    memory = get_conversation_memory_store()
    if not previous_loaded and memory:
        try:
            previous = memory.load_session(sender)
        except requests.RequestException:
            logger.warning("Conversation memory unavailable during observation")
    context_started_at = time.monotonic()
    payload = build_observation_payload(previous, text, raw_text)
    if timings is not None:
        timings["context_ms"] = round((time.monotonic() - context_started_at) * 1000)
    started_at = time.monotonic()
    try:
        response = post_openai_response(payload)
        response.raise_for_status()
        response_body = response.json()
        telemetry = response_usage_metadata(response_body, started_at)
        if timings is not None:
            timings["openai_ms"] = telemetry["latency_ms"]
        logger.info("Conversation observation telemetry: model=%s usage_available=%s input_tokens=%s output_tokens=%s reasoning_tokens=%s total_tokens=%s latency_ms=%s", telemetry["model"], telemetry["usage_available"], telemetry["input_tokens"], telemetry["output_tokens"], telemetry["reasoning_tokens"], telemetry["total_tokens"], telemetry["latency_ms"])
        output_text = response_output_text(response_body)
        if not output_text:
            outputs = response_body.get("output", [])
            content_types = []
            refusal_present = False
            if isinstance(outputs, list):
                for output in outputs:
                    for content in output.get("content", []) if isinstance(output, dict) else []:
                        content_type = content.get("type", "unknown") if isinstance(content, dict) else "unknown"
                        content_types.append(content_type)
                        refusal_present = refusal_present or content_type == "refusal"
            incomplete = response_body.get("incomplete_details", {})
            logger.warning("Conversation observation empty output: output_present=%s content_types=%s refusal_present=%s status=%s incomplete_reason=%s", isinstance(outputs, list), ",".join(sorted(set(content_types))) or "none", refusal_present, response_body.get("status", "unknown"), incomplete.get("reason", "none") if isinstance(incomplete, dict) else "none")
            raise ValueError("empty_output")
        observation = parse_structured_json(output_text)
        required = {"intent", "slot_updates", "criteria_change", "user_question", "next_action", "handoff", "assistant_reply"}
        if not isinstance(observation, dict) or not required.issubset(observation) or not isinstance(observation["slot_updates"], dict):
            raise ValueError("invalid_contract")
        logger.info("Conversation observation: intent=%s next_action=%s slots=%s handoff=%s latency_ms=%s", observation["intent"], observation["next_action"], sum(value is not None and value != [] for value in observation["slot_updates"].values()), observation["handoff"], telemetry["latency_ms"])
        return observation
    except requests.HTTPError as error:
        details = {}
        try:
            details = error.response.json().get("error", {}) if error.response is not None else {}
        except ValueError:
            pass
        logger.warning("Conversation observation unavailable: status=%s code=%s type=%s", error.response.status_code if error.response is not None else "unknown", details.get("code", "unknown"), details.get("type", "unknown"))
    except json.JSONDecodeError:
        output_length, initial_type, residual_present = structured_output_metadata(output_text)
        logger.warning("Conversation observation unavailable: reason=invalid_json output_length=%s initial_type=%s residual_present=%s", output_length, initial_type, residual_present)
    except ValueError as error:
        output_length, initial_type, residual_present = structured_output_metadata(output_text)
        logger.warning("Conversation observation unavailable: reason=%s output_length=%s initial_type=%s residual_present=%s", str(error), output_length, initial_type, residual_present)
    except requests.RequestException as error:
        logger.warning("Conversation observation unavailable: reason=request_failed error_type=%s latency_ms=%s", type(error).__name__, round((time.monotonic() - started_at) * 1000))
    except (KeyError, TypeError, AttributeError) as error:
        logger.warning("Conversation observation unavailable: reason=response_shape_error error_type=%s", type(error).__name__)
    return None


def persist_conversation_observation(sender, message_id, inbound, outbound, observation):
    memory = get_conversation_memory_store()
    if not memory:
        return
    try:
        memory.record_observation(sender, message_id, inbound, outbound, observation)
    except requests.RequestException:
        logger.warning("Conversation observation persistence unavailable")


ALLOWED_OPERATIONS = {"compra", "alquiler", "venta"}
OPERATION_ALIASES = {"comprar": "compra", "alquilar": "alquiler", "vender": "venta"}
ALLOWED_CURRENCIES = {"USD", "PEN"}
CURRENCY_ALIASES = {"dólares": "USD", "dolares": "USD", "dólar": "USD", "dolar": "USD", "$": "USD", "us$": "USD",
                    "soles": "PEN", "sol": "PEN", "s/": "PEN", "s/.": "PEN"}
PROPERTY_TYPE_ALIASES = {"apartamento": "departamento"}
INVENTORY_RULES_VERSION = "p1-v1"
INVENTORY_MAX_RESULTS = 3
INVENTORY_VERIFICATION_DAYS = 7
# Most of the C2 list is priced in soles, while clients often state budgets in dollars;
# matching only same-currency listings hid nearly all of it. Update when the rate moves.
USD_PEN_RATE = float(os.getenv("USD_PEN_RATE", "3.45"))


def budget_in_currency(criteria, currency):
    """The client's budget expressed in a listing's currency, or None if not convertible."""
    budget, own = criteria.get("budget_max"), criteria.get("currency")
    if budget is None or own not in ALLOWED_CURRENCIES or currency not in ALLOWED_CURRENCIES:
        return None
    if own == currency:
        return float(budget)
    return float(budget) * USD_PEN_RATE if own == "USD" else float(budget) / USD_PEN_RATE
THIRD_PARTY = "third_party"


def explicit_operation_from_text(text):
    """Recognize only explicit purchase/rental/sale requests without touching other slots."""
    normalized = " ".join((text or "").casefold().split())
    wants_purchase = bool(re.search(r"\b(?:quiero|busco|deseo)\s+comprar\b|\bcomprar\b|\bcompra\b", normalized))
    wants_rental = bool(re.search(r"\b(?:quiero|busco|deseo)\s+alquilar\b|\balquilar\b|\balquiler\b", normalized))
    wants_sale = bool(re.search(r"\b(?:quiero|deseo)\s+vender\b|\bvender\s+mi\s+(?:departamento|inmueble)\b|\bventa\b", normalized))
    matches = [operation for operation, matched in (("compra", wants_purchase), ("alquiler", wants_rental), ("venta", wants_sale)) if matched]
    if len(matches) != 1:
        return None
    return matches[0]


SPANISH_NUMBERS = dict(zip(
    "cero uno un una dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince dieciseis diecisiete dieciocho diecinueve veinte veintiuno veintiun veintidos veintitres veinticuatro veinticinco veintiseis veintisiete veintiocho veintinueve treinta cuarenta cincuenta sesenta setenta ochenta noventa cien ciento doscientos trescientos cuatrocientos quinientos seiscientos setecientos ochocientos novecientos".split(),
    list(range(0, 2)) + [1, 1] + list(range(2, 22)) + [21] + list(range(22, 30)) + [30, 40, 50, 60, 70, 80, 90, 100, 100, 200, 300, 400, 500, 600, 700, 800, 900]))


def explicit_money_amount(text):
    """Read a single currency-adjacent amount, including Spanish whole numbers."""
    normalized = text.translate(str.maketrans("áéíóú", "aeiou"))
    units = r"(?:dolares?|usd|soles|pen)\b"
    number = r"\d+(?:[.,]\d{3})*(?:[.,]\d{1,2})?(?:\s+mil)?"
    words = "|".join(sorted((*SPANISH_NUMBERS, "y", "mil", "millon", "millones"), key=len, reverse=True))
    written = rf"(?:\b(?:{words})\s+)+"
    candidates = re.findall(rf"(?<![\w.])({number}|{written})\s*(?={units})", normalized)
    for match in re.finditer(rf"(?:us\$|\$|s/)\s*({number})(?![\w.,])", normalized):
        if not re.match(rf"\s*{units}", normalized[match.end():]):
            candidates.append(match.group(1))
    candidates += re.findall(rf"(?<![\w.])({number})\s*(?=\$)", normalized)
    if len(candidates) != 1:
        return None
    value = candidates[0].strip()
    if value[0].isdigit():
        multiplier = 1000 if value.endswith("mil") else 1
        value = value.removesuffix("mil").strip()
        value = re.sub(r"[.,](?=\d{3}(?:[.,]|$))", "", value).replace(",", ".")
        return float(value) * multiplier
    total = group = 0
    for token in value.split():
        if token == "y":
            continue
        if token == "mil":
            total += (group or 1) * 1000
            group = 0
        elif token in {"millon", "millones"}:
            total = (total + group or 1) * 1000000
            group = 0
        else:
            group += SPANISH_NUMBERS[token]
    return float(total + group)


def with_explicit_operation(observation, text):
    """Overlay a deterministic explicit operation while preserving extractor output."""
    operation = explicit_operation_from_text(text)
    normalized = (text or "").casefold()
    usd = bool(re.search(r"\b(?:d[oó]lares?|usd)\b|\$", normalized))
    pen = bool(re.search(r"\b(?:soles|sol|pen)\b|s/", normalized))
    currency = "USD" if usd and not pen else "PEN" if pen and not usd else None
    if not operation and not currency:
        return observation
    result = dict(observation) if isinstance(observation, dict) else {"intent": "change_criteria", "handoff": False}
    slots = dict(result.get("slot_updates", {}))
    if operation:
        slots["operation"] = operation
    if currency:
        slots["currency"] = currency
        # A single explicit amount next to its currency is safe even without AI.
        amount = explicit_money_amount(normalized)
        if amount is not None:
            # Nobody buys a flat for 500: "500 cocos" or "450 lucas" means thousands.
            slots["budget_max"] = amount * 1000 if 10 <= amount < 1000 else amount
        # Discard a stale generated question about a currency just supplied.
        if re.search(r"moneda|usd|pen|d[oó]lares|soles", str(result.get("assistant_reply", "")), re.I):
            result["assistant_reply"] = None
    result["slot_updates"] = slots
    return result


def canonical_property_type(value):
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return PROPERTY_TYPE_ALIASES.get(normalized, normalized)


def fold_text(value):
    """Lowercase without accents: 'Jesús Maria' and 'jesus maría' fold to the same key."""
    return "".join(c for c in unicodedata.normalize("NFD", value or "") if unicodedata.category(c) != "Mn").casefold().strip()


DISTRICT_ALIASES = {
    "santiago de surco": "Surco", "surco": "Surco", "jesus maria": "Jesús María", "cercado": "Cercado Lima",
    "cercado de lima": "Cercado Lima", "lima cercado": "Cercado Lima", "lima": "Cercado Lima", "magdalena del mar": "Magdalena",
    "brena": "Breña", "san martin de porres": "San Martín de Porres", "smp": "San Martín de Porres",
    "san juan de lurigancho": "San Juan de Lurigancho", "sjl": "San Juan de Lurigancho",
    "san juan de miraflores": "San Juan de Miraflores", "sjm": "San Juan de Miraflores", "la molina": "La Molina",
    "rimac": "Rímac", "lurin": "Lurín", "san isidro": "San Isidro", "san borja": "San Borja", "san miguel": "San Miguel",
}


def canonical_district(value):
    text = re.sub(r"^(en|el|la|distrito de|distrito)\s+", "", (value or "").strip(), flags=re.IGNORECASE)
    """Known Lima district for a name, or None: an unknown word ("Once", "Lima centro?") never becomes a district."""
    found = DISTRICT_ALIASES.get(fold_text(text)) or closest_district(text)
    return found if found and is_known_district(found) else None


def district_variants(district):
    """Spellings the inventory may use for one district (accented and not), for the exact-match PostgREST filter."""
    words = district.split()
    variants = {""}
    for word in words:
        plain = fold_text(word)
        options = {word, plain.capitalize() if word[:1].isupper() else plain}
        variants = {f"{v} {o}".strip() for v in variants for o in options}
    return sorted(variants)


def validated_slot_updates(slot_updates):
    """Accept only non-empty, bounded criteria from the structured AI contract."""
    if not isinstance(slot_updates, dict):
        return {}
    clean = {}
    operation = slot_updates.get("operation")
    if isinstance(operation, str):
        operation = OPERATION_ALIASES.get(operation.lower(), operation.lower())
        if operation in ALLOWED_OPERATIONS:
            clean["operation"] = operation
    districts = slot_updates.get("districts")
    if isinstance(districts, list):
        districts = [d for d in dict.fromkeys(canonical_district(value) for value in districts
                                              if isinstance(value, str) and 1 <= len(value.strip()) <= 60) if d][:3]
        if districts:
            clean["districts"] = districts
    budget = slot_updates.get("budget_max")
    if isinstance(budget, (int, float)) and 1000 <= budget <= 10000000:
        clean["budget_max"] = int(budget)
    currency = slot_updates.get("currency")
    if isinstance(currency, str):
        currency = CURRENCY_ALIASES.get(currency.strip().casefold(), currency.strip().upper())
        if currency in ALLOWED_CURRENCIES:
            clean["currency"] = currency
    bedrooms = slot_updates.get("bedrooms")
    if isinstance(bedrooms, int) and 0 <= bedrooms <= 20:
        clean["bedrooms"] = bedrooms
    property_type = canonical_property_type(slot_updates.get("property_type"))
    if isinstance(property_type, str) and 1 <= len(property_type) <= 40:
        clean["property_type"] = property_type
    parking_required = slot_updates.get("parking_required")
    if isinstance(parking_required, bool):
        clean["parking_required"] = parking_required
    preferences = slot_updates.get("preferences")
    if isinstance(preferences, list):
        preferences = [value.strip().lower() for value in preferences if isinstance(value, str) and 1 <= len(value.strip()) <= 80][:10]
        if preferences:
            clean["preferences"] = preferences
    for key in ("payment_method", "credit_status"):
        value = slot_updates.get(key)
        if value is not None and value in COMMERCIAL_FIELDS[key]["enum"]:
            clean[key] = value
    timeline = slot_updates.get("purchase_timeline")
    if isinstance(timeline, str) and 1 <= len(timeline.strip()) <= 120:
        clean["purchase_timeline"] = timeline.strip()
    for key, limit in (("area_min_m2", 100000), ("bathrooms_min", 30)):
        value = slot_updates.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= limit:
            clean[key] = value
    return clean


def conversation_state(previous):
    state = previous.get("state", {}) if isinstance(previous, dict) else {}
    searches = state.get("searches", {}) if isinstance(state, dict) else {}
    active = state.get("active_search_id") if isinstance(state, dict) else None
    if isinstance(searches, dict) and active in searches and isinstance(searches[active], dict):
        criteria = searches[active].get("criteria", {})
    else:
        criteria = state.get("criteria", {}) if isinstance(state, dict) else {}
    keys = ("operation", "districts", "budget_max", "currency", "bedrooms", "property_type", "parking_required", "preferences", "delivery_preference", *COMMERCIAL_FIELDS)
    return {key: criteria.get(key) for key in keys if criteria.get(key) not in (None, [], "")}


def active_search_id(previous):
    state = previous.get("state", {}) if isinstance(previous, dict) else {}
    active = state.get("active_search_id") if isinstance(state, dict) else None
    return active if isinstance(active, str) and active != "legacy" else None


RESUME_QUESTION = "¿Quieres retomar tu búsqueda anterior o comenzar una nueva?"
NEW_SEARCH_ANSWER = re.compile(r"^(no\b|nueva|nuevo|otra|otro|comenzar|empezar|desde cero|de cero|una nueva)", re.IGNORECASE)


def resume_answer_as_command(previous, text):
    """'No', 'otra', 'nueva'... right after the resume question means a new search."""
    asked = ""
    turns = recent_conversation_turns(previous, limit=4) if isinstance(previous, dict) else []
    for turn in turns:
        if turn["direction"] == "assistant":
            asked = turn["content"]
    if RESUME_QUESTION in asked and NEW_SEARCH_ANSWER.match((text or "").strip()):
        return "nueva búsqueda"
    return text


def is_new_search_request(text):
    normalized = " ".join((text or "").casefold().split())
    return normalized in {"nueva búsqueda", "nueva busqueda", "nueva búsqueda.", "nueva busqueda."}


def sanitize_new_search_observation(observation, user_text=None):
    """Treat an explicit NEW SEARCH command as context control, never criteria."""
    if not is_new_search_request(user_text) or not isinstance(observation, dict):
        return observation
    sanitized = dict(observation)
    sanitized["slot_updates"] = {}
    sanitized["criteria_actions"] = []
    sanitized["criteria_change"] = False
    return sanitized


def search_state_for_turn(previous, observation, user_text=None):
    """Return a backward-compatible multi-search state and its active criteria."""
    old_state = previous.get("state", {}) if isinstance(previous, dict) else {}
    searches = dict(old_state.get("searches", {})) if isinstance(old_state.get("searches"), dict) else {}
    active = old_state.get("active_search_id") if isinstance(old_state, dict) else None
    if not searches and conversation_state(previous):
        active = active_search_id(previous) or str(uuid.uuid4())
        searches[active] = {"criteria": conversation_state(previous)}
    if active == "legacy" and active in searches:
        # Old sessions used the sentinel "legacy"; durable search keys are UUIDs.
        legacy_search = searches[active]
        active = str(uuid.uuid4())
        searches[active] = legacy_search
    slots = validated_slot_updates(observation.get("slot_updates")) if isinstance(observation, dict) else {}
    requested_operation = slots.get("operation")
    starts_new = is_new_search_request(user_text) or (isinstance(observation, dict) and observation.get("intent") == "new_search")
    current = searches.get(active, {}).get("criteria", {}) if active else {}
    if requested_operation and current.get("operation") and requested_operation != current.get("operation"):
        starts_new = True
    create_search = starts_new or (active not in searches and bool(requested_operation))
    if create_search:
        active = str(uuid.uuid4())
        searches[active] = {"criteria": {}}
    if active not in searches:
        return {"active_search_id": None, "searches": searches}, {}
    return {"active_search_id": active, "searches": searches}, searches[active]["criteria"]


def apply_criteria_actions(criteria, actions):
    """Apply explicit mutations so removed values cannot reappear from stale state."""
    result = dict(criteria)
    scalar_update_fields = {"budget_max", "currency", "bedrooms", "property_type", "parking_required", *COMMERCIAL_FIELDS}
    for action in actions if isinstance(actions, list) else []:
        if not isinstance(action, dict):
            continue
        kind, field, values = action.get("action"), action.get("field"), action.get("values")
        if kind not in {"ADD", "UPDATE", "REMOVE"} or field not in {"districts", "budget_max", "currency", "bedrooms", "property_type", "parking_required", "preferences", *COMMERCIAL_FIELDS}:
            continue
        value_for_validation = values
        if kind == "UPDATE" and field in scalar_update_fields and isinstance(values, list) and len(values) == 1:
            value_for_validation = values[0]
        cleaned = validated_slot_updates({field: value_for_validation}).get(field)
        if kind == "REMOVE":
            if field in {"districts", "preferences"} and isinstance(values, list) and values:
                result[field] = [item for item in result.get(field, []) if item not in (cleaned or [])]
                if not result[field]: result.pop(field, None)
            else:
                result.pop(field, None)
        elif kind == "ADD" and field in {"districts", "preferences"}:
            result[field] = list(dict.fromkeys(result.get(field, []) + (cleaned or [])))
        elif cleaned not in (None, [], ""):
            result[field] = cleaned
    return result


def assistant_reply_is_real_estate_scoped(reply):
    """Accept only concise reply segments tied to the supported real-estate flow."""
    domain_terms = ("inmueble", "inmobiliaria", "propiedad", "departamento", "apartamento", "casa", "compra", "comprar", "alquiler", "alquilar", "venta", "vender", "búsqueda", "busqueda", "buscar", "criterio", "distrito", "zona", "presupuesto", "precio", "usd", "pen", "dormitorio", "estacionamiento", "cochera", "asesor", "inventario", "moneda")
    generic_acknowledgements = ("claro", "perfecto", "de acuerdo", "gracias", "claro, puedo ayudarte con esa consulta", "puedo ayudarte")
    neutral_clarifications = ("qué deseas consultar", "que deseas consultar", "en qué puedo ayudarte", "en que puedo ayudarte")
    segments = [re.sub(r"[^\w\s¿¡,;:()'\"/-]", "", segment).strip().casefold() for segment in re.split(r"[.!?]+", reply)]
    segments = [segment for segment in segments if segment]
    return bool(segments) and all(
        any(term in segment for term in domain_terms)
        or segment.strip("¿¡ ") in generic_acknowledgements
        or any(question in segment for question in neutral_clarifications)
        for segment in segments
    )


def usable_assistant_reply(observation, route):
    reply = observation.get("assistant_reply") if isinstance(observation, dict) else None
    if route not in {"conversation", "qualification"} or not isinstance(reply, str):
        return None
    reply = reply.strip()
    if not reply or len(reply) > 280 or reply.count("?") > 1:
        return None
    if re.search(r"alquil|vender|venta o|tipo de (inmueble|propiedad)|casa|terreno|oficina|local comercial", reply, re.IGNORECASE):
        return None
    prohibited = ("tenemos disponible", "encontré", "encontre", "hay disponibilidad", "opción recomendada", "opcion recomendada", "la recomendada", "revisando disponibilidad", "cuenta con", "te mostraré las opciones", "te mostrare las opciones")
    return None if any(term in reply.lower() for term in prohibited) or not assistant_reply_is_real_estate_scoped(reply) else reply


def with_sale_defaults(criteria):
    """C2 only sells apartments: operation and type are implied when the client did not say them."""
    if not isinstance(criteria, dict):
        return criteria
    return {**criteria, "operation": criteria.get("operation") or "compra", "property_type": criteria.get("property_type") or "departamento"}


def inventory_ready(criteria):
    """Only run a concrete inventory search after the P0 qualification is complete."""
    criteria = with_sale_defaults(criteria)
    required = ("operation", "property_type", "districts", "budget_max", "currency", "bedrooms")
    return (
        isinstance(criteria, dict)
        and criteria.get("operation") in {"compra", "alquiler"}
        and all(criteria.get(key) not in (None, [], "") for key in required)
    )


def public_property_snapshot(property_row, media_rows):
    """Allowlist the only property fields that may reach a prospect."""
    approved_media = [
        {"media_type": row["media_type"], "media_url": row["media_url"]}
        for row in media_rows if row.get("approved_for_client") is True
    ]
    return {
        "public_reference": property_row["public_reference"],
        "operation": property_row["operation"],
        "property_type": property_row["property_type"],
        "district": property_row["district"],
        "zone": property_row.get("zone"),
        "public_location_reference": property_row.get("public_location_reference"),
        "price_amount": property_row["price_amount"],
        "currency": property_row["currency"],
        "bedrooms": property_row.get("bedrooms"),
        "bathrooms": property_row.get("bathrooms"),
        "area_m2": property_row.get("area_m2"),
        "area_total_m2": property_row.get("area_total_m2"),
        "area_built_m2": property_row.get("area_built_m2"),
        "terrace_area_m2": property_row.get("terrace_area_m2"),
        # Provenance travels to the client on purpose: a third-party listing must
        # be shown as such. source_url and advertiser_name stay out — naming the
        # other agency, or linking to it, hands the lead away.
        "provenance": property_row.get("provenance") or "own",
        "parking_spaces": property_row.get("parking_spaces"),
        "features": property_row.get("features") or [],
        "public_description": property_row.get("public_description"),
        "availability_confirmed_at": property_row["availability_confirmed_at"],
        "media": approved_media,
    }


COLLABORATION_CONFIRMED = "collaboration_confirmed"


def collaboration_allows_showing(property_row):
    """A borrowed listing needs the advertiser's consent, not just availability.

    A public advert is not permission to route clients to it. ARENZ's own approval
    cannot stand in for the other agency accepting the referral.
    """
    if property_row.get("provenance") != THIRD_PARTY:
        return True
    return (property_row.get("verification_status") == COLLABORATION_CONFIRMED
            and bool(property_row.get("collaboration_confirmed_at")))


def property_is_eligible(property_row, now=None):
    """The P1 eligibility rule is deterministic and independent of model output."""
    now = now or datetime.now(timezone.utc)
    required = ("public_reference", "operation", "property_type", "district", "price_amount", "currency", "approved_at", "availability_confirmed_at")
    if property_row.get("lifecycle_state") != "active_confirmed" or any(property_row.get(field) in (None, "") for field in required):
        return False
    if not collaboration_allows_showing(property_row):
        return False
    try:
        verified_at = datetime.fromisoformat(property_row["availability_confirmed_at"].replace("Z", "+00:00"))
        verified_at = verified_at if verified_at.tzinfo else verified_at.replace(tzinfo=timezone.utc)
        return verified_at >= now - timedelta(days=INVENTORY_VERIFICATION_DAYS)
    except (TypeError, ValueError, AttributeError):
        return False


def property_matches_criteria(property_row, criteria):
    """P1 hard filters; preferences are intentionally only a small deterministic rank."""
    districts = {fold_text(district) for district in criteria.get("districts", []) if isinstance(district, str)}
    if property_row.get("operation") != criteria.get("operation"):
        return False
    if fold_text(property_row.get("district", "")) not in districts:
        return False
    if canonical_property_type(property_row.get("property_type")) != criteria.get("property_type"):
        return False
    # Unknown parking ("por confirmar") stays in the results; only a unit recorded without parking is dropped.
    if criteria.get("parking_required") is True and property_row.get("parking_spaces") == 0:
        return False
    try:
        for field, minimum in (("bedrooms", criteria.get("bedrooms")),
                               ("bathrooms", criteria.get("bathrooms_min")),
                               ("area_total_m2", criteria.get("area_min_m2"))):
            actual = property_row.get(field)
            if field == "area_total_m2" and actual is None:
                actual = property_row.get("area_m2")
            if minimum is not None and (actual is None or float(actual) < float(minimum)):
                return False
        limit = budget_in_currency(criteria, property_row.get("currency"))
        return limit is not None and float(property_row["price_amount"]) <= limit
    except (KeyError, TypeError, ValueError):
        return False


def property_match_rank(property_row, criteria):
    score = 0
    if property_row.get("bedrooms") == criteria.get("bedrooms"):
        score += 4
    elif isinstance(property_row.get("bedrooms"), int) and property_row["bedrooms"] > criteria.get("bedrooms", 0):
        score += 2
    if property_row.get("parking_spaces"):
        score += 1
    requested = set(criteria.get("preferences") or [])
    score += len(requested.intersection(set(property_row.get("features") or [])))
    return score


DELIVERY_BUCKETS = ("inmediata", "futura", "por_confirmar")
_MONTHS = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12}
_QUARTER_END = {"1er": 3, "2do": 6, "3er": 9, "4to": 12}


def delivery_date(text):
    """(year, month) from a source delivery label such as 'Oct-27', 'Dic 2027' or '1er Tri 2029'."""
    label = (text or "").casefold()
    year = re.search(r"(20\d\d)", label) or re.search(r"[-\s](\d\d)\b", label)
    if not year:
        return None
    y = int(year.group(1)); y = y + 2000 if y < 100 else y
    month = next((m for key, m in _MONTHS.items() if key in label), None)
    quarter = re.search(r"(1er|2do|3er|4to)\s*tri", label)
    if quarter:
        month = _QUARTER_END[quarter.group(1)]
    return y, month or 12


def property_delivery(row, today=None):
    """inmediata, futura or por_confirmar, from the delivery label C2's lists publish."""
    today = today or datetime.now(timezone.utc).date()
    label = re.search(r"Entrega según fuente: ([^.]+)\.", row.get("public_description") or "")
    label = label.group(1).strip() if label else ""
    if re.search(r"entregad|inmediat", label, re.IGNORECASE):
        return "inmediata"
    if not label:
        # Second-hand units from Trello carry no delivery label: they already exist.
        return "inmediata" if row.get("source_name") == "trello" else "por_confirmar"
    when = delivery_date(label)
    if not when:
        return "por_confirmar"
    return "inmediata" if when <= (today.year, today.month) else "futura"


def delivery_summary(rows):
    counts = {bucket: 0 for bucket in DELIVERY_BUCKETS}
    years = []
    for row in rows:
        bucket = property_delivery(row)
        counts[bucket] += 1
        if bucket == "futura":
            label = re.search(r"Entrega según fuente: ([^.]+)\.", row.get("public_description") or "")
            when = delivery_date(label.group(1)) if label else None
            if when:
                years.append(when[0])
    counts["years"] = (min(years), max(years)) if years else None
    return counts


class MatchList(list):
    """Inventory matches plus the per-delivery counts of everything that matched."""
    summary = None


class InventoryStore:
    """Small REST adapter for canonical inventory; it never exposes internal fields."""
    property_fields = ("property_id,public_reference,operation,property_type,district,zone,public_location_reference,"
                      "price_amount,currency,bedrooms,bathrooms,area_m2,area_total_m2,area_built_m2,terrace_area_m2,"
                      "parking_spaces,features,public_description,lifecycle_state,availability_confirmed_at,approved_at,"
                      "provenance,verification_status,collaboration_confirmed_at,source_name")

    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def find_matches(self, criteria, now=None):
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=INVENTORY_VERIFICATION_DAYS)
        params = {
            "select": self.property_fields,
            "operation": f"eq.{criteria['operation']}",
            "lifecycle_state": "eq.active_confirmed",
            "approved_at": "not.is.null",
            "availability_confirmed_at": f"gte.{cutoff.isoformat()}",
            "or": "(" + ",".join(f"and(currency.eq.{c},price_amount.lte.{budget_in_currency(criteria, c):.2f})"
                                 for c in sorted(ALLOWED_CURRENCIES)) + ")",
            "property_type": f"eq.{criteria['property_type']}",
            "district": "in.(" + ",".join(f'"{v}"' for d in criteria["districts"] for v in district_variants(canonical_district(d) or d)) + ")",
        }
        response = requests.get(f"{self.url}/rest/v1/inventory_properties", headers=self.headers, params=params, timeout=10)
        response.raise_for_status()
        candidates = [row for row in response.json() if property_is_eligible(row, now) and property_matches_criteria(row, criteria)]
        candidates.sort(key=lambda row: (-property_match_rank(row, criteria),
                                         float(row["price_amount"]) / (budget_in_currency(criteria, row["currency"]) or 1),
                                         row["public_reference"]))
        summary = delivery_summary(candidates)
        kept = []
        for bucket in DELIVERY_BUCKETS:
            kept += [row for row in candidates if property_delivery(row) == bucket][:INVENTORY_MAX_RESULTS * 10]
        media_by_property = self._approved_media([row["property_id"] for row in kept])
        matches = MatchList({"property": row, "public": public_property_snapshot(row, media_by_property.get(row["property_id"], [])),
                             "delivery": property_delivery(row)} for row in kept)
        matches.summary = summary
        return matches, cutoff

    def project_card(self, reference):
        """Trello card text for a unit (its own card for second-hand units, else its project's), or ''."""
        if not reference:
            return ""
        return self.card_row(reference).get("card_desc") or ""

    def card_row(self, reference):
        """{'card_desc', 'client_notice'} for a unit, or {}."""
        if not reference:
            return {}
        response = requests.get(f"{self.url}/rest/v1/inventory_card_by_ref", headers=self.headers,
                                params={"select": "card_desc,client_notice", "public_reference": f"eq.{reference}"}, timeout=10)
        response.raise_for_status()
        rows = response.json()
        return rows[0] if rows else {}

    def unit_notice(self, reference):
        return self.card_row(reference).get("client_notice") or ""

    def _approved_media(self, property_ids):
        if not property_ids:
            return {}
        response = requests.get(
            f"{self.url}/rest/v1/inventory_property_media",
            headers=self.headers,
            params={"select": "property_id,media_type,media_url,approved_for_client,display_order", "property_id": "in.(" + ",".join(property_ids) + ")", "approved_for_client": "eq.true", "order": "display_order.asc"},
            timeout=10,
        )
        response.raise_for_status()
        grouped = {}
        for row in response.json():
            grouped.setdefault(row["property_id"], []).append(row)
        return grouped

    def unit_row(self, reference):
        response = requests.get(f"{self.url}/rest/v1/inventory_properties", headers=self.headers,
                                params={"select": self.property_fields, "public_reference": f"eq.{reference}"}, timeout=10)
        response.raise_for_status()
        rows = response.json()
        return rows[0] if rows else None

    def photos_for_reference(self, reference, limit=5):
        """Approved photo URLs of one unit, in display order."""
        row = self.unit_row(reference)
        if not row:
            return []
        media = self._approved_media([row["property_id"]]).get(row["property_id"], [])
        return [m["media_url"] for m in media if m.get("media_type") == "photo"][:limit]

    def closing_question(self, reference):
        """The same closing the ficha would use: visit for immediate delivery, brochure otherwise."""
        row = self.unit_row(reference)
        if row and property_delivery(row) == "inmediata":
            return "¿Quieres que coordinemos una visita al departamento?"
        return "¿Quieres que un asesor te envíe los planos y el brochure del proyecto?"

    def sent_media(self, phone, urls):
        """Which of these URLs this phone already received."""
        if not urls:
            return set()
        response = requests.get(f"{self.url}/rest/v1/media_deliveries", headers=self.headers,
                                params={"select": "media_url", "phone": f"eq.{phone}",
                                        "media_url": "in.(" + ",".join(f'"{u}"' for u in urls) + ")"}, timeout=10)
        response.raise_for_status()
        return {row["media_url"] for row in response.json()}

    def record_media_delivery(self, phone, search_key, url):
        response = requests.post(f"{self.url}/rest/v1/media_deliveries", headers={**self.headers, "Prefer": "return=minimal"},
                                 json={"phone": phone, "search_key": search_key, "media_url": url}, timeout=10)
        response.raise_for_status()

    def record_query(self, search_id, message_id, criteria, cutoff, matches):
        payload = {"search_id": search_id, "request_message_id": message_id, "criteria_snapshot": criteria, "eligibility_cutoff_at": cutoff.isoformat(), "rules_version": INVENTORY_RULES_VERSION, "result_status": "executed" if matches else "no_eligible", "eligible_count": len(matches)}
        response = requests.post(f"{self.url}/rest/v1/inventory_query_log", headers={**self.headers, "Prefer": "return=representation"}, json=payload, timeout=10)
        response.raise_for_status()
        rows = response.json()
        if not rows:
            raise RuntimeError("Inventory query log was not created")
        query_id = rows[0]["query_id"]
        if matches:
            result_rows = [{"query_id": query_id, "property_id": item["property"]["property_id"], "rank_position": index, "presented_to_client": True, "eligibility_snapshot": {"rules_version": INVENTORY_RULES_VERSION, "eligible": True}, "public_snapshot": item["public"]} for index, item in enumerate(matches, start=1)]
            result = requests.post(f"{self.url}/rest/v1/inventory_query_results", headers={**self.headers, "Prefer": "return=minimal"}, json=result_rows, timeout=10)
            result.raise_for_status()


def get_inventory_store():
    supabase_url, supabase_key = os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")
    return InventoryStore(supabase_url, supabase_key) if supabase_url and supabase_key else None


THIRD_PARTY_LABEL = "publicada por un tercero"
THIRD_PARTY_NOTE = ("Las publicadas por terceros no son captaciones de ARENZ: reconfirmamos "
                    "disponibilidad con el anunciante antes de coordinar una visita.")


def is_third_party(public):
    return (public or {}).get("provenance") == THIRD_PARTY


def inventory_reply(matches):
    if not matches:
        return "No encontré opciones verificadas que coincidan exactamente con tus criterios en este momento. Puedo ampliar distrito, presupuesto o derivar tu solicitud a un asesor."
    borrowed = any(is_third_party(item["public"]) for item in matches)
    # Calling a listing ARENZ neither owns nor captured "verificada" would be exactly
    # the false claim this classification exists to prevent.
    own_stock = all((item["public"].get("provenance") or "own") == "own" for item in matches)
    lines = ["Encontré opciones verificadas que coinciden con tus criterios:" if own_stock
             else "Encontré estas opciones para tu búsqueda:"]
    for index, item in enumerate(matches, 1):
        public = item["public"]
        description = public.get("public_description") or ""
        identity = re.match(r"^Proyecto (.*?); unidad (.*?)\.(?:\s|$)", description)
        title = f"{identity.group(1)} · Unidad {identity.group(2)}" if identity else public["property_type"].capitalize()
        amount = f"{float(public['price_amount']):,.2f}".rstrip("0").rstrip(".")
        amount = amount.translate(str.maketrans({",": ".", ".": ","}))
        symbol = {"PEN": "S/", "USD": "US$"}.get(public["currency"], public["currency"])
        lines.append(f"\n{index}. {title} · {public['district']}")
        lines.append(f"Ref. {public['public_reference']}")
        details = [f"{symbol} {amount}"]
        if public.get("bedrooms") is not None:
            details.append(f"{public['bedrooms']:g} dormitorios")
        area = public.get("area_total_m2") or public.get("area_m2")
        if area:
            details.append(f"{float(area):g}".replace(".", ",") + " m²")
        if public.get("bathrooms") is not None:
            details.append(f"{float(public['bathrooms']):g}".replace(".", ",") + " baños")
        if public.get("parking_spaces"):
            details.append(f"{public['parking_spaces']} estacionamiento(s)")
        elif public.get("parking_spaces") == 0:
            details.append("sin estacionamiento incluido")
        else:
            details.append("estacionamiento por confirmar")
        if is_third_party(public):
            details.append(THIRD_PARTY_LABEL)
        lines.append(" · ".join(details) + ".")
        if identity:
            for label, pattern in (("Piso", r"\bPiso ([^.]+)\."), ("Entrega según fuente", r"Entrega según fuente: ([^.]+)\.")):
                value = re.search(pattern, description)
                if value:
                    lines.append(f"{label}: {value.group(1)}.")
    if borrowed:
        lines.append(THIRD_PARTY_NOTE)
    return "\n".join(lines)


# "referencia" goes first and the code must contain a digit, so "referencia" is never read as "ref" + "erencia".
PRESENTED_REFERENCE_PATTERN = re.compile(r"\b(?:referencia|ref\.?)\s*((?=[a-z-]*\d)[a-z0-9][a-z0-9-]{1,23})\b", re.IGNORECASE)
CALLBACK_REQUEST_PATTERN = re.compile(r"\bll[aá]mame\b|\bll[aá]menme\b|\bque me llamen\b|\bprefiero (?:una )?llamada\b|\bcont[aá]ct(?:enme|ame) por tel[eé]fono\b")
VISIT_REQUEST_PATTERN = re.compile(r"\bvisita\b|\bvisitar\b|\bver (?:la|el) (?:propiedad|departamento|casa)\b")
PROPERTY_INTEREST_PATTERN = re.compile(r"\bme interesa\b|\bm[aá]s informaci[oó]n\b|\bquiero informaci[oó]n\b")
ADVISOR_REQUEST_PATTERN = re.compile(r"\basesor\b|\bhablar con (?:un|una) (?:persona|humano|humana)\b|\bagente humano\b")
REFERENCED_HANDOFF_TYPES = {"visit", "property_interest"}


def presented_property_references(previous, inventory_matches=None):
    """Only references ARENZ actually showed the client may back a handoff."""
    references = {item["public"]["public_reference"] for item in (inventory_matches or [])
                  if isinstance(item.get("public"), dict) and item["public"].get("public_reference")}
    for turn in recent_conversation_turns(previous):
        if turn.get("direction") == "assistant":
            references.update(PRESENTED_REFERENCE_PATTERN.findall(turn.get("content") or ""))
    return references


def mentioned_property_reference(text, presented):
    """Never fabricate a reference: match only what was previously presented."""
    normalized = " ".join((text or "").casefold().split())
    for reference in sorted(presented, key=len, reverse=True):
        if re.search(rf"(?<![a-z0-9]){re.escape(reference.casefold())}(?![a-z0-9])", normalized):
            return reference
    return None


def latest_presented_references(previous):
    """References of the most recent assistant message that listed units (an advisor note in between does not erase them)."""
    for turn in reversed(recent_conversation_turns(previous, limit=8)):
        if turn["direction"] == "assistant":
            found = list(dict.fromkeys(PRESENTED_REFERENCE_PATTERN.findall(turn["content"])))
            if found:
                return found
    return []


SELECTION_PATTERN = re.compile(r"(?:^|\b(?:opci[oó]n|departamento|depa|dpto|unidad|n[uú]mero|la|el|nro|#)\s*)([1-9])\b")
QUALIFYING_QUESTION = re.compile(r"dormitorios necesitas|pagarlo|para cu[aá]ndo|presupuesto|distrito", re.IGNORECASE)


def numbered_references(previous):
    """{número mostrado: referencia} from the latest listing, so '4' works on the second page."""
    for turn in reversed(recent_conversation_turns(previous, limit=8, raw=True)):
        if turn["direction"] == "assistant" and PRESENTED_REFERENCE_PATTERN.search(turn["content"]):
            pairs = re.findall(r"(?m)^(\d{1,2})\.\s[^\n]*\n[^\n]*?\bRef\.\s*([A-Za-z0-9-]+)", turn["content"])
            return {int(n): ref for n, ref in pairs}
    return {}


def selected_option(previous, text):
    normalized = (text or "").casefold().strip().strip(".!¡¿?")
    turns = recent_conversation_turns(previous)
    last = next((t["content"] for t in reversed(turns) if t["direction"] == "assistant"), "")
    references = latest_presented_references(previous)
    if references and not QUALIFYING_QUESTION.search(last) and len(normalized) <= 80:
        loose = SELECTION_PATTERN.search(normalized)
        if loose and not re.search(r"\d\s*(dorm|m2|m²|mil|k\b|soles|d[oó]lares|ba[ñn]o)", normalized):
            index = int(loose.group(1))
            numbered = numbered_references(previous)
            reference = numbered.get(index) or (references[index - 1] if index <= len(references) else None)
            return {"index": index, "reference": reference, "count": len(references)}
        if len(references) == 1 and "Responde con el número" in last and AFFIRMATIVE_PATTERN.match(" ".join(normalized.split())):
            return {"index": 1, "reference": references[0], "count": 1}
    match = re.fullmatch(r"(?:la\s+|el\s+)?(?:opci[oó]n\s*)?([1-9])", normalized)
    ordinal = {"la primera": 1, "el primero": 1, "primera": 1,
               "la segunda": 2, "el segundo": 2, "segunda": 2,
               "la tercera": 3, "el tercero": 3, "tercera": 3}.get(normalized)
    if not match and ordinal is None:
        return None
    turns = recent_conversation_turns(previous)
    last = next((t["content"] for t in reversed(turns) if t["direction"] == "assistant"), "")
    references = latest_presented_references(previous)
    # A bare count answers a bedroom/payment question, not an old property list.
    if not references or not re.search(r"cu[aá]l de estas|opci[oó]n|elegiste|unidad.*confirmar", last, re.I):
        return None
    index = int(match.group(1)) if match else ordinal
    return {"index": index, "reference": references[index - 1] if index <= len(references) else None,
            "count": len(references)}


def selected_unit_reply(item):
    details = inventory_reply([item]).split("\n", 1)[1].strip()
    details = re.sub(r"^1\.\s*", "", details)
    return "Revisemos esta unidad:\n\n" + details + "\n\n¿Qué característica te gustaría confirmar de esta unidad?"


PHOTO_WORDS = re.compile(r"\bfotos?\b|\bim[aá]genes\b|\bvideos?\b", re.IGNORECASE)
LOCATION_WORDS = re.compile(r"ubica|direcci|d[oó]nde|cerca|referencia|lugares|mapa|avenida|calle", re.IGNORECASE)
ADVISOR_OFFER = "¿Quieres que te contacte un asesor?"


def unit_question_reply(previous, item, user_text, observation):
    """Answer a question about the unit already shown, instead of resending its card."""
    public = item["public"]
    found = re.match(r"^Proyecto (.*?); unidad (.*?)\.", public.get("public_description") or "")
    name = f"{found.group(1)} (unidad {found.group(2)})" if found else f"La unidad Ref. {public['public_reference']}"
    if conversacion_ia_activa():
        # The observation already read the project card: no second OpenAI call.
        natural = ai_reply_text(observation, previous)
        if natural:
            return natural if "?" in natural else f"{natural} {UNIT_FOLLOW_UP}"
    card = "" if conversacion_ia_activa() else clean_card_text(project_card_text(item))
    if card:
        brief = ai_text(
            f"Eres la asesora de {COMMERCIAL_BRAND}. Responde SOLO lo que el cliente pregunta sobre este departamento, en 1 a 3 "
            "líneas para WhatsApp, usando únicamente la tarjeta del proyecto y los datos de la unidad. Si pregunta por ubicación, "
            "da la dirección, 2-3 referencias cercanas y el enlace de Google Maps si existe. Nunca menciones inmobiliaria, "
            "constructora, desarrolladora, contactos ni teléfonos. Si el dato no está, dilo y ofrece que un asesor lo confirme. "
            "Termina con una pregunta breve para continuar.",
            f"Pregunta: {user_text}\nDatos de la unidad: {unit_facts(item)}\nTarjeta del proyecto:\n{card}", 400)
        if brief and brief.strip() != last_assistant_message(previous).strip():
            return brief
    if LOCATION_WORDS.search(user_text or ""):
        extra = public.get("public_location_reference") or public.get("zone")
        where = f"en {public['district']}" + (f", {extra}" if extra else "")
        if item_delivery(item) == "inmediata":
            follow = f"Un asesor de {COMMERCIAL_BRAND} puede enviarte la ubicación exacta y coordinar una visita al departamento."
        else:
            # Under construction or on plans: there is no unit to visit yet.
            follow = (f"Como el proyecto aún está en construcción, un asesor de {COMMERCIAL_BRAND} puede enviarte la ubicación exacta, "
                      "los planos y el brochure del proyecto, y resolver tus dudas por llamada.")
        reply = f"{name} está {where}. En mi ficha no tengo la dirección exacta ni los lugares cercanos. {follow} {ADVISOR_OFFER}"
    else:
        natural = (None if conversacion_ia_activa() else
                   usable_assistant_reply(observation, "conversation") if isinstance(observation, dict) else None)
        reply = natural or (f"De {name} tengo el precio, dormitorios, área, piso y fecha de entrega que te mostré. "
                            f"Para otros detalles, un asesor de {COMMERCIAL_BRAND} puede ayudarte. {ADVISOR_OFFER}")
    if reply.strip() == last_assistant_message(previous).strip():
        reply = f"Para darte ese detalle con precisión, lo mejor es que te contacte un asesor de {COMMERCIAL_BRAND}. {ADVISOR_OFFER}"
    return reply


def unit_project_name(item):
    found = re.match(r"^Proyecto (.*?); unidad", item["public"].get("public_description") or "")
    return found.group(1) if found else None


def project_card_text(item):
    store = get_inventory_store()
    if not store:
        return ""
    try:
        return store.project_card(item["public"]["public_reference"])
    except (requests.RequestException, ValueError):
        logger.warning("Project card unavailable")
        return ""


CHARGE_NOTICE = ("⚠️ Importante: este inmueble registra una carga que se encuentra en proceso de levantamiento. "
                 f"Para conocer el detalle y cómo afecta la compra, consúltalo con un asesor de {COMMERCIAL_BRAND}.")
NOTICE_TEXTS = {"carga": CHARGE_NOTICE}


def unit_notice_text(item):
    """General client-facing notice for a unit (e.g. a legal charge), '' if none. The internal detail is never shown."""
    store = get_inventory_store()
    if not store:
        return ""
    try:
        code = store.unit_notice(item["public"]["public_reference"])
    except (requests.RequestException, ValueError, AttributeError):
        logger.warning("Unit notice unavailable")
        return ""
    return NOTICE_TEXTS.get(code.strip().casefold(), code.strip()) if code else ""


def with_notice(text, item):
    notice = unit_notice_text(item)
    return f"{text}\n\n{notice}" if notice and notice not in text else text


def clean_card_text(card):
    """Drop the internal header of a Trello card (owner, contacts, internal comments) before the model sees it."""
    text = card or ""
    for marker in ("📍", "DIRECCI", "UBICACI", "TITULO DEL INMUEBLE", "DESCRIPCI"):
        position = text.find(marker)
        if position > 0:
            text = text[position:]
            break
    text = re.sub(r"(?im)^.*(n[uú]mero de contacto|contacto de visitas|t[ií]tulares?|empresa desarrolladora|precio al cierre|embargo).*$", "", text)
    # Ningún precio de la tarjeta: puede diferir del inventario (Universo 407: tarjeta S/ 285,000 "negociable",
    # inventario S/ 295,000) y el cliente recibía dos precios distintos.
    text = re.sub(r"(?im)^.*(precio|ratio de venta|negociable).*$", "", text)
    return text[:6000]


def unit_facts(item):
    public = item["public"]
    facts = {key: public.get(key) for key in ("public_reference", "district", "property_type", "price_amount", "currency",
                                              "bedrooms", "bathrooms", "area_total_m2", "area_m2", "parking_spaces",
                                              "public_description")}
    facts["entrega"] = {"inmediata": "entrega inmediata", "futura": "en construcción / planos",
                        "por_confirmar": "fecha por confirmar"}[item_delivery(item)]
    return json.dumps(facts, ensure_ascii=False, default=str)


FICHA_RULES = (
    "Eres la asesora de {brand}. Redacta la ficha de UN departamento para WhatsApp en español, con este formato y orden "
    "(omite una sección o línea si no hay datos: nunca escribas 'no especificado', 'no indicado' ni similares; nunca inventes): "
    "'🏠 DEPARTAMENTO EN VENTA – <PROYECTO>, <DISTRITO>' en mayúsculas; '✨' resumen de 1-2 líneas; "
    "'💰 PRECIO DE VENTA: <moneda y monto>'; '🚗' estacionamiento (si no está confirmado: 'Estacionamiento: por confirmar'); "
    "'🏡 CARACTERÍSTICAS DEL DEPARTAMENTO' (área, piso, dormitorios, baños, vista, acabados) con un emoji por línea; "
    "'🏗️ CARACTERÍSTICAS DEL EDIFICIO' (pisos, antigüedad o etapa y fecha de entrega, financiamiento bancario si lo hay); "
    "'🌟 ÁREAS COMUNES'; '📍 UBICACIÓN' (dirección); '📌 ENTORNO Y CONECTIVIDAD' (avenidas, colegios, comercios); "
    "'🗺️ GOOGLE MAPS' (solo el enlace si existe); '📑 FINANCIAMIENTO' (crédito hipotecario, AFP si aplica). "
    "Los datos de la UNIDAD (precio, dormitorios, área, piso, entrega) mandan sobre la tarjeta del proyecto. "
    "PROHIBIDO mencionar el nombre de la inmobiliaria, constructora, desarrolladora o empresa comercializadora, titulares, "
    "nombres de contactos internos, teléfonos, 'XXXXX', precios de cierre u observaciones internas, "
    "ni embargos, cargas, hipotecas o situación legal (eso lo informa el sistema aparte). "
    "Escribe los montos como 'S/ 295,000' para soles y 'US$ 150,000' para dólares (nunca PEN ni USD). "
    "Termina con una sola pregunta de cierre: {closing} Máximo 2500 caracteres. Solo el texto de la ficha."
)


def ai_text(instructions, content, max_tokens):
    if not OPENAI_API_KEY:
        return None
    try:
        response = post_openai_response({"model": OPENAI_MODEL, "store": False, "max_output_tokens": max_tokens,
                                         "instructions": instructions, "input": content})
        response.raise_for_status()
        return (response_output_text(response.json()) or "").strip() or None
    except (requests.RequestException, ValueError, KeyError, TypeError):
        logger.warning("AI text unavailable; deterministic reply used")
        return None


def full_unit_reply(item):
    """The complete ficha, sent once the client picks a unit; the brief card stays as fallback."""
    reference = item["public"]["public_reference"]
    closing = ("'¿Quieres que coordinemos una visita al departamento?'" if item_delivery(item) == "inmediata"
               else "'¿Quieres que un asesor te envíe los planos y el brochure del proyecto?'")
    card = clean_card_text(project_card_text(item))
    text = ai_text(FICHA_RULES.format(brand=COMMERCIAL_BRAND, closing=closing),
                   f"Datos de la unidad: {unit_facts(item)}\nTarjeta del proyecto:\n{card or '(sin tarjeta)'}", 1400)
    if not text:
        return with_notice(selected_unit_reply(item), item)
    # The general notice goes last, after the closing question, as the operator asked.
    return with_notice(f"{text}\n\nRef. {reference}", item)


def parking_question_reply(item):
    public = item["public"]
    spaces = public.get("parking_spaces")
    reference = public["public_reference"]
    # Corto y en lenguaje del cliente: la versión anterior explicaba el trámite interno del asesor (prueba del 03/11).
    if spaces is None:
        fact = "El estacionamiento de este departamento está por confirmar 🚗"
    elif spaces == 0:
        fact = "Este departamento no incluye estacionamiento en el precio 🚗"
    else:
        fact = f"La ficha registra {spaces} estacionamiento(s), pero falta confirmar si está incluido en el precio 🚗"
    return (f"Ref. {reference}\n{fact} Un asesor de {COMMERCIAL_BRAND} puede confirmarte si hay uno disponible y, si se "
            f"vende por separado, su precio. {ADVISOR_OFFER}")


def is_selected_parking_question(previous, text):
    normalized = (text or "").casefold()
    return ((previous or {}).get("stage") == "property_detail"
            and len(latest_presented_references(previous)) == 1
            and bool(re.search(r"estacionamiento|cochera|parking", normalized))
            and bool(re.search(r"\?|confirm|falta|inclu|cu[aá]nt|precio|disponib|tiene|cuenta", normalized)))


def handoff_request_type(text, reference):
    """Deterministic escalation types. Visit and interest need a real reference."""
    normalized = " ".join((text or "").casefold().split())
    if CALLBACK_REQUEST_PATTERN.search(normalized):
        return "callback"
    if reference and VISIT_REQUEST_PATTERN.search(normalized):
        return "visit"
    if reference and PROPERTY_INTEREST_PATTERN.search(normalized):
        return "property_interest"
    if ADVISOR_REQUEST_PATTERN.search(normalized):
        return "advisor"
    return None


AFFIRMATIVE_PATTERN = re.compile(r"^(?:s[ií]|ok|okay|claro|dale|de acuerdo|por favor|ya|bueno|perfecto)\b[\s,.!]*(?:por favor|gracias|claro)?[\s.!]*$")


VISIT_OFFER = "coordinemos una visita"
BROCHURE_OFFER = "envíe los planos"


def accepted_advisor_offer(previous, user_text):
    """'Sí' right after the bot offered an advisor, a visit or the brochure: which offer was accepted, or None."""
    if not AFFIRMATIVE_PATTERN.match(" ".join((user_text or "").casefold().split())):
        return None
    last = last_assistant_message(previous)
    if VISIT_OFFER in last:
        return "visit"
    if BROCHURE_OFFER in last:
        return "property_interest"
    if "te contacte un asesor" in last:
        return "advisor"
    return None


def detected_handoff(previous, observation, user_text, inventory_matches=None):
    """Resolve one escalation per turn, or None to continue the qualification."""
    reference = mentioned_property_reference(user_text, presented_property_references(previous, inventory_matches))
    request_type = handoff_request_type(user_text, reference)
    if (not request_type and isinstance(observation, dict) and (observation.get("handoff") or observation.get("intent") == "human_handoff")
            and re.search(r"asesor|humano|persona|ll[aá]m|contact|visita|agend|hablar con", user_text or "", re.IGNORECASE)):
        # The model's handoff flag alone is not enough: it tends to stick once a handoff happened.
        request_type = "advisor"
    accepted = None if request_type else accepted_advisor_offer(previous, user_text)
    if accepted:
        request_type = accepted
        if accepted in REFERENCED_HANDOFF_TYPES and not reference:
            shown = latest_presented_references(previous)
            reference = shown[0] if len(shown) == 1 else None
            if not reference:
                request_type = "advisor"
    if not request_type:
        return None
    return {"request_type": request_type, "property_reference": reference if request_type in REFERENCED_HANDOFF_TYPES else None}


def handoff_reply(handoff):
    request_type, reference = handoff["request_type"], handoff["property_reference"]
    if request_type == "callback":
        return (f"Perfecto. Registré tu solicitud de llamada; un asesor de {COMMERCIAL_BRAND} coordinará contigo 🙌",
                "Solicitud de llamada registrada con consentimiento explícito.")
    if request_type == "visit":
        return (f"Perfecto. Registré tu solicitud de visita para la referencia {reference}; un asesor de {COMMERCIAL_BRAND} coordinará contigo 🙌",
                f"Solicitud de visita registrada para la referencia {reference}.")
    if request_type == "property_interest":
        return (f"Perfecto. Registré tu interés en la referencia {reference}; un asesor de {COMMERCIAL_BRAND} continuará contigo 🙌",
                f"Interés registrado en la referencia {reference}.")
    return (f"Perfecto. Registraré tu solicitud para que un asesor de {COMMERCIAL_BRAND} continúe contigo 🙌",
            "Solicitud de asesor registrada.")


def _handoff_payload(phone, handoff, search_id, criteria):
    """Shape the row exactly as the handoff_requests constraints require."""
    consent = handoff["request_type"] == "callback"
    return {"phone": phone, "search_id": search_id, "request_type": handoff["request_type"],
            "criteria_snapshot": criteria or {}, "property_reference": handoff["property_reference"],
            "contact_preference": "phone_call" if consent else None, "callback_consent": consent}


def customer_pause_reply(text):
    normalized = " ".join((text or "").casefold().split())
    if any(phrase in normalized for phrase in ("no me escribas", "no me contacten", "no me contactes", "no me interesa")):
        return f"Entendido. Respetaré tu decisión y cierro esta consulta. Gracias por escribir a {COMMERCIAL_BRAND}.", "do_not_contact"
    family_review = re.search(r"(?:voy a|quiero|necesito|debo|tengo que|lo)\s+consult\w*.{0,35}(?:familia|hijas|hijos|esposa|esposo|pareja)", normalized)
    if family_review or any(phrase in normalized for phrase in ("consultar con mi familia", "consultarlo con mi familia", "consultarlo con mis hijas", "consultar con mis hijas", "consultarlo con mi esposa", "consultarlo con mi esposo", "lo voy a consultar", "te escribo después", "te escribo luego", "yo te aviso", "yo les aviso")):
        return "Claro, revísalo con tranquilidad. Cuando quieras retomar la búsqueda, escríbeme por aquí; con gusto te ayudo.", "awaiting_customer"
    return None


NEGATIVE_PATTERN = re.compile(r"^(?:no|nop|nel|no gracias|no,? gracias|por ahora no|ahora no|todav[ií]a no|no quiero|no deseo|no necesito|no es necesario|nada m[aá]s|eso es todo|as[ií] est[aá] bien)\b", re.IGNORECASE)
THANKS_PATTERN = re.compile(r"^(?:ok|okay|gracias|muchas gracias|ok,? gracias|listo|perfecto|vale|genial|bien|entendido)\b[\s,.!👍🙏]*(?:gracias)?[\s.!👍🙏]*$", re.IGNORECASE)
OFFER_DECLINED_REPLY = ("Entendido, no te contactará un asesor. Si más adelante quieres revisar otra opción, ver más fotos "
                        "o cambiar tu búsqueda, escríbeme por aquí. ¡Gracias por escribir a {brand}! 😊")
# Base sin emoticon: también reconoce el mensaje en conversaciones guardadas antes de añadirlo.
CLOSING_THANKS_BASE = "¡Con gusto! Aquí estaré cuando quieras retomar tu búsqueda."
CLOSING_THANKS_REPLY = CLOSING_THANKS_BASE + " 😊"


def declined_offer_reply(previous, text):
    """'No' to the advisor, brochure or visit offer closes the conversation instead of re-describing the unit."""
    normalized = " ".join((text or "").casefold().split()).strip(".!¡")
    last = last_assistant_message(previous)
    offered = BROCHURE_OFFER in last or VISIT_OFFER in last or "te contacte un asesor" in last
    if offered and NEGATIVE_PATTERN.match(normalized) and not re.search(r"\b(?:pero|otra|otro|m[aá]s opciones|cambi)", normalized):
        return OFFER_DECLINED_REPLY.format(brand=COMMERCIAL_BRAND)
    if last.startswith("Entendido, no te contactará un asesor") and THANKS_PATTERN.match(normalized):
        return CLOSING_THANKS_REPLY
    return None


PAYMENT_QUESTION = "¿cómo piensas pagarlo: al contado, con crédito hipotecario o aún lo estás evaluando?"
TIMELINE_QUESTION = "¿para cuándo necesitas el departamento? Por ejemplo: lo antes posible, en los próximos meses o sin apuro."


def commercial_next_question(criteria):
    if criteria.get("operation") == "compra" and not criteria.get("payment_method"):
        return "Para orientarte mejor, " + PAYMENT_QUESTION
    if not criteria.get("purchase_timeline"):
        return TIMELINE_QUESTION[0].upper() + TIMELINE_QUESTION[1:]
    return None


def inventory_conversation_reply(matches, criteria):
    """Keep the verified listing reply and one useful next step on every path."""
    reply = inventory_reply(matches)
    if matches:
        if not criteria.get("purchase_timeline"):
            dates = list(dict.fromkeys(m.group(1) for item in matches
                for m in re.finditer(r"Entrega según fuente: ([^.]+)\.", item["public"].get("public_description") or "")))
            if dates:
                question = f"Estas opciones indican entrega en {' / '.join(dates)}. ¿Para cuándo necesitas tener el departamento?"
            else:
                question = commercial_next_question(criteria)
        else:
            question = commercial_next_question(criteria)
        if not question:
            question = "¿Cuál de estas unidades te gustaría revisar con más detalle?"
    else:
        question = "¿Prefieres ampliar las zonas de búsqueda, revisar el presupuesto o que te contacte un asesor?"
    return reply + "\n\n" + question


IMMEDIATE_WORDS = re.compile(r"inmediat|entregad|lo antes posible|cuanto antes|lo m[aá]s pronto|urgente|ya mismo|\bpronto\b|este a[ñn]o|este mes|pr[oó]ximo mes|\bsemanas?\b|fin de a[ñn]o|\bya\b|para mudarme|\b\d+\s*mes|enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre", re.IGNORECASE)
FUTURE_YEAR = re.compile(r"20(2[7-9]|3\d)")
FUTURE_WORDS = re.compile(r"preventa|en plano|en construcci|futur|pr[oó]ximos a[ñn]os|pr[oó]ximo a[ñn]o|sin apuro|no tengo apuro|no hay apuro|20(2[7-9]|3\d)|a[ñn]os", re.IGNORECASE)
BOTH_WORDS = re.compile(r"\bambas\b|\blas dos\b|\btodas\b|\bambos\b", re.IGNORECASE)
DELIVERY_QUESTION = "¿Te muestro las de entrega inmediata o las de entrega en los próximos años?"


def delivery_preference_from_text(text):
    text = text or ""
    if BOTH_WORDS.search(text):
        return "ambas"
    if FUTURE_YEAR.search(text):
        return "futura"
    if IMMEDIATE_WORDS.search(text):
        return "inmediata"
    if FUTURE_WORDS.search(text):
        return "futura"
    return None


def item_delivery(item):
    return item.get("delivery") or property_delivery({**item.get("public", {}), "source_name": None})


def matches_summary(matches):
    summary = getattr(matches, "summary", None)
    if summary:
        return summary
    counts = {bucket: 0 for bucket in DELIVERY_BUCKETS}
    years = []
    for item in matches:
        bucket = item_delivery(item)
        counts[bucket] += 1
        label = re.search(r"Entrega según fuente: ([^.]+)\.", item.get("public", {}).get("public_description") or "")
        when = delivery_date(label.group(1)) if label and bucket == "futura" else None
        if when:
            years.append(when[0])
    counts["years"] = (min(years), max(years)) if years else None
    return counts


MORE_WORDS = re.compile(r"\bm[aá]s opciones\b|\bver m[aá]s\b|^m[aá]s\b|\botr[oa]s?\b|siguientes|\bresto\b|dem[aá]s|\btodas\b|\btodos\b", re.IGNORECASE)


def is_more_request(text):
    return bool(MORE_WORDS.search(text or "")) and not re.search(r"b[uú]squeda|distrito|presupuesto", text or "", re.IGNORECASE)


def shown_references(previous):
    refs = []
    for turn in recent_conversation_turns(previous, limit=12, raw=True):
        if turn["direction"] == "assistant":
            refs += [m.group(1).upper() for m in PRESENTED_REFERENCE_PATTERN.finditer(turn["content"])]
    return list(dict.fromkeys(refs))


def brief_listing(items, start=1):
    """Two short lines per unit; the full ficha is sent only after the client picks one."""
    lines = []
    for index, item in enumerate(items, start):
        public = item["public"]
        project = unit_project_name(item) or public["property_type"].capitalize()
        symbol = {"PEN": "S/", "USD": "US$"}.get(public["currency"], public["currency"])
        parts = [f"{symbol} {float(public['price_amount']):,.0f}"]
        if public.get("bedrooms") is not None:
            parts.append(f"{public['bedrooms']:g} dorm")
        area = public.get("area_total_m2") or public.get("area_m2")
        if area:
            parts.append(f"{float(area):g} m²")
        label = re.search(r"Entrega según fuente: ([^.]+)\.", public.get("public_description") or "")
        parts.append("entrega inmediata" if item_delivery(item) == "inmediata" else f"entrega {label.group(1)}" if label else "entrega por confirmar")
        lines.append(f"\n{index}. {project} · {public['district']}\n{' · '.join(parts)} · Ref. {public['public_reference']}")
    return "\n".join(lines)


def delivery_reply(previous, matches, criteria, user_text):
    """After the five filters: say what exists by delivery, then show only the bucket the client wants."""
    counts = matches_summary(matches)
    offered_future = "¿Te muestro las de entrega en los próximos años?" in last_assistant_message(previous)
    offered_immediate = "¿Te muestro las de entrega inmediata?" in last_assistant_message(previous)
    pref = delivery_preference_from_text(user_text)
    if not pref and AFFIRMATIVE_PATTERN.match(" ".join((user_text or "").casefold().split())):
        pref = "futura" if offered_future else "inmediata" if offered_immediate else None
    pref = pref or criteria.get("delivery_preference") or delivery_preference_from_text(criteria.get("purchase_timeline"))
    years = counts.get("years")
    future_label = f"con entrega entre {years[0]} y {years[1]}" if years and years[0] != years[1] else (
        f"con entrega en {years[0]}" if years else "con entrega en los próximos años")
    immediate, future, unknown = counts["inmediata"], counts["futura"], counts["por_confirmar"]
    parts = []
    if immediate:
        parts.append(f"{immediate} de entrega inmediata")
    if future:
        parts.append(f"{future} {future_label}")
    if unknown:
        parts.append(f"{unknown} con fecha de entrega por confirmar")
    summary = criteria_summary(criteria)
    lead = f"Con lo que me contaste ({summary}) tengo " + (", ".join(parts[:-1]) + " y " + parts[-1] if len(parts) > 1 else parts[0]) + "."
    available = [b for b in ("inmediata", "futura", "por_confirmar") if counts[b]]
    if not pref and len(available) == 1:
        pref = available[0]
    if not pref:
        return lead + " " + DELIVERY_QUESTION, criteria
    criteria = {**criteria, "delivery_preference": pref}
    wanted = ("inmediata", "futura", "por_confirmar") if pref == "ambas" else (pref,)
    pool = [item for item in matches if item_delivery(item) in wanted]
    if is_more_request(user_text):
        already = shown_references(previous)
        rest = [item for item in pool if item["public"]["public_reference"] not in already]
        if rest:
            page = rest[:INVENTORY_MAX_RESULTS]
            left = len(rest) - len(page)
            tail = (f"Me quedan {left} opciones más; escribe 'más' para verlas." if left else "Esas son todas las que tengo con tus criterios.")
            return (f"Aquí tienes {len(page)} opciones más:\n{brief_listing(page, start=len(already) + 1)}\n\n{tail} "
                    "Responde con el número de la opción que te interesa y te envío la ficha completa y fotografías del proyecto."), criteria
        return ("Ya te mostré todas las opciones que tengo con tus criterios. Si quieres, amplío el distrito o el presupuesto. "
                "¿Qué prefieres?"), criteria
    shown = pool[:INVENTORY_MAX_RESULTS]
    if not shown and pref == "inmediata" and future:
        return lead + " Por ahora no tengo de entrega inmediata con esos criterios. ¿Te muestro las de entrega en los próximos años?", criteria
    if not shown and pref == "futura" and immediate:
        return lead + " Con esos criterios solo tengo de entrega inmediata. ¿Te muestro las de entrega inmediata?", criteria
    if not shown:
        shown = list(matches)[:INVENTORY_MAX_RESULTS]
    label = {"inmediata": "las de entrega inmediata", "futura": "las de entrega en los próximos años",
             "por_confirmar": "las que tienen fecha por confirmar", "ambas": "las mejores opciones"}[pref]
    listing = brief_listing(shown)
    return f"{lead} Te muestro {label}:\n{listing}\n\nResponde con el número de la opción que te interesa y te envío la ficha completa y fotografías del proyecto.", criteria


NO_MATCH_PREFIX = "No encontré opciones verificadas"
SHORT_NUMBER_PATTERN = re.compile(r"^\D{0,20}?(\d{1,2})\b(?!\s*(?:mil|k|m2|m²|metros|usd|soles|d[oó]lares))", re.IGNORECASE)


def last_assistant_message(previous):
    turns = [t for t in recent_conversation_turns(previous, limit=8) if t["direction"] == "assistant"]
    return turns[-1]["content"] if turns else ""


def answered_count_question(previous, user_text, criteria):
    """A bare '3' right after the bot asked for bedrooms or bathrooms is that answer."""
    asked = last_assistant_message(previous).casefold()
    match = SHORT_NUMBER_PATTERN.match((user_text or "").strip())
    if not match or len((user_text or "").strip()) > 40:
        return criteria
    value = int(match.group(1))
    updated = dict(criteria)
    if "dormitorio" in asked or "habitaci" in asked:
        if 0 < value <= 20:
            updated["bedrooms"] = value
    elif "baño" in asked and 0 < value <= 30:
        updated["bathrooms_min"] = value
    return updated


def criteria_summary(criteria):
    parts = []
    if criteria.get("property_type"):
        parts.append(criteria["property_type"])
    if criteria.get("districts"):
        parts.append("en " + ", ".join(criteria["districts"]))
    if criteria.get("budget_max"):
        symbol = {"PEN": "S/", "USD": "US$"}.get(criteria.get("currency"), "")
        parts.append(f"hasta {symbol} {criteria['budget_max']:,}".replace("  ", " ").strip())
    if criteria.get("bedrooms"):
        parts.append(f"{criteria['bedrooms']} dormitorios")
    if criteria.get("area_min_m2"):
        parts.append(f"desde {criteria['area_min_m2']:g} m²")
    return ", ".join(parts)


def no_match_followup(previous, criteria):
    """After a first 'no matches', stop repeating it: summarise and offer the advisor."""
    if not last_assistant_message(previous).startswith(NO_MATCH_PREFIX) and not (
            last_assistant_message(previous).startswith("Revisé") or last_assistant_message(previous).startswith("Por ahora")):
        return None
    summary = criteria_summary(criteria)
    changed = criteria != conversation_state(previous)
    lead = (f"Revisé con tus nuevos criterios ({summary}) y por ahora no tengo unidades verificadas que coincidan."
            if changed and summary else
            f"Por ahora no tengo unidades verificadas para {summary or 'tu búsqueda'}.")
    return (lead + " Un asesor de C2 Inmobiliaria puede buscarte opciones fuera de mi lista. "
            "¿Quieres que te contacte un asesor, o prefieres cambiar el distrito o el presupuesto?")


def qualified_inventory_reply(previous, matches, criteria, user_text):
    """Complete the five filters (qué, dónde, cuánto, cómo, cuándo) before talking about units."""
    if not matches:
        followup = no_match_followup(previous, criteria)
        if followup:
            return followup, criteria
        summary = criteria_summary(criteria)
        extra = " Incluiste estacionamiento obligatorio; puedo buscar también sin ese requisito." if criteria.get("parking_required") else ""
        return (f"Con tus criterios ({summary}) no encontré unidades verificadas en este momento.{extra} "
                "¿Prefieres ampliar los distritos, revisar el presupuesto o que te contacte un asesor?"), criteria
    teaser_sent = "Tengo opciones que podrían encajar" in last_assistant_message(previous)
    if not criteria.get("payment_method"):
        return "Tengo opciones que podrían encajar con lo que buscas. Antes de mostrártelas, " + PAYMENT_QUESTION, criteria
    if not criteria.get("purchase_timeline"):
        if teaser_sent:
            return "Perfecto, gracias. Último dato antes de mostrarte las opciones: " + TIMELINE_QUESTION, criteria
        return "Tengo opciones que podrían encajar con lo que buscas. Antes de mostrártelas, " + TIMELINE_QUESTION, criteria
    reply, criteria = delivery_reply(previous, matches, criteria, user_text)
    if reply.strip() == last_assistant_message(previous).strip():
        reply = ("Esas son las opciones que tengo con tus criterios. Responde con el número de la que te interese para enviarte "
                 "la ficha completa y fotografías del proyecto, o dime si prefieres cambiar el distrito, el presupuesto o los dormitorios.")
    return reply, criteria


def progressive_reply(previous, observation, fallback, user_text=None, prior_criteria=None, inventory_matches=None):
    """Policy layer: AI extracts; bounded code validates state and chooses the reply."""
    greeting = re.sub(r"[^\w\s]", "", (user_text or "").casefold()).strip()
    if greeting in {"hola", "buenos días", "buenos dias", "buenas tardes", "buenas noches"}:
        criteria = dict(prior_criteria if prior_criteria is not None else conversation_state(previous))
        if criteria:
            reply = f"Hola, soy {ASSISTANT_NAME}, asesora de {COMMERCIAL_BRAND} 😊 ¿Quieres retomar tu búsqueda anterior o comenzar una nueva?"
            return reply, criteria, "conversation", "Saludo atendido; criterios conservados sin consultar inventario."
        return INTRODUCTION, criteria, "qualification", "Saludo atendido antes de la calificación."
    observation = with_explicit_operation(observation, user_text)
    observation = sanitize_new_search_observation(observation, user_text)
    pause = customer_pause_reply(user_text)
    if pause:
        criteria = dict(prior_criteria if prior_criteria is not None else conversation_state(previous))
        if isinstance(observation, dict):
            criteria = apply_criteria_actions(criteria, observation.get("criteria_actions"))
            criteria.update(validated_slot_updates(observation.get("slot_updates")))
        return pause[0], criteria, pause[1], "Cliente solicita espacio o finalizar la consulta; sin seguimiento automático."
    declined = declined_offer_reply(previous, user_text)
    if declined:
        criteria = dict(prior_criteria if prior_criteria is not None else conversation_state(previous))
        return declined, criteria, "awaiting_customer", "Cliente no desea asesor por ahora; conversación cerrada sin seguimiento automático."
    if is_selected_parking_question(previous, user_text):
        criteria = dict(prior_criteria if prior_criteria is not None else conversation_state(previous))
        reference = latest_presented_references(previous)[0]
        if inventory_matches is None:
            return "Consultaré el dato de estacionamiento de esta unidad.", criteria, "property_detail", "Consulta de estacionamiento; criterios conservados."
        item = next((m for m in inventory_matches if m["public"]["public_reference"] == reference), None)
        if item:
            return parking_question_reply(item), criteria, "property_detail", f"Estacionamiento pendiente de confirmación para {reference}."
        return f"Ref. {reference}\nNo pude reconfirmar la ficha actual de esta unidad. El asesor debe verificar con el responsable del proyecto la disponibilidad y las condiciones del estacionamiento.", criteria, "property_detail", "Ficha no reconfirmada; sin inventar condiciones de estacionamiento."
    selection = selected_option(previous, user_text)
    if selection:
        criteria = dict(prior_criteria if prior_criteria is not None else conversation_state(previous))
        if not selection["reference"]:
            return f"Te mostré {selection['count']} opciones. ¿Cuál de esas opciones deseas revisar?", criteria, "qualified", "Selección fuera de rango; sin inventar una unidad."
        if inventory_matches is None:
            return "Revisaré la unidad que seleccionaste.", criteria, "property_detail", f"Selección de la referencia {selection['reference']}."
        item = next((m for m in inventory_matches if m["public"]["public_reference"] == selection["reference"]), None)
        if item:
            return full_unit_reply(item), criteria, "property_detail", f"Detalle de la referencia seleccionada {selection['reference']}."
        return "No pude reconfirmar la unidad que elegiste entre las opciones actuales. ¿Quieres revisar las alternativas vigentes?", criteria, "conversation", "Unidad anterior no reconfirmada; no se sustituye por otra posición."
    if not observation:
        if is_new_search_request(user_text):
            return "Nueva búsqueda iniciada. ¿En qué distrito o distritos te gustaría tu departamento?", {}, "qualification", "Nueva búsqueda creada con criterios vacíos."
        if (user_text or "").strip().casefold() in {"hola", "buenos días", "buenas tardes", "buenas noches"} and not conversation_state(previous):
            return INTRODUCTION, {}, "qualification", "Apertura cálida sin cuestionario."
        return fallback, conversation_state(previous), "fallback", "IA no disponible; flujo determinista aplicado."
    if prior_criteria is None:
        _, prior_criteria = search_state_for_turn(previous, observation, user_text)
    criteria = apply_criteria_actions(prior_criteria, observation.get("criteria_actions"))
    slots = validated_slot_updates(observation.get("slot_updates"))
    if observation.get("criteria_change") and "preferences" in slots and not observation.get("criteria_actions"):
        criteria["preferences"] = list(dict.fromkeys(criteria.get("preferences", []) + slots.pop("preferences")))
    criteria.update(slots)
    criteria = answered_count_question(previous, user_text, criteria)
    if criteria.get("operation") in ("alquiler", "venta"):
        criteria = {k: v for k, v in criteria.items() if k != "operation"}
        return (f"Por ahora en {COMMERCIAL_BRAND} solo ofrecemos departamentos en venta (flat, dúplex y otros). "
                "Si te interesa comprar uno, te ayudo a encontrarlo. ¿En qué distrito o distritos lo buscas?"), criteria, "qualification", "Solo venta de departamentos."
    intent = observation.get("intent")
    if intent == "greeting" and not criteria and not conversation_state(previous):
        return INTRODUCTION, criteria, "qualification", "Apertura cálida; escuchar la necesidad antes de calificar."
    handoff = detected_handoff(previous, observation, user_text, inventory_matches)
    if handoff:
        reply, summary = handoff_reply(handoff)
        if reply.strip() == last_assistant_message(previous).strip():
            reply = (f"Ya registré tu solicitud y un asesor de {COMMERCIAL_BRAND} te escribirá. Mientras tanto, puedo enviarte la "
                     "ficha de alguna opción o buscar con otros criterios. ¿Qué prefieres?")
        return reply, criteria, "handoff", summary
    if PHOTO_WORDS.search(user_text or ""):
        shown = latest_presented_references(previous)
        if len(shown) == 1:
            if (previous or {}).get("stage") == "handoff":
                return (f"Por ahora no tengo las fotos cargadas en el chat. Ya registré tu solicitud: el asesor de {COMMERCIAL_BRAND} "
                        f"que te contactará te enviará las fotos de la Ref. {shown[0]}."), criteria, "handoff", "Pedido de fotos tras derivación."
            return (f"Por ahora no tengo las fotos cargadas en el chat. Un asesor de {COMMERCIAL_BRAND} puede enviarte las fotos "
                    f"de la Ref. {shown[0]}. {ADVISOR_OFFER}"), criteria, "property_detail", "Pedido de fotos."
        if shown:
            return "Con gusto. ¿De cuál opción quieres las fotos? Respóndeme con su número.", criteria, "qualified", "Pedido de fotos sin unidad."
    if ((previous or {}).get("stage") == "property_detail" and inventory_matches is not None and not is_more_request(user_text)
            and not observation.get("criteria_change") and intent not in ("new_search", "change_criteria")):
        references = latest_presented_references(previous)
        if len(references) == 1:
            item = next((m for m in inventory_matches if m["public"]["public_reference"] == references[0]), None)
            if item:
                return unit_question_reply(previous, item, user_text, observation), criteria, "property_detail", f"Consulta sobre la unidad seleccionada {references[0]}."
    if intent == "general_question":
        natural_reply = usable_assistant_reply(observation, "conversation")
        if inventory_matches is not None and inventory_ready(criteria):
            reply, criteria = qualified_inventory_reply(previous, inventory_matches, criteria, user_text)
            return reply, criteria, "qualified", "Consulta de inventario con siguiente paso comercial."
        if natural_reply:
            return natural_reply, criteria, "conversation", "Consulta atendida; criterios conservados."
        return "Claro. Puedo orientarte sobre tu búsqueda inmobiliaria. ¿Qué deseas consultar?", criteria, "conversation", "Consulta libre; criterios conservados."
    missing = [("districts", "¿En qué distrito o distritos te gustaría tu departamento?"),
               ("budget_max", "¿Cuál es tu presupuesto máximo aproximado? Puedes indicarlo en soles o en dólares."),
               ("currency", "¿Ese monto es en soles o en dólares?"), ("bedrooms", "¿Cuántos dormitorios necesitas?")]
    for key, question in missing:
        if criteria.get(key) in (None, [], ""):
            if (is_new_search_request(user_text) or intent == "new_search") and key == "districts" and not criteria.get("districts"):
                return "Nueva búsqueda iniciada. ¿En qué distrito o distritos te gustaría tu departamento?", criteria, "qualification", "Nueva búsqueda creada con criterios vacíos."
            # Fixed questions: free model replies drifted (re-asking known districts, asking parking out of order).
            return question, criteria, "qualification", "Continuar calificación con el siguiente criterio faltante."
    if inventory_matches is not None and inventory_ready(criteria):
        reply, criteria = qualified_inventory_reply(previous, inventory_matches, criteria, user_text)
        return reply, criteria, "qualified", "Consulta de inventario respondida desde registros verificados."
    question = commercial_next_question(criteria)
    if question and criteria.get("operation") in {"compra", "alquiler"}:
        return "Tengo registrados tus criterios; aún no cuento con inventario verificado conectado para mostrarte propiedades reales. " + question, criteria, "qualified", "Completar los cinco filtros ARENZ sin repetir criterios conocidos."
    return "Tengo registrados tus criterios. Aún no cuento con inventario verificado conectado para mostrarte propiedades reales; puedo derivar tu solicitud a un asesor.", criteria, "qualified", "Criterios completos; inventario no conectado, sin afirmaciones sobre propiedades."


def persist_conversation_turn(sender, message_id, inbound, outbound, observation, criteria, stage, summary, previous=None, timings=None, search_state=None):
    memory = get_conversation_memory_store()
    if not memory:
        return
    observation = sanitize_new_search_observation(observation, inbound)
    turns = recent_conversation_turns(previous) + [{"direction": "user", "content": inbound}, {"direction": "assistant", "content": outbound}]
    search_state = search_state or search_state_for_turn(previous, observation, inbound)[0]
    active_id = search_state.get("active_search_id")
    if active_id:
        search_state["searches"][active_id]["criteria"] = criteria
        search_state["searches"][active_id]["recent_turns"] = turns[-4:]
    prior_state = previous.get("state", {}) if isinstance(previous, dict) else {}
    legacy_searches = prior_state.get("legacy_searches", prior_state.get("searches", {})) if isinstance(prior_state, dict) else {}
    # Legacy/local mode retains its existing durable adapter. Supabase mode must
    # use commit_consistent_turn below, rather than hiding a persistence error.
    memory.record_turn(sender, message_id, inbound, outbound, observation, {**search_state, "legacy_searches": legacy_searches, "previous": previous or {}}, stage, summary, timings)
    return active_id


def qualification_snapshot(criteria):
    """Expose the five declared filters to the advisor without assuming missing data."""
    groups = {
        "que": ("operation", "property_type", "bedrooms", "area_min_m2", "bathrooms_min", "parking_required", "preferences"),
        "donde": ("districts",),
        "cuanto": ("budget_max", "currency"),
        "como": ("payment_method", "credit_status"),
        "cuando": ("purchase_timeline",),
    }
    return {group: {key: criteria[key] for key in keys if key in criteria and criteria[key] is not None}
            for group, keys in groups.items()}


def _profile_payload(phone, criteria, inbound, outbound, stage):
    status = "pendiente_asesor" if stage == "handoff" else "en_calificacion"
    return {"phone": phone, "intent": criteria.get("operation"), "district": ", ".join(criteria.get("districts", [])) or None,
            "budget": f"{criteria.get('currency')} {criteria.get('budget_max')}" if criteria.get("currency") and criteria.get("budget_max") is not None else None,
            "bedrooms": str(criteria["bedrooms"]) if criteria.get("bedrooms") is not None else None,
            "conversation": {"last_user_message": inbound, "last_assistant_message": outbound, "qualification": qualification_snapshot(criteria)}, "status": status,
            "next_action": "Contactar al lead" if status == "pendiente_asesor" else "Continuar la calificación por WhatsApp"}


def _inventory_trace(criteria, matches, cutoff):
    operation = criteria.get("operation")
    if operation == "venta": state = "skipped_sale"
    elif not inventory_ready(criteria): state = "skipped_incomplete"
    else: state = "executed" if matches else "no_eligible"
    return {"criteria_snapshot": criteria, "eligibility_cutoff_at": (cutoff or datetime.now(timezone.utc)).isoformat(),
            "rules_version": "p1", "result_status": state, "eligible_count": len(matches or [])}


def commit_consistent_turn(memory, claim_token, sender, message_id, inbound, outbound, observation, criteria, stage, summary, previous, search_state, inventory_matches=None, inventory_cutoff=None):
    """Prepare data only; the database RPC commits every durable consequence together."""
    observation = sanitize_new_search_observation(observation, inbound)
    active_id = search_state.get("active_search_id")
    searches = search_state.get("searches", {})
    current = searches.get(active_id, {}) if active_id else {}
    previous_id = active_search_id(previous or {})
    prior_state = (previous or {}).get("state", {}) if isinstance(previous, dict) else {}
    legacy = prior_state.get("legacy_searches", prior_state.get("searches", {})) if isinstance(prior_state, dict) else {}
    session_state = {"active_search_id": active_id} if active_id else {}
    if isinstance(legacy, dict) and legacy:
        session_state["legacy_searches"] = legacy
    # Nombre del cliente: se conserva entre turnos y búsquedas. La pregunta pendiente solo dura un turno.
    name = search_state.get("customer_name") or (prior_state.get("customer_name") if isinstance(prior_state, dict) else None)
    if name:
        session_state["customer_name"] = name
    if search_state.get("pending_question"):
        session_state["pending_question"] = search_state["pending_question"]
    turns = (recent_conversation_turns(previous) + [{"direction": "user", "content": inbound},
                                                    {"direction": "assistant", "content": outbound}])[-4:]
    if not active_id:
        # Sin búsqueda activa (saludo, nombre) el historial va en la sesión; sin él, el bot no sabía que ya había
        # saludado ni que pidió el nombre, y volvía a presentarse en cada mensaje.
        session_state["recent_turns"] = turns
    payload = {"p_message_id": message_id, "p_claim_token": claim_token,
               "p_profile": _profile_payload(sender, criteria, inbound, outbound, stage),
               "p_session": {"phone": sender, "stage": stage, "state": session_state, "summary": summary},
               "p_search": {"search_id": active_id, "phone": sender, "operation": criteria.get("operation"), "state": {**current, "criteria": criteria, "recent_turns": turns}},
               "p_previous_search_id": previous_id if previous_id != active_id else None,
               "p_inbound": {"message_key": f"in:{message_id}", "phone": sender, "content": inbound, "extraction": observation},
               "p_outbound": {"message_key": f"out:{message_id}", "phone": sender, "content": outbound},
               "p_inventory_log": _inventory_trace(criteria, inventory_matches, inventory_cutoff),
               "p_inventory_results": []}
    if inventory_matches:
        # find_matches yields {"property": row, "public": snapshot}; reading a flat
        # property_id here raised KeyError on the first real listing, and KeyError is
        # not caught by the webhook, so the turn died with the claim still pending.
        payload["p_inventory_results"] = [{"property_id": item["property"]["property_id"], "rank_position": index, "presented_to_client": True,
                                            "eligibility_snapshot": {"rules_version": INVENTORY_RULES_VERSION, "eligible": True}, "public_snapshot": item.get("public", {})}
                                           for index, item in enumerate(inventory_matches, 1)]
    # The escalation is recomputed from the same deterministic inputs that chose the
    # reply, so the promise made to the client and the routed request cannot diverge.
    handoff = detected_handoff(previous, observation, inbound, inventory_matches) if stage == "handoff" else None
    if handoff:
        payload["p_handoff"] = _handoff_payload(sender, handoff, active_id, criteria)
    return memory.commit_work(payload)


def customer_visible_reply(message):
    """Keep stable property identifiers in memory, outside customer-facing copy."""
    message = re.sub(r"(?im)^\s*Ref\.\s*[a-z0-9-]+\s*\n?", "", message)
    message = re.sub(r"(?i)(?:la\s+)?referencia\s+C2-[a-z0-9-]+", "la unidad elegida", message)
    message = re.sub(r"(?i)\bC2-[a-z0-9-]+\b", "la unidad elegida", message)
    return message.strip()


def send_whatsapp_message(to, message):
    message = customer_visible_reply(message)
    response = post_graph_message({"messaging_product":"whatsapp","recipient_type":"individual","to":to,"type":"text","text":{"body":message}})
    logger.info("WhatsApp reply accepted by Graph API")
    return response


MAX_REQUESTED_PHOTOS = 5
MAX_PROJECT_PHOTOS = 15
PHOTOS_UNAVAILABLE_PREFIX = "Por ahora no tengo las fotos cargadas en el chat."
MORE_PHOTOS_QUESTION = "¿Quieres que te envíe más fotos?"
YES_MORE_PHOTOS = re.compile(r"^(?:s[ií]|sip|ok|okay|claro|dale|ya|bueno|perfecto|por favor|env[ií]a(?:me|las)?|m[aá]nda(?:me|las)?|m[aá]s)\b", re.IGNORECASE)


def remaining_photos_line(count):
    noun = "foto más" if count == 1 else "fotos más"
    return f"📷 Tengo {count} {noun} de este proyecto. {MORE_PHOTOS_QUESTION}"


def photo_reference_marker(reference):
    """Kept in memory so the next turn knows the unit; customer_visible_reply strips it."""
    return f"\nRef. {reference}"


FICHA_CLOSING = re.compile(r"\n*[^\n]*(?:env[ií]e los planos|coordinemos una visita)[^\n]*\?\s*(?=\n|$)", re.IGNORECASE)


def wants_more_photos(previous, text):
    if MORE_PHOTOS_QUESTION not in last_assistant_message(previous):
        return False
    cleaned = " ".join((text or "").casefold().split())
    return bool(YES_MORE_PHOTOS.match(cleaned) or PHOTO_WORDS.search(cleaned))


def declines_more_photos(previous, text):
    if MORE_PHOTOS_QUESTION not in last_assistant_message(previous):
        return False
    normalized = " ".join((text or "").casefold().split()).strip(".!¡")
    return bool(NEGATIVE_PATTERN.match(normalized)) and not re.search(r"\b(?:pero|otra|otro|m[aá]s opciones|cambi)", normalized)


def pending_photos(store, phone, reference):
    urls = store.photos_for_reference(reference, MAX_PROJECT_PHOTOS)
    already = store.sent_media(phone, urls) if urls else set()
    return urls, [url for url in urls if url not in already]


def final_photos_text(store, reference, stage):
    if stage == "handoff":
        return "Esas son todas las fotos que tengo de este proyecto. El asesor que te contactará puede resolver cualquier detalle."
    return "Esas son todas las fotos que tengo de este proyecto. " + store.closing_question(reference)


def photo_turn(store, phone, previous, text, reply, stage, selection=None):
    """Plan the photos of this turn: {'stored', 'text', 'photos'}.

    'stored' is committed to memory, 'text' is sent first (None = no text message) and the last
    photo carries the follow-up as caption, so the question always lands under the last image.
    - Listing: text only.
    - Unit chosen: ficha (its closing question held back), then the cover photo with
      "Tengo N fotos más… ¿Quieres que te envíe más fotos?" as caption.
    - "Sí" to that offer: the next 5 photos, the last one asking again while photos remain;
      the final batch ends with the ficha's closing question (brochure or visit).
    """
    plan = {"stored": reply, "text": reply, "photos": []}
    selection = selection or selected_option(previous, text)
    if selection and selection.get("reference") and not reply.startswith(PHOTOS_UNAVAILABLE_PREFIX):
        reference = selection["reference"]
        _, pending = pending_photos(store, phone, reference)
        if not pending:
            return plan
        rest = len(pending) - 1
        if not rest:
            plan["photos"] = [{"reference": reference, "url": pending[0], "caption": "Imagen referencial del proyecto"}]
            return plan
        ficha = FICHA_CLOSING.sub("", reply).rstrip()
        offer = remaining_photos_line(rest)
        plan.update(stored=f"{ficha}\n\n{offer}" + photo_reference_marker(reference), text=ficha,
                    photos=[{"reference": reference, "url": pending[0], "caption": offer}])
        return plan
    shown = latest_presented_references(previous)
    if len(shown) == 1 and declines_more_photos(previous, text):
        # "No" to more photos ends the photo flow with the ficha's own closing question.
        closing = ("Perfecto. El asesor que te contactará puede resolver cualquier detalle." if stage == "handoff"
                   else "Perfecto. " + store.closing_question(shown[0]))
        return {"stored": closing + photo_reference_marker(shown[0]), "text": closing, "photos": []}
    if len(shown) == 1 and (reply.startswith(PHOTOS_UNAVAILABLE_PREFIX) or wants_more_photos(previous, text)):
        reference = shown[0]
        urls, pending = pending_photos(store, phone, reference)
        if not urls:
            return plan
        if not pending:
            closing = final_photos_text(store, reference, stage).replace("Esas son todas las fotos", "Ya te envié todas las fotos")
            return {"stored": closing + photo_reference_marker(reference), "text": closing, "photos": []}
        batch, rest = pending[:MAX_REQUESTED_PHOTOS], len(pending) - MAX_REQUESTED_PHOTOS
        follow_up = remaining_photos_line(rest) if rest > 0 else final_photos_text(store, reference, stage)
        photos = [{"reference": reference, "url": url} for url in batch]
        photos[-1]["caption"] = follow_up
        return {"stored": follow_up + photo_reference_marker(reference), "text": None, "photos": photos}
    return plan


def send_property_photos(to, photos, store=None, skip_already_sent=True):
    """Best effort: images go after the text reply and never break the turn."""
    if not photos:
        return 0
    try:
        already = store.sent_media(to, [p["url"] for p in photos]) if (store and skip_already_sent) else set()
    except requests.RequestException:
        already = set()
    sent = 0
    for photo in photos:
        if photo["url"] in already:
            continue
        try:
            image = {"link": photo["url"]}
            if photo.get("caption"):
                image["caption"] = photo["caption"]
            post_graph_message({"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": "image", "image": image})
            sent += 1
            if store:
                store.record_media_delivery(to, photo.get("reference") or "", photo["url"])
        except requests.RequestException:
            logger.warning("Property photo not delivered")
    return sent


def post_graph_message(payload):
    """POST one messages payload to the Graph API with the active credentials; raise on rejection."""
    token, phone_number_id = embedded_signup.active_credentials(WHATSAPP_TOKEN, PHONE_NUMBER_ID)
    if not token or not phone_number_id: raise RuntimeError("WhatsApp credentials are not configured")
    response = requests.post(f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages", headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}, json=payload, timeout=20)
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        try:
            details = response.json().get("error", {})
        except ValueError:
            details = {}
        logger.error("Graph API rejected reply: status=%s code=%s type=%s message=%s", response.status_code, details.get("code"), details.get("type"), details.get("message"))
        raise error
    return response


def notify_advisor_of_handoff(sender, stage, previous, observation, text, criteria, inventory_matches=None, turn_state=None, summary=None):
    """After the client has their reply, tell the advisor (nota CRM ARENZ). Best effort; never raises."""
    if stage != "handoff":
        return 0
    try:
        handoff = detected_handoff(previous, sanitize_new_search_observation(observation, text), text, inventory_matches)
        name = (turn_state or {}).get("customer_name") or customer_name(previous)
        broker = BROKER_SUMMARY in (summary or "") or broker_terms_told(previous)
        return advisor_notify.notify_advisors(post_graph_message, sender, handoff, criteria, text, name, broker)
    except Exception:
        logger.exception("Advisor notice skipped")
        return 0


# ---------------------------------------------------------------- CONVERSACION_IA=on
# The AI classifies how the client answered the bot's last question (respuesta_cliente);
# this layer turns that class into an action. Prices, areas, units and availability never
# come from the AI: they come from the inventory, and AI text that carries them is dropped.

ALTERNATIVES_REPLY = "Entendido. ¿Prefieres ampliar los distritos, revisar el presupuesto o que te contacte un asesor?"
CLOSING_REPLY = ("Gracias por escribir a {brand}. Cuando quieras retomar tu búsqueda, ver más fotos o hablar con un asesor, "
                 "escríbeme por aquí. 😊")
CLOSING_AFTER_HANDOFF_REPLY = "¡Gracias a ti! El asesor de {brand} te escribirá pronto por aquí 🙌"
CHANGE_CRITERIA_REPLY = "Claro. ¿Qué te gustaría cambiar: el distrito, el presupuesto o los dormitorios?"
REPETITION_REPLIES = {
    "unit": "Tienes razón, disculpa la repetición. ¿Quieres ver otras opciones, más fotos o que un asesor te contacte?",
    "listing": "Tienes razón, disculpa. ¿Quieres revisar alguna de estas opciones o cambiar algún criterio de búsqueda?",
    "other": "Tienes razón, disculpa la repetición. ¿En qué más te puedo ayudar con tu búsqueda?",
}
UNIT_FOLLOW_UP = "¿Quieres saber algo más de este departamento o que te contacte un asesor?"
YES_NO_OFFERS = {"mas_fotos", "brochure", "visita", "asesor", "entrega"}
AI_TEXT_FORBIDDEN = re.compile(
    r"(?:S/|US\$|\$|soles|d[oó]lares|\bUSD\b|\bPEN\b)\s*\.?\s*\d|\d[\d.,]*\s*(?:soles|d[oó]lares|lucas|mil\b|k\b|m²|m2\b|mts?\b|metros)"
    r"|disponib|\bC2-|\bref\.|\bunidad(?:es)?\s+(?:n[°º.]?\s*)?\d|\d+\s*(?:dormitorios|dorm\b|ba[ñn]os|cuartos|habitaciones)"
    r"|\bpiso\s+\d|tenemos disponible|encontr[eé]|te mostrar[eé] las opciones", re.IGNORECASE)


def last_bot_message(previous):
    turns = recent_conversation_turns(previous, limit=8, raw=True) if isinstance(previous, dict) else []
    return next((t["content"] for t in reversed(turns) if t["direction"] == "assistant"), "")


def bot_offer(previous):
    """What the bot's last message asked for, so a yes/no can be read against it."""
    last = last_bot_message(previous)
    if last.startswith(("Entendido, no te contactará un asesor", "Gracias por escribir a")) or CLOSING_THANKS_BASE in last:
        return "cerrada"
    if MORE_PHOTOS_QUESTION in last:
        return "mas_fotos"
    if BROCHURE_OFFER in last:
        return "brochure"
    if VISIT_OFFER in last:
        return "visita"
    if "te contacte un asesor" in last:
        return "asesor"
    if "¿Te muestro las de entrega en los próximos años?" in last or "¿Te muestro las de entrega inmediata?" in last:
        return "entrega"
    if "Responde con el número de la opción" in last or REPETITION_REPLIES["listing"] in last:
        return "listado"
    return "otra"


# Lo que convierte un "no" en un pedido: "no, pero mándame fotos", "no gracias, ¿tiene cochera?".
REQUEST_AFTER_NO = re.compile(r"\bpero\b|\?|\b(?:foto|opci[oó]n|otra|otro|mejor|precio|cu[aá]nto|d[oó]nde|cochera|estacionamiento|"
                              r"visita|asesor|llam|ficha|brochure|plano|distrito|presupuesto|dormitorio)|\d", re.IGNORECASE)


def plain_refusal(text):
    """'no', 'nel', 'todavía no', 'no gracias, así está bien': un rechazo sin ningún pedido."""
    normalized = " ".join((text or "").casefold().split()).strip(".!¡ ")
    found = NEGATIVE_PATTERN.match(normalized)
    return bool(found) and not REQUEST_AFTER_NO.search(normalized[found.end():])


def corrected_reply_class(reply_class, text):
    """Corrige dos confusiones que la IA real tuvo en la prueba del 02/11 (scripts/probar_ia_real.py).

    - Ante una oferta, un "no" simple a veces llega con el pedido de la oferta ("todavía no" al asesor ->
      rechaza/asesor, "nop" a más fotos -> rechaza/fotos) y el bot hacía justo lo rechazado.
    - "no, mejor la 2" llegaba como otras_opciones (con opcion 2) y se mostraban más opciones.
    - Una pregunta por un dato de la unidad llegaba como rechaza u otra sin pedido ("no gracias, pero
      ¿tiene cochera?", "¿y cuánto cuesta?") y el bot cerraba o no la respondía.
    """
    if not reply_class:
        return reply_class
    kind, request, fact, option = reply_class["tipo"], reply_class["pedido"], reply_class["dato"], reply_class["opcion"]
    if kind != "cierra" and plain_refusal(text):
        return {**reply_class, "tipo": "rechaza", "pedido": "ninguno", "dato": "ninguno", "opcion": None}
    # "no, mejor la 2" llegaba a veces como otras_opciones con opcion 2: el número que escribió manda.
    if option and request in ("ninguno", "otras_opciones") and re.search(rf"(?<!\d){option}(?!\d)", text or ""):
        return {**reply_class, "tipo": "pide_otra_cosa" if kind in ("rechaza", "otra") else kind, "pedido": "opcion"}
    if kind in ("rechaza", "otra") and fact != "ninguno" and request in ("ninguno", "dato_unidad"):
        return {**reply_class, "tipo": "pide_otra_cosa", "pedido": "dato_unidad"}
    return reply_class


def customer_reply(observation):
    """respuesta_cliente from the observation, cleaned; None when the AI did not classify."""
    data = observation.get("respuesta_cliente") if isinstance(observation, dict) else None
    if not isinstance(data, dict) or data.get("tipo") not in CUSTOMER_REPLY_TYPES:
        return None
    option = data.get("opcion")
    return {"tipo": data["tipo"],
            "pedido": data.get("pedido") if data.get("pedido") in CUSTOMER_REQUESTS else "ninguno",
            "opcion": option if isinstance(option, int) and not isinstance(option, bool) and 0 < option < 100 else None,
            "dato": data.get("dato") if data.get("dato") in UNIT_FACTS_ASKED else "ninguno"}


def ai_reply_text(observation, previous):
    """The AI's own sentence, only if it states no price, area, unit or availability and is not a repeat."""
    reply = observation.get("assistant_reply") if isinstance(observation, dict) else None
    if not isinstance(reply, str):
        return None
    reply = reply.strip()
    if not reply or len(reply) > 300 or reply.count("?") > 1 or AI_TEXT_FORBIDDEN.search(reply):
        return None
    last = customer_visible_reply(last_bot_message(previous)).casefold()
    if last and difflib.SequenceMatcher(None, last, reply.casefold()).ratio() > 0.8:
        return None
    return reply


def unit_display_name(item):
    found = re.match(r"^Proyecto (.*?); unidad (.*?)\.", item["public"].get("public_description") or "")
    return f"{found.group(1)} (unidad {found.group(2)})" if found else "Esta unidad"


def unit_fact_reply(item, fact):
    """A unit fact written from the inventory record, never by the AI; None if the record lacks it."""
    public = item["public"]
    name = unit_display_name(item)
    description = public.get("public_description") or ""
    if fact == "estacionamiento":
        return parking_question_reply(item)
    if fact == "precio":
        symbol = {"PEN": "S/", "USD": "US$"}.get(public["currency"], public["currency"])
        sentence = f"{name} tiene un precio de {symbol} {float(public['price_amount']):,.0f}."
    elif fact == "area":
        area = public.get("area_total_m2") or public.get("area_m2")
        sentence = f"{name} tiene {float(area):g} m²." if area else None
    elif fact in ("dormitorios", "banos"):
        parts = []
        if public.get("bedrooms") is not None:
            parts.append(f"{float(public['bedrooms']):g} dormitorios")
        if public.get("bathrooms") is not None:
            parts.append(f"{float(public['bathrooms']):g} baños")
        sentence = f"{name} tiene {' y '.join(parts)}." if parts else None
    elif fact == "piso":
        floor = re.search(r"\bPiso ([^.]+)\.", description)
        sentence = f"{name} está en el piso {floor.group(1)}." if floor else None
    elif fact == "entrega":
        when = re.search(r"Entrega según fuente: ([^.]+)\.", description)
        sentence = (f"{name} es de entrega inmediata." if item_delivery(item) == "inmediata" else
                    f"{name} tiene entrega prevista para {when.group(1)}." if when else
                    f"La fecha de entrega de {name} está por confirmar.")
    else:
        return None
    return f"{sentence} {UNIT_FOLLOW_UP}" if sentence else None


def option_reference(previous, number):
    """Reference shown as option `number` in any recent listing (the ficha in between does not hide it)."""
    for turn in reversed(recent_conversation_turns(previous, limit=8, raw=True)):
        if turn["direction"] == "assistant":
            pairs = dict(re.findall(r"(?m)^(\d{1,2})\.\s[^\n]*\n[^\n]*?\bRef\.\s*([A-Za-z0-9-]+)", turn["content"]))
            if str(number) in pairs:
                return pairs[str(number)]
    return None


AMOUNT_IN_TEXT = re.compile(r"\d|\b(?:mil|mill[oó]n|millones|lucas|k)\b", re.IGNORECASE)


def without_invented_budget(observation, text, previous=None):
    """Sin una cifra en el mensaje no hay presupuesto nuevo: ante 'ta caro' la IA real puso a veces el precio de la
    unidad (S/ 289,460) como presupuesto máximo, y el bot volvía a mostrar esa misma unidad. Se conserva el presupuesto
    que ya se conocía (la IA lo repite en slot_updates y una búsqueda nueva lo necesita)."""
    if not isinstance(observation, dict) or AMOUNT_IN_TEXT.search(text or ""):
        return observation
    known = (conversation_state(previous) or {}).get("budget_max") if isinstance(previous, dict) else None
    actions = [a for a in observation.get("criteria_actions") or [] if not (isinstance(a, dict) and a.get("field") == "budget_max")]
    slots = observation.get("slot_updates")
    if isinstance(slots, dict) and slots.get("budget_max") not in (None, known):
        slots = {**slots, "budget_max": known}
    return {**observation, "criteria_actions": actions, "slot_updates": slots}


def changes_criteria(criteria, actions):
    """True solo si las acciones cambian algo: la IA a veces repite un criterio conocido ('UPDATE property_type
    departamento' en 'no, mejor la 2') y eso mandaba el turno a las reglas, que leían el 'no' como 'no más fotos'."""
    if not actions:
        return False
    try:
        updated = apply_criteria_actions(copy.deepcopy(criteria), actions)
    except (TypeError, ValueError, KeyError, AttributeError):
        return True
    fields = {action.get("field") for action in actions if isinstance(action, dict)}
    return any(updated.get(field) != criteria.get(field) for field in fields) or not fields


def conversational_turn(sender, previous, text, observation, inventory_store):
    """Act on the client's answer to the bot's last question.

    Returns (result, text): a complete turn, or (None, text) to run the rules, where text may be
    rewritten to the canonical form the rules already understand ('Sí', 'más opciones'...).
    """
    reply_class = customer_reply(observation)
    greeting = re.sub(r"[^\w\s]", "", (text or "").casefold()).strip() in {"hola", "buenos días", "buenos dias", "buenas tardes", "buenas noches"}
    if not reply_class or greeting or customer_pause_reply(text) or is_new_search_request(text):
        return None, text
    reply_class = corrected_reply_class(reply_class, text)
    kind, request, offer = reply_class["tipo"], reply_class["pedido"], bot_offer(previous)
    stage = (previous or {}).get("stage") or "conversation"
    shown = latest_presented_references(previous)
    unit = shown[0] if len(shown) == 1 and stage in ("property_detail", "handoff") else None
    neutral = {**observation, "intent": "confirmation", "slot_updates": {}, "criteria_actions": [], "criteria_change": False, "handoff": False}
    turn_state, criteria = search_state_for_turn(previous, neutral, text)
    criteria = dict(criteria)
    slots = validated_slot_updates(observation.get("slot_updates"))
    # New search data ("ya te dije, en Surco") goes through the rules, which store it. Only explicit actions or
    # core fields count: the model tends to echo known criteria and loose preferences in every turn.
    changes_search = changes_criteria(criteria, observation.get("criteria_actions")) or any(
        slots.get(key) not in (None, [], "") and slots.get(key) != criteria.get(key)
        for key in ("districts", "budget_max", "currency", "bedrooms"))

    def matches():
        if inventory_store and inventory_ready(criteria):
            return inventory_store.find_matches(with_sale_defaults(criteria))
        return None, None

    def done(reply, new_stage, summary, reference=None, plan=None, found=(None, None)):
        stored = reply + (photo_reference_marker(reference) if reference and reference not in reply else "")
        if found == (None, None):
            found = matches()
        return ({"observation": observation, "search_state": turn_state, "criteria": criteria, "stage": new_stage,
                 "summary": summary, "plan": plan or {"stored": stored, "text": reply, "photos": []},
                 "inventory_matches": found[0], "inventory_cutoff": found[1]}, text)

    if kind == "cierra":
        if offer == "cerrada":
            return done(CLOSING_THANKS_REPLY, stage, "Cliente agradece tras el cierre.")
        if stage == "handoff":
            return done(CLOSING_AFTER_HANDOFF_REPLY.format(brand=COMMERCIAL_BRAND), "handoff", "Cliente se despide; derivación ya registrada.", unit)
        return done(CLOSING_REPLY.format(brand=COMMERCIAL_BRAND), "awaiting_customer", "Cliente cierra la conversación; sin seguimiento automático.", unit)

    # Pedir más opciones, fotos o elegir una opción no es cambiar la búsqueda, aunque la IA añada algo como la
    # preferencia 'opcion_2' o marque una búsqueda nueva ("no, pero muéstrame otra" llegó a reiniciar la búsqueda).
    if changes_search and kind in ("queja_repeticion", "rechaza", "pide_otra_cosa") and request not in ("opcion", "otras_opciones", "fotos"):
        return None, text

    if kind == "queja_repeticion":
        context = "unit" if unit else "listing" if offer == "listado" or (shown and stage == "qualified") else "other"
        reply = ai_reply_text(observation, previous) or REPETITION_REPLIES[context]
        return done(reply, stage, "Cliente señala que el bot repite; no se repite la información.", unit)

    if kind == "rechaza" and request == "ninguno":
        if offer in ("brochure", "visita", "asesor"):
            return done(OFFER_DECLINED_REPLY.format(brand=COMMERCIAL_BRAND), "awaiting_customer",
                        "Cliente no desea asesor por ahora; conversación cerrada sin seguimiento automático.")
        if offer == "mas_fotos" and unit:
            if stage == "handoff":
                closing = "Perfecto. El asesor que te contactará puede resolver cualquier detalle."
            else:
                try:
                    closing = "Perfecto. " + (inventory_store.closing_question(unit) if inventory_store else ADVISOR_OFFER)
                except requests.RequestException:
                    closing = "Perfecto. " + ADVISOR_OFFER
            return done(closing, stage, "Cliente no quiere más fotos; se pasa a la pregunta de cierre.", unit)
        if offer in ("entrega", "listado"):
            return done(ALTERNATIVES_REPLY, "qualified", "Cliente rechaza las opciones ofrecidas; se proponen alternativas.")
        if offer == "cerrada":
            return done(CLOSING_THANKS_REPLY, stage, "Conversación ya cerrada.")
        return None, text

    if kind == "acepta":
        return None, ("Sí" if offer in YES_NO_OFFERS else text)

    if kind in ("pide_otra_cosa", "rechaza"):
        if request == "otras_opciones":
            return None, "Muéstrame más opciones"
        if request == "fotos":
            return None, "Envíame más fotos"
        if request == "asesor":
            return None, ("Sí" if offer in ("brochure", "visita", "asesor") else "Quiero hablar con un asesor")
        if request == "opcion" and reply_class["opcion"]:
            reference = option_reference(previous, reply_class["opcion"])
            found = matches()
            item = next((m for m in found[0] or [] if m["public"]["public_reference"] == reference), None)
            if not item:
                return None, text
            reply = full_unit_reply(item)
            selection = {"index": reply_class["opcion"], "reference": reference, "count": len(shown)}
            try:
                plan = photo_turn(inventory_store, sender, previous, text, reply, "property_detail", selection)
            except requests.RequestException:
                plan = None
            return done(reply, "property_detail", f"Detalle de la referencia elegida {reference}.", plan=plan, found=found)
        if request == "dato_unidad" and unit:
            found = matches()
            item = next((m for m in found[0] or [] if m["public"]["public_reference"] == unit), None)
            reply = unit_fact_reply(item, reply_class["dato"]) if item else None
            if reply:
                return done(reply, stage, f"Dato de la unidad {unit} desde el inventario.", unit, found=found)
            return None, text
        if request == "cambiar_criterios":
            reply = ai_reply_text(observation, previous) or CHANGE_CRITERIA_REPLY
            return done(reply, stage, "Cliente quiere cambiar la búsqueda; se pregunta qué cambiar.", unit)
    return None, text


# Agentes o corredores que buscan para un cliente: colaboración comercial, no compradores directos.
AGENT_COMMISSION_SHARE = os.getenv("AGENT_COMMISSION_SHARE", "50 % para cada parte").strip()
BROKER_PATTERN = re.compile(
    r"\b(?:soy|somos)\s+(?:una?\s+)?(?:agentes?|corredor(?:a|es)?|brokers?|asesor(?:a|es)?\s+inmobiliari[oa]s?)\b"
    r"|\bpara\s+(?:una?|mis?)\s+clientes?\b|\bmis?\s+clientes?\s+(?:busca|está|esta|quiere|necesita)"
    r"|\bcompart(?:en|es|imos|ir)\s+(?:la\s+)?comisi[oó]n", re.IGNORECASE)
BROKER_SUMMARY = "Contacto de colaboración: agente inmobiliario que busca para un cliente."


def broker_terms_told(previous):
    """The bot already told this contact the collaboration terms (so it is an agent)."""
    return isinstance(previous, dict) and any(
        turn["direction"] == "assistant" and "colaboración con agentes" in turn["content"]
        for turn in recent_conversation_turns(previous, limit=8, raw=True))


def is_broker_message(text):
    return bool(BROKER_PATTERN.search(text or ""))


def broker_reply(rules_reply, fallback=None):
    commission = f", compartiendo la comisión al {AGENT_COMMISSION_SHARE}" if AGENT_COMMISSION_SHARE else ""
    opening = (f"¡Mucho gusto! 😊 Soy {ASSISTANT_NAME}, de {COMMERCIAL_BRAND}. Sí trabajamos en colaboración con agentes "
               f"inmobiliarios{commission}.")
    # Ante la presentación genérica se pregunta qué busca su cliente; un listado o una pregunta concreta se conservan.
    if not rules_reply or rules_reply in (INTRODUCTION, fallback):
        return f"{opening} ¿Qué departamento o en qué distrito busca tu cliente? 🏡"
    return f"{opening}\n\n{rules_reply}"


NEW_SEARCH_PREFIX = "Nueva búsqueda iniciada. "
DISTRICT_QUESTION = "¿En qué distrito o distritos te gustaría tu departamento?"
NAME_QUESTION = "¿Me podrías indicar tu nombre?"
FREE_QUERY_REPLY = "Claro. Puedo orientarte sobre tu búsqueda inmobiliaria. ¿Qué deseas consultar?"
# Preguntas de calificación que pueden esperar a que el cliente diga su nombre.
QUALIFYING_REPLIES = (DISTRICT_QUESTION, "¿Cuál es tu presupuesto máximo aproximado? Puedes indicarlo en soles o en dólares.",
                      "¿Ese monto es en soles o en dólares?", "¿Cuántos dormitorios necesitas?")
NAME_WORD = r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{2,20}"
NAME_INTRO_PATTERN = re.compile(rf"\b(?P<intro>me llamo|mi nombre es|soy)\s+(?P<name>{NAME_WORD}(?:\s+{NAME_WORD})?)", re.IGNORECASE)
NOT_A_NAME = {
    "de", "del", "la", "el", "los", "las", "un", "una", "y", "e", "o", "en", "con", "para", "por", "que", "yo", "mi", "su",
    "agente", "agentes", "corredor", "corredora", "broker", "asesor", "asesora", "inmobiliaria", "inmobiliario",
    "cliente", "interesado", "interesada", "nuevo", "nueva", "independiente", "propietario", "propietaria",
    "no", "si", "sí", "hola", "ok", "okay", "gracias", "busco", "buscamos", "quiero", "queremos", "ya", "claro",
    "prefiero", "depa", "departamento", "información", "informacion", "buenas", "buenos", "tardes", "días", "dias",
    "noches", "perfecto", "listo", "bien", "dale", "vale", "nada", "todavía", "todavia", "luego", "después", "despues",
    "lima", "peruano", "peruana", "contado", "crédito", "credito", "más", "mas", "fotos", "opciones", "otra", "otro",
    "soy", "me", "llamo", "es", "tengo", "estoy", "necesito", "busca", "quisiera", "deseo", "aquí", "aqui", "chau",
}


def customer_name(previous):
    state = (previous or {}).get("state") if isinstance(previous, dict) else None
    name = state.get("customer_name") if isinstance(state, dict) else None
    return name if isinstance(name, str) and name.strip() else None


def _clean_name(words):
    words = [w for w in words if w]
    if not words or words[0].casefold() in NOT_A_NAME:
        return None
    if len(words) > 1 and words[1].casefold() in NOT_A_NAME:
        words = words[:1]
    return " ".join(w[:1].upper() + w[1:].lower() for w in words)


def name_from_text(text, asked=False):
    """Nombre dicho por el cliente ('me llamo Ana', 'soy Ana') o, si el bot lo pidió, una respuesta corta ('Ana Pérez')."""
    text = (text or "").strip()
    for found in NAME_INTRO_PATTERN.finditer(text):
        if found.group("intro").casefold() != "soy" or asked or found.start() < 40:
            name = _clean_name(found.group("name").split())
            if name:
                return name
    if asked:
        bare = re.sub(r"^(?:hola|buenas|buenos días|buenas tardes|buenas noches)[\s,!.]*", "", text, flags=re.IGNORECASE)
        bare = bare.strip(" .!¡,😊🙂👋")
        if re.fullmatch(rf"{NAME_WORD}(?:\s+{NAME_WORD}){{0,2}}", bare):
            return _clean_name(bare.split()[:2])
    return None


def asked_for_name(previous):
    turns = recent_conversation_turns(previous, limit=8, raw=True) if isinstance(previous, dict) else []
    last = next((t["content"] for t in reversed(turns) if t["direction"] == "assistant"), "")
    return NAME_QUESTION in last


def pending_question(previous):
    state = (previous or {}).get("state") if isinstance(previous, dict) else None
    question = state.get("pending_question") if isinstance(state, dict) else None
    return question if isinstance(question, str) and question else None


def is_first_contact(previous):
    return not (isinstance(previous, dict) and any(
        turn["direction"] == "assistant" for turn in recent_conversation_turns(previous, limit=8, raw=True)))


def first_contact_reply(reply, name=None):
    """Primer mensaje al lead: se presenta como en WhatsApp, sin 'Nueva búsqueda iniciada'."""
    hello = f"¡Hola, {name}! 😊" if name else "¡Hola! 😊"
    if not reply:
        return reply
    if reply == INTRODUCTION:
        reply = "Te ayudo a encontrar tu departamento ideal 🏡 ¿En qué distrito o distritos lo buscas?"
    elif reply.startswith(("Hola", "¡Hola")):
        return reply
    reply = reply[len(NEW_SEARCH_PREFIX):] if reply.startswith(NEW_SEARCH_PREFIX) else reply
    return f"{hello} Soy {ASSISTANT_NAME}, de {COMMERCIAL_BRAND}. {reply}"


def _with_reply(result, reply, **state):
    """Same turn with another text (no photos) and extra session data (customer_name, pending_question)."""
    turn_state = {**(result.get("search_state") or {}), **state}
    return {**result, "plan": {"stored": reply, "text": reply, "photos": []}, "search_state": turn_state}


NOT_UNDERSTOOD = "Perdona, no te entendí bien 😅"


def repeated_question_reply(previous, reply):
    """Si el bot vuelve a hacer la misma pregunta, es que no entendió: lo dice en vez de repetirla como si nada."""
    if not reply or "Responde con el número" in reply or NOT_UNDERSTOOD in reply:
        return None
    asked = last_bot_question(previous)
    # Solo las preguntas para conocer al cliente (distrito, presupuesto, moneda, dormitorios, pago, plazo). Ni las de
    # seguimiento tras un dato ni "¿Prefieres ampliar los distritos...?": ahí el cliente suele preguntar otra cosa
    # ("¿tienes estacionamiento?", prueba del 03/11) y "no te entendí" no corresponde.
    qualifying = {q.split("?")[0].casefold() for q in re.findall(r"¿[^¿?]*\?", " ".join(QUALIFYING_REPLIES + (PAYMENT_QUESTION, TIMELINE_QUESTION)))}
    if not asked.startswith("¿") or asked.split("?")[0].casefold() not in qualifying:
        return None
    questions = re.findall(r"¿[^¿?]*\?", customer_visible_reply(reply))
    if not questions or questions[-1].strip().casefold() != asked.casefold():
        return None
    question = reply[reply.rindex(questions[-1]):]
    return f"{NOT_UNDERSTOOD} ¿{question[1:2].upper()}{question[2:]}"


def plan_turn(sender, previous, text, observation, fallback, inventory_store):
    """Decide the turn, then apply the ARENZ conversation style.

    First contact: greet as Cecilia and ask the client's name before the search questions.
    Name answer: thank by name and ask the pending question. Agents writing for a client
    get the collaboration terms once.
    """
    known_name = customer_name(previous)
    if not known_name and asked_for_name(previous):
        name = name_from_text(text, asked=True)
        if name:
            neutral = {**(observation or {}), "intent": "confirmation", "slot_updates": {}, "criteria_actions": [],
                       "criteria_change": False, "handoff": False}
            result = rules_turn(sender, previous, text, neutral, fallback, inventory_store)
            question = pending_question(previous) or DISTRICT_QUESTION
            stage = (previous or {}).get("stage") or result["stage"]
            result = _with_reply(result, f"Mucho gusto, {name} 😊 {question}", customer_name=name)
            return {**result, "stage": stage, "summary": f"Cliente se identifica como {name}."}
    result = _plan_turn(sender, previous, text, observation, fallback, inventory_store)
    name = known_name or name_from_text(text, asked=is_first_contact(previous))
    if name and not known_name:
        result = {**result, "search_state": {**(result.get("search_state") or {}), "customer_name": name}}
    if is_broker_message(text) and not broker_terms_told(previous):
        generic = (INTRODUCTION, fallback, FREE_QUERY_REPLY, NEW_SEARCH_PREFIX + DISTRICT_QUESTION) + QUALIFYING_REPLIES
        plan = dict(result["plan"])
        for key in ("text", "stored"):
            plan[key] = broker_reply(None if plan.get(key) in generic else plan.get(key), fallback)
        result = {**result, "plan": plan, "summary": f"{BROKER_SUMMARY} {result.get('summary') or ''}".strip()}
    elif is_first_contact(previous):
        reply = result["plan"].get("text")
        waiting = reply[len(NEW_SEARCH_PREFIX):] if reply and reply.startswith(NEW_SEARCH_PREFIX) else reply
        generic = waiting in (INTRODUCTION, fallback, FREE_QUERY_REPLY) + QUALIFYING_REPLIES and not result["plan"].get("photos")
        if generic and not name:
            question = DISTRICT_QUESTION if waiting in (INTRODUCTION, fallback, FREE_QUERY_REPLY) else waiting
            greeting = (f"¡Hola! 😊 Soy {ASSISTANT_NAME}, de {COMMERCIAL_BRAND}. Con gusto te ayudo a encontrar tu "
                        f"departamento 🏡 {NAME_QUESTION}")
            result = _with_reply(result, greeting, pending_question=question)
        else:
            plan = dict(result["plan"])
            for key in ("text", "stored"):
                plan[key] = first_contact_reply(plan.get(key), name)
            result = {**result, "plan": plan}
    elif not result["plan"].get("photos"):
        clarified = repeated_question_reply(previous, result["plan"].get("text"))
        if clarified:
            result = _with_reply(result, clarified)
    return with_unit_reference(previous, result)


def with_unit_reference(previous, result):
    """Las respuestas sobre la unidad que el cliente ve llevan su referencia guardada.

    Solo se recuerdan los 4 últimos turnos: tras dos preguntas (cochera, precio) la ficha salía de la memoria, el bot
    olvidaba la unidad y a "¿cuál es su ubicación?" volvía a listar opciones (prueba del 03/11).
    """
    stored = result["plan"].get("stored") or ""
    if result.get("stage") not in ("property_detail", "handoff") or PRESENTED_REFERENCE_PATTERN.search(stored):
        return result
    shown = latest_presented_references(previous)
    if len(shown) != 1 or (previous or {}).get("stage") not in ("property_detail", "handoff"):
        return result
    plan = {**result["plan"], "stored": stored + photo_reference_marker(shown[0])}
    return {**result, "plan": plan}


def _plan_turn(sender, previous, text, observation, fallback, inventory_store):
    """Decide one turn (reply, criteria, stage and photos) from the AI observation.

    Reads inventory and may ask OpenAI for the ficha or a unit answer; persistence and
    WhatsApp delivery stay in the webhook. With CONVERSACION_IA=on the client's answer to
    the bot's last question is acted on first (conversational_turn).
    """
    if conversacion_ia_activa():
        observation = without_invented_budget(observation, text, previous)
        result, text = conversational_turn(sender, previous, text, observation, inventory_store)
        if result:
            return result
    return rules_turn(sender, previous, text, observation, fallback, inventory_store)


def rules_turn(sender, previous, text, observation, fallback, inventory_store):
    """The deterministic flow (the whole behavior while CONVERSACION_IA is off)."""
    # One effective observation governs reply, search selection, and persistence.
    # This prevents an explicit operation from updating the prior search only in
    # the response layer while persistence still sees the unnormalized input.
    effective_observation = with_explicit_operation(observation, text)
    if is_more_request(text) and isinstance(effective_observation, dict) and effective_observation.get("intent") == "new_search":
        # "Muéstrame otro departamento" continues the same search; the model reads it as a new one.
        effective_observation = {**effective_observation, "intent": "property_search", "criteria_actions": []}
    if selected_option(previous, text) or is_selected_parking_question(previous, text):
        effective_observation = {**(effective_observation or {}), "intent": "confirmation",
                                 "slot_updates": {}, "criteria_actions": [], "criteria_change": False, "handoff": False}
    turn_state, turn_prior_criteria = search_state_for_turn(previous, effective_observation, text)
    reply, criteria, stage, summary = progressive_reply(previous, effective_observation, fallback, text, turn_prior_criteria)
    inventory_matches, inventory_cutoff = None, None
    if inventory_store and inventory_ready(criteria):
        inventory_matches, inventory_cutoff = inventory_store.find_matches(with_sale_defaults(criteria))
        reply, criteria, stage, summary = progressive_reply(previous, effective_observation, fallback, text, turn_prior_criteria, inventory_matches)
    plan = {"stored": reply, "text": reply, "photos": []}
    if inventory_store:
        try:
            plan = photo_turn(inventory_store, sender, previous, text, reply, stage)
        except requests.RequestException:
            plan = {"stored": reply, "text": reply, "photos": []}
    return {"observation": effective_observation, "search_state": turn_state, "criteria": criteria, "stage": stage,
            "summary": summary, "plan": plan, "inventory_matches": inventory_matches, "inventory_cutoff": inventory_cutoff}


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
    webhook_started_at = time.monotonic()
    raw = request.get_data(cache=True)
    if not valid_meta_signature(raw, request.headers.get("X-Hub-Signature-256", "")):
        logger.warning("Rejected webhook with invalid signature"); return jsonify({"status":"invalid_signature"}), 401
    messages = iter_incoming_messages(request.get_json(silent=True))
    if not messages: return jsonify({"status":"ignored"}), 200
    try:
        processed = 0
        for message in messages:
            timings = {}
            started_at = time.monotonic()
            memory = get_conversation_memory_store()
            message_id = message.get("id")
            claim_token = None
            if memory:
                outcome, claim_token = memory.claim_work(message_id)
                if outcome not in ("claimed", "reclaimed"):
                    continue
            elif not get_lead_store().claim_message(message_id):
                continue
            timings["dedupe_ms"] = round((time.monotonic() - started_at) * 1000)
            sender, text = message["from"], message["text"]["body"]
            raw_text = text
            fallback = generate_reply(sender, text)
            started_at = time.monotonic()
            try:
                previous = memory.load_session(sender) if memory else None
            except requests.RequestException:
                previous = None
                logger.warning("Conversation memory unavailable; using in-process fallback")
            timings["session_read_ms"] = round((time.monotonic() - started_at) * 1000)
            # Clients type fast, with typos and slang; every rule below reads the cleaned message.
            text = resume_answer_as_command(previous, normalize_client_text(text))
            observation = observe_conversation(sender, text, fallback, timings, previous, True, raw_text=raw_text)
            inventory_store = get_inventory_store()
            turn = plan_turn(sender, previous, text, observation, fallback, inventory_store)
            effective_observation, turn_state, criteria, stage, summary, plan = (
                turn["observation"], turn["search_state"], turn["criteria"], turn["stage"], turn["summary"], turn["plan"])
            inventory_matches, inventory_cutoff = turn["inventory_matches"], turn["inventory_cutoff"]
            reply = plan["stored"]
            started_at = time.monotonic()
            if memory:
                # All mandatory internal state commits before attempting Meta.
                commit_consistent_turn(memory, claim_token, sender, message_id, text, reply, effective_observation, criteria, stage, summary, previous, turn_state, inventory_matches, inventory_cutoff)
            else:
                persist_conversation_turn(sender, message_id, text, reply, effective_observation, criteria, stage, summary, previous, timings, turn_state)
                profile_session = {**user_sessions.get(sender, {})}
                if criteria:
                    profile_session.update({"intent": criteria.get("operation"), "district": ", ".join(criteria.get("districts", [])) or None, "budget": f"{criteria.get('currency')} {criteria.get('budget_max')}" if criteria.get("currency") and criteria.get("budget_max") else None, "bedrooms": str(criteria["bedrooms"]) if criteria.get("bedrooms") is not None else None})
                get_lead_store().upsert_lead(sender, profile_session, text, reply)
            timings["internal_commit_ms"] = round((time.monotonic() - started_at) * 1000)
            started_at = time.monotonic()
            try:
                if plan["text"]:
                    send_whatsapp_message(sender, plan["text"])
                if plan["photos"]:
                    sent = send_property_photos(sender, plan["photos"], inventory_store, skip_already_sent=False)
                    # The follow-up rides on the last photo; if that photo did not go out, send it as text.
                    if sent < len(plan["photos"]) and plan["photos"][-1].get("caption") and not plan["text"]:
                        send_whatsapp_message(sender, plan["photos"][-1]["caption"])
                if memory: memory.record_delivery(message_id, "sent")
            except requests.RequestException as error:
                # Internal commit remains processed. Never retry Meta automatically.
                if memory:
                    code = f"meta_http_{getattr(error.response, 'status_code', None)}" if getattr(error, "response", None) is not None else "meta_transport"
                    memory.record_delivery(message_id, "failed", code)
                logger.warning("WhatsApp delivery failed after internal commit: category=%s", type(error).__name__)
            # The handoff is already committed; the notice to the advisor comes last so it
            # can never delay or break the client's reply.
            notify_advisor_of_handoff(sender, stage, previous, effective_observation, text, criteria, inventory_matches, turn_state, summary)
            timings["graph_ms"] = round((time.monotonic() - started_at) * 1000)
            timings["total_ms"] = round((time.monotonic() - webhook_started_at) * 1000)
            logger.info("Webhook timing: dedupe_ms=%s session_read_ms=%s context_ms=%s openai_ms=%s session_write_ms=%s messages_write_ms=%s lead_write_ms=%s graph_ms=%s total_ms=%s", timings.get("dedupe_ms"), timings.get("session_read_ms"), timings.get("context_ms"), timings.get("openai_ms"), timings.get("session_write_ms"), timings.get("messages_write_ms"), timings.get("lead_write_ms"), timings.get("graph_ms"), timings.get("total_ms"))
            processed += 1
        return jsonify({"status":"received" if processed else "duplicate", "processed":processed}), 200
    except (requests.RequestException, RuntimeError, sqlite3.Error):
        if 'memory' in locals() and memory and claim_token:
            memory.fail_work(message.get("id"), claim_token, "internal_commit")
        logger.exception("Webhook processing failed"); return jsonify({"status":"error"}), 500


if __name__ == "__main__": app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))


