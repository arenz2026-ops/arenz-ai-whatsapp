import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, request

import advisor_notify
import embedded_signup

app = Flask(__name__)
app.register_blueprint(embedded_signup.bp)
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
ASSISTANT_NAME = os.getenv("ASSISTANT_NAME", "Cecilia")
COMMERCIAL_BRAND = os.getenv("COMMERCIAL_BRAND", "C2 Inmobiliaria")
INTRODUCTION = f"Hola, soy {ASSISTANT_NAME}, asesora de {COMMERCIAL_BRAND}. Te ayudo a orientar tu búsqueda inmobiliaria. ¿Qué inmueble estás buscando?"
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
    "Para una visita o un asesor, registra una solicitud sin prometer una fecha confirmada."
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
        session["step"] = "intent"; user_sessions[sender] = session
        return INTRODUCTION if any(w in text for w in ("hola", "buenos días", "buenas tardes", "buenas noches")) else f"Hola, soy {ASSISTANT_NAME}, asesora de {COMMERCIAL_BRAND}. ¿Deseas comprar, alquilar o vender?"
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
        session.update(budget=text, step="bedrooms"); user_sessions[sender] = session; return "¿Cuántos dormitorios necesitas?"
    if session["step"] == "bedrooms":
        session.update(bedrooms=text, step="summary"); user_sessions[sender] = session
        return f"Tengo estos datos:\nOperación: {session['intent']}\nZona: {session['district']}\nPresupuesto: {session['budget']}\nDormitorios: {session['bedrooms']}\n\n¿La información es correcta? Responde Sí o No."
    if session["step"] == "summary":
        if text in ("sí","si"):
            session["step"]="done"; user_sessions[sender]=session; return f"Excelente ✅. Un asesor de {COMMERCIAL_BRAND} podrá continuar contigo."
        if "no" in text:
            session["step"]="intent"; user_sessions[sender]=session; return "De acuerdo. ¿Deseas comprar, alquilar o vender una propiedad?"
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


def recent_conversation_turns(previous, limit=4):
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
            clean.append({"direction": direction, "content": bounded_turn_content(content, direction)})
    if not clean and isinstance(state, dict):
        for direction, key in (("user", "last_user_message"), ("assistant", "last_assistant_message")):
            content = state.get(key)
            if isinstance(content, str) and content.strip():
                clean.append({"direction": direction, "content": bounded_turn_content(content, direction)})
    return clean[-limit:]


def build_observation_payload(previous, text):
    """Build a bounded request from durable criteria and recent dialogue."""
    context = {"stage": previous.get("stage") if isinstance(previous, dict) else None, "active_search_id": active_search_id(previous), "criteria": conversation_state(previous), "recent_turns": recent_conversation_turns(previous)}
    payload = {
        "model": OPENAI_MODEL, "store": False, "max_output_tokens": 1200,
        "instructions": "Analiza una conversación inmobiliaria en Lima. Extrae solo datos explícitos o altamente confiables. No inventes inmuebles, precios ni disponibilidad. Devuelve únicamente el JSON del esquema. NUEVA BÚSQUEDA es intent new_search. Para cambios usa criteria_actions: ADD agrega, UPDATE reemplaza y REMOVE elimina; 'ya no es necesario' es REMOVE, nunca una preferencia negativa. Estacionamiento/cochera se expresa únicamente con parking_required: 'con estacionamiento', 'con cochera', 'necesito estacionamiento' y 'necesito cochera' son true; 'sin estacionamiento', 'sin cochera', 'no necesito estacionamiento' y 'no necesito cochera' son false. Para cualquier valor explícito usa UPDATE parking_required con una lista unitaria; false significa que no es requisito y no una preferencia negativa. property_type debe usar solo valores canónicos: departamento es el valor para departamento y apartamento; nunca devuelvas apartamento. Un cambio explícito entre compra, alquiler y venta inicia un contexto independiente. assistant_reply debe ser natural, útil y máximo 180 caracteres, exclusivamente sobre búsqueda inmobiliaria, criterios, aclaraciones, compra, alquiler, venta o derivación a un asesor. No introduzcas humor, temas generales, recomendaciones no inmobiliarias ni contenido accesorio; nunca afirmes disponibilidad, recomendación ni características de una propiedad concreta. No preguntes un criterio ya presente en Contexto salvo ambigüedad o conflicto. user_question máximo 120 caracteres; máximo tres distritos y tres preferencias.",
        "text": {"format": {"type": "json_schema", "name": "arenz_conversation_observation", "strict": True, "schema": OBSERVATION_SCHEMA}},
        "input": f"Contexto: {json.dumps(context, ensure_ascii=False)}\nMensaje: {text}",
    }
    payload["instructions"] += COMMERCIAL_POLICY
    return payload


def observe_conversation(sender, text, deterministic_reply, timings=None, previous=None, previous_loaded=False):
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
    payload = build_observation_payload(previous, text)
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
        logger.info("Conversation observation telemetry: model=%s usage_available=%s output_tokens=%s reasoning_tokens=%s total_tokens=%s latency_ms=%s", telemetry["model"], telemetry["usage_available"], telemetry["output_tokens"], telemetry["reasoning_tokens"], telemetry["total_tokens"], telemetry["latency_ms"])
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
PROPERTY_TYPE_ALIASES = {"apartamento": "departamento"}
INVENTORY_RULES_VERSION = "p1-v1"
INVENTORY_MAX_RESULTS = 3
INVENTORY_VERIFICATION_DAYS = 7
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
            slots["budget_max"] = amount
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
        districts = [value.strip().title() for value in districts if isinstance(value, str) and 1 <= len(value.strip()) <= 60][:3]
        if districts:
            clean["districts"] = districts
    budget = slot_updates.get("budget_max")
    if isinstance(budget, (int, float)) and 1000 <= budget <= 10000000:
        clean["budget_max"] = int(budget)
    currency = slot_updates.get("currency")
    if isinstance(currency, str) and currency.upper() in ALLOWED_CURRENCIES:
        clean["currency"] = currency.upper()
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
    keys = ("operation", "districts", "budget_max", "currency", "bedrooms", "property_type", "parking_required", "preferences", *COMMERCIAL_FIELDS)
    return {key: criteria.get(key) for key in keys if criteria.get(key) not in (None, [], "")}


def active_search_id(previous):
    state = previous.get("state", {}) if isinstance(previous, dict) else {}
    active = state.get("active_search_id") if isinstance(state, dict) else None
    return active if isinstance(active, str) and active != "legacy" else None


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
    segments = [segment.strip().casefold() for segment in re.split(r"[.!?]+", reply) if segment.strip()]
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
    prohibited = ("tenemos disponible", "encontré", "encontre", "hay disponibilidad", "opción recomendada", "opcion recomendada", "la recomendada", "revisando disponibilidad", "cuenta con", "te mostraré las opciones", "te mostrare las opciones")
    return None if any(term in reply.lower() for term in prohibited) or not assistant_reply_is_real_estate_scoped(reply) else reply


def inventory_ready(criteria):
    """Only run a concrete inventory search after the P0 qualification is complete."""
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
    districts = {district.casefold() for district in criteria.get("districts", []) if isinstance(district, str)}
    if property_row.get("operation") != criteria.get("operation"):
        return False
    if property_row.get("district", "").casefold() not in districts:
        return False
    if property_row.get("currency") != criteria.get("currency"):
        return False
    if canonical_property_type(property_row.get("property_type")) != criteria.get("property_type"):
        return False
    if criteria.get("parking_required") is True and not property_row.get("parking_spaces"):
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
        return float(property_row["price_amount"]) <= float(criteria["budget_max"])
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


class InventoryStore:
    """Small REST adapter for canonical inventory; it never exposes internal fields."""
    property_fields = ("property_id,public_reference,operation,property_type,district,zone,public_location_reference,"
                      "price_amount,currency,bedrooms,bathrooms,area_m2,area_total_m2,area_built_m2,terrace_area_m2,"
                      "parking_spaces,features,public_description,lifecycle_state,availability_confirmed_at,approved_at,"
                      "provenance,verification_status,collaboration_confirmed_at")

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
            "currency": f"eq.{criteria['currency']}",
            "price_amount": f"lte.{criteria['budget_max']}",
            "property_type": f"eq.{criteria['property_type']}",
            "district": "in.(" + ",".join(criteria["districts"]) + ")",
        }
        response = requests.get(f"{self.url}/rest/v1/inventory_properties", headers=self.headers, params=params, timeout=10)
        response.raise_for_status()
        candidates = [row for row in response.json() if property_is_eligible(row, now) and property_matches_criteria(row, criteria)]
        candidates.sort(key=lambda row: (-property_match_rank(row, criteria), float(row["price_amount"]), row["public_reference"]))
        candidates = candidates[:INVENTORY_MAX_RESULTS]
        media_by_property = self._approved_media([row["property_id"] for row in candidates])
        return [{"property": row, "public": public_property_snapshot(row, media_by_property.get(row["property_id"], []))} for row in candidates], cutoff

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
        if public["media"]:
            first_media = public["media"][0]
            lines.append(f"{first_media['media_type'].capitalize()}: {first_media['media_url']}")
    if borrowed:
        lines.append(THIRD_PARTY_NOTE)
    return "\n".join(lines)


PRESENTED_REFERENCE_PATTERN = re.compile(r"\bref\.?\s*([a-z0-9][a-z0-9-]{1,23})\b", re.IGNORECASE)
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
    for turn in reversed(recent_conversation_turns(previous)):
        if turn["direction"] == "assistant":
            return list(dict.fromkeys(PRESENTED_REFERENCE_PATTERN.findall(turn["content"])))
    return []


def selected_option(previous, text):
    normalized = (text or "").casefold().strip().strip(".!¡¿?")
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


def parking_question_reply(item):
    public = item["public"]
    spaces = public.get("parking_spaces")
    reference = public["public_reference"]
    if spaces is None:
        fact = "La ficha de esta unidad no indica si cuenta con estacionamiento."
    elif spaces == 0:
        fact = "La ficha registra esta unidad sin estacionamiento incluido."
    else:
        fact = f"La ficha registra {spaces} estacionamiento(s), pero ese dato por sí solo no confirma si están incluidos en el precio."
    return (f"Ref. {reference}\n{fact} "
            "Para confirmarlo, un asesor debe consultar con el responsable del proyecto "
            "si hay una cochera disponible para esta unidad, si está incluida en el precio del departamento "
            "y, si se vende por separado, cuál es su precio. "
            "Esa confirmación aún está pendiente; no puedo asegurar que esté incluida.")


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


def detected_handoff(previous, observation, user_text, inventory_matches=None):
    """Resolve one escalation per turn, or None to continue the qualification."""
    reference = mentioned_property_reference(user_text, presented_property_references(previous, inventory_matches))
    request_type = handoff_request_type(user_text, reference)
    if not request_type and isinstance(observation, dict) and (observation.get("handoff") or observation.get("intent") == "human_handoff"):
        request_type = "advisor"
    if not request_type:
        return None
    return {"request_type": request_type, "property_reference": reference if request_type in REFERENCED_HANDOFF_TYPES else None}


def handoff_reply(handoff):
    request_type, reference = handoff["request_type"], handoff["property_reference"]
    if request_type == "callback":
        return (f"Perfecto. Registré tu solicitud de llamada; un asesor de {COMMERCIAL_BRAND} coordinará contigo.",
                "Solicitud de llamada registrada con consentimiento explícito.")
    if request_type == "visit":
        return (f"Perfecto. Registré tu solicitud de visita para la referencia {reference}; un asesor de {COMMERCIAL_BRAND} coordinará contigo.",
                f"Solicitud de visita registrada para la referencia {reference}.")
    if request_type == "property_interest":
        return (f"Perfecto. Registré tu interés en la referencia {reference}; un asesor de {COMMERCIAL_BRAND} continuará contigo.",
                f"Interés registrado en la referencia {reference}.")
    return (f"Perfecto. Registraré tu solicitud para que un asesor de {COMMERCIAL_BRAND} continúe contigo.",
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


def commercial_next_question(criteria):
    if criteria.get("operation") == "compra" and not criteria.get("payment_method"):
        return "Para orientarte mejor, ¿piensas comprar al contado, con crédito o aún estás evaluando cómo financiarlo?"
    if not criteria.get("purchase_timeline"):
        return "¿Para cuándo te gustaría concretarlo, o por ahora estás evaluando opciones sin urgencia?"
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
        question = "¿Prefieres ampliar las zonas de búsqueda o revisar el presupuesto?"
    return reply + "\n\n" + question


def progressive_reply(previous, observation, fallback, user_text=None, prior_criteria=None, inventory_matches=None):
    """Policy layer: AI extracts; bounded code validates state and chooses the reply."""
    greeting = re.sub(r"[^\w\s]", "", (user_text or "").casefold()).strip()
    if greeting in {"hola", "buenos días", "buenos dias", "buenas tardes", "buenas noches"}:
        criteria = dict(prior_criteria if prior_criteria is not None else conversation_state(previous))
        if criteria:
            reply = f"Hola, soy {ASSISTANT_NAME}, asesora de {COMMERCIAL_BRAND}. ¿Quieres retomar tu búsqueda anterior o comenzar una nueva?"
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
            return selected_unit_reply(item), criteria, "property_detail", f"Detalle de la referencia seleccionada {selection['reference']}."
        return "No pude reconfirmar la unidad que elegiste entre las opciones actuales. ¿Quieres revisar las alternativas vigentes?", criteria, "conversation", "Unidad anterior no reconfirmada; no se sustituye por otra posición."
    if not observation:
        if is_new_search_request(user_text):
            return "Nueva búsqueda iniciada. ¿Deseas comprar, alquilar o vender?", {}, "qualification", "Nueva búsqueda creada con criterios vacíos."
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
    intent = observation.get("intent")
    if intent == "greeting" and not criteria and not conversation_state(previous):
        return INTRODUCTION, criteria, "qualification", "Apertura cálida; escuchar la necesidad antes de calificar."
    handoff = detected_handoff(previous, observation, user_text, inventory_matches)
    if handoff:
        reply, summary = handoff_reply(handoff)
        return reply, criteria, "handoff", summary
    if (previous or {}).get("stage") == "property_detail" and intent == "general_question" and inventory_matches is not None and criteria == conversation_state(previous):
        references = latest_presented_references(previous)
        if len(references) == 1:
            item = next((m for m in inventory_matches if m["public"]["public_reference"] == references[0]), None)
            if item:
                return selected_unit_reply(item), criteria, "property_detail", f"Consulta sobre la unidad seleccionada {references[0]}."
    if intent == "general_question":
        natural_reply = usable_assistant_reply(observation, "conversation")
        if inventory_matches is not None and inventory_ready(criteria):
            return inventory_conversation_reply(inventory_matches, criteria), criteria, "qualified", "Consulta de inventario con siguiente paso comercial."
        if natural_reply:
            return natural_reply, criteria, "conversation", "Consulta atendida; criterios conservados."
        return "Claro. Puedo orientarte sobre tu búsqueda inmobiliaria. ¿Qué deseas consultar?", criteria, "conversation", "Consulta libre; criterios conservados."
    missing = [("operation", "¿Deseas comprar, alquilar o vender?"), ("property_type", "¿Qué tipo de inmueble buscas?"), ("districts", "¿En qué distrito o zona estás interesado?"), ("budget_max", "¿Cuál es tu presupuesto máximo aproximado?"), ("currency", "¿Tu presupuesto es en USD o PEN?"), ("bedrooms", "¿Cuántos dormitorios necesitas?")]
    for key, question in missing:
        if criteria.get(key) in (None, [], ""):
            if (is_new_search_request(user_text) or intent == "new_search") and key == "operation":
                return "Nueva búsqueda iniciada. ¿Deseas comprar, alquilar o vender?", criteria, "qualification", "Nueva búsqueda creada con criterios vacíos."
            natural_reply = usable_assistant_reply(observation, "qualification")
            if natural_reply:
                return natural_reply, criteria, "qualification", "Respuesta IA validada; faltan criterios por completar."
            return question, criteria, "qualification", "Continuar calificación con el siguiente criterio faltante."
    if inventory_matches is not None and inventory_ready(criteria):
        reply = inventory_conversation_reply(inventory_matches, criteria)
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
    payload = {"p_message_id": message_id, "p_claim_token": claim_token,
               "p_profile": _profile_payload(sender, criteria, inbound, outbound, stage),
               "p_session": {"phone": sender, "stage": stage, "state": session_state, "summary": summary},
               "p_search": {"search_id": active_id, "phone": sender, "operation": criteria.get("operation"), "state": {**current, "criteria": criteria, "recent_turns": (recent_conversation_turns(previous) + [{"direction":"user","content":inbound},{"direction":"assistant","content":outbound}])[-4:]}},
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


def notify_advisor_of_handoff(sender, stage, previous, observation, text, criteria, inventory_matches=None):
    """After the client has their reply, tell the advisor. Best effort; never raises."""
    if stage != "handoff":
        return 0
    try:
        handoff = detected_handoff(previous, sanitize_new_search_observation(observation, text), text, inventory_matches)
        return advisor_notify.notify_advisors(post_graph_message, sender, handoff, criteria, text)
    except Exception:
        logger.exception("Advisor notice skipped")
        return 0


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
            fallback = generate_reply(sender, text)
            started_at = time.monotonic()
            try:
                previous = memory.load_session(sender) if memory else None
            except requests.RequestException:
                previous = None
                logger.warning("Conversation memory unavailable; using in-process fallback")
            timings["session_read_ms"] = round((time.monotonic() - started_at) * 1000)
            observation = observe_conversation(sender, text, fallback, timings, previous, True)
            # One effective observation governs reply, search selection, and persistence.
            # This prevents an explicit operation from updating the prior search only in
            # the response layer while persistence still sees the unnormalized input.
            effective_observation = with_explicit_operation(observation, text)
            if selected_option(previous, text) or is_selected_parking_question(previous, text):
                effective_observation = {**(effective_observation or {}), "intent": "confirmation",
                                         "slot_updates": {}, "criteria_actions": [], "criteria_change": False, "handoff": False}
            turn_state, turn_prior_criteria = search_state_for_turn(previous, effective_observation, text)
            reply, criteria, stage, summary = progressive_reply(previous, effective_observation, fallback, text, turn_prior_criteria)
            inventory_store = get_inventory_store()
            inventory_matches, inventory_cutoff = None, None
            if inventory_store and inventory_ready(criteria):
                inventory_matches, inventory_cutoff = inventory_store.find_matches(criteria)
                reply, criteria, stage, summary = progressive_reply(previous, effective_observation, fallback, text, turn_prior_criteria, inventory_matches)
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
                send_whatsapp_message(sender, reply)
                if memory: memory.record_delivery(message_id, "sent")
            except requests.RequestException as error:
                # Internal commit remains processed. Never retry Meta automatically.
                if memory:
                    code = f"meta_http_{getattr(error.response, 'status_code', None)}" if getattr(error, "response", None) is not None else "meta_transport"
                    memory.record_delivery(message_id, "failed", code)
                logger.warning("WhatsApp delivery failed after internal commit: category=%s", type(error).__name__)
            # The handoff is already committed; the notice to the advisor comes last so it
            # can never delay or break the client's reply.
            notify_advisor_of_handoff(sender, stage, previous, effective_observation, text, criteria, inventory_matches)
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


