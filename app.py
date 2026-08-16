import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
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

OBSERVATION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "intent": {"type": "string", "enum": ["property_search", "general_question", "change_criteria", "human_handoff", "greeting", "confirmation", "unknown"]},
        "slot_updates": {"type": "object", "additionalProperties": False, "properties": {"operation": {"type": ["string", "null"]}, "districts": {"type": "array", "items": {"type": "string"}}, "budget_max": {"type": ["number", "null"]}, "currency": {"type": ["string", "null"]}, "bedrooms": {"type": ["integer", "null"]}, "property_type": {"type": ["string", "null"]}, "preferences": {"type": "array", "items": {"type": "string"}}}, "required": ["operation", "districts", "budget_max", "currency", "bedrooms", "property_type", "preferences"]},
        "criteria_change": {"type": "boolean"},
        "user_question": {"type": ["string", "null"]},
        "next_action": {"type": "string", "enum": ["reply", "ask_clarification", "confirm", "search_inventory", "handoff"]},
        "handoff": {"type": "boolean"},
        "assistant_reply": {"type": "string"}
    },
    "required": ["intent", "slot_updates", "criteria_change", "user_question", "next_action", "handoff", "assistant_reply"]
}


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
        payload = {"phone": phone, "intent": session.get("intent") or "consulta inmobiliaria", "district": session.get("district"), "budget": session.get("budget"), "bedrooms": session.get("bedrooms"), "conversation": {"last_user_message": inbound, "last_assistant_message": reply}, "status": status, "next_action": next_action}
        response = requests.post(f"{self.url}/rest/v1/leads", headers={**self.headers, "Prefer": "return=minimal"}, json=payload, timeout=10)
        response.raise_for_status()

    def get_lead(self, phone):
        response = requests.get(f"{self.url}/rest/v1/leads", headers=self.headers, params={"phone": f"eq.{phone}", "select": "*"}, timeout=10)
        response.raise_for_status()
        rows = response.json()
        return rows[0] if rows else None


class ConversationMemoryStore:
    """Durable conversation state and turn history, keyed by WhatsApp phone."""
    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def load_session(self, phone):
        response = requests.get(f"{self.url}/rest/v1/conversation_sessions", headers=self.headers, params={"phone": f"eq.{phone}", "select": "state,summary,stage"}, timeout=10)
        response.raise_for_status()
        rows = response.json()
        return rows[0] if rows else None

    def record_turn(self, phone, message_id, inbound, outbound, observation, state, stage, summary, timings=None):
        now = datetime.now(timezone.utc).isoformat()
        session = {"phone": phone, "stage": stage, "state": {**state, "last_observation": observation, "last_user_message": inbound, "last_assistant_message": outbound}, "summary": summary, "updated_at": now}
        started_at = time.monotonic()
        response = requests.post(f"{self.url}/rest/v1/conversation_sessions?on_conflict=phone", headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"}, json=session, timeout=10)
        response.raise_for_status()
        if timings is not None:
            timings["session_write_ms"] = round((time.monotonic() - started_at) * 1000)
        messages = [
            {"phone": phone, "message_key": f"in:{message_id}", "direction": "inbound", "content": inbound, "extraction": observation},
            {"phone": phone, "message_key": f"out:{message_id}", "direction": "outbound", "content": outbound, "extraction": None},
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


def recent_conversation_turns(previous, limit=4):
    """Return a small durable dialogue window, without internal observation data."""
    state = previous.get("state", {}) if isinstance(previous, dict) else {}
    turns = state.get("recent_turns", []) if isinstance(state, dict) else []
    clean = []
    for turn in turns if isinstance(turns, list) else []:
        if not isinstance(turn, dict):
            continue
        direction, content = turn.get("direction"), turn.get("content")
        if direction in ("user", "assistant") and isinstance(content, str) and content.strip():
            clean.append({"direction": direction, "content": content.strip()[:280]})
    if not clean and isinstance(state, dict):
        for direction, key in (("user", "last_user_message"), ("assistant", "last_assistant_message")):
            content = state.get(key)
            if isinstance(content, str) and content.strip():
                clean.append({"direction": direction, "content": content.strip()[:280]})
    return clean[-limit:]


def build_observation_payload(previous, text):
    """Build a bounded request from durable criteria and recent dialogue."""
    context = {"stage": previous.get("stage") if isinstance(previous, dict) else None, "criteria": conversation_state(previous), "recent_turns": recent_conversation_turns(previous)}
    return {
        "model": OPENAI_MODEL, "store": False, "max_output_tokens": 1200,
        "instructions": "Analiza una conversación inmobiliaria en Lima. Extrae solo datos explícitos o altamente confiables. No inventes inmuebles, precios ni disponibilidad. Devuelve únicamente el JSON del esquema. assistant_reply es la respuesta normal para todos los intents no handoff: natural, útil y máximo 180 caracteres. Reconoce explícitamente cambios o preferencias nuevos. No preguntes un criterio ya presente en Contexto salvo ambigüedad o conflicto. user_question máximo 120 caracteres; máximo tres distritos y tres preferencias.",
        "text": {"format": {"type": "json_schema", "name": "arenz_conversation_observation", "strict": True, "schema": OBSERVATION_SCHEMA}},
        "input": f"Contexto: {json.dumps(context, ensure_ascii=False)}\nMensaje: {text}",
    }


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
        response = requests.post("https://api.openai.com/v1/responses", headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}, json=payload, timeout=15)
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


def validated_slot_updates(slot_updates):
    """Accept only bounded, typed criteria from the structured AI contract."""
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
        clean["districts"] = [value.strip().title() for value in districts if isinstance(value, str) and 1 <= len(value.strip()) <= 60][:3]
    budget = slot_updates.get("budget_max")
    if isinstance(budget, (int, float)) and 1000 <= budget <= 10000000:
        clean["budget_max"] = int(budget)
    currency = slot_updates.get("currency")
    if isinstance(currency, str) and currency.upper() in ALLOWED_CURRENCIES:
        clean["currency"] = currency.upper()
    bedrooms = slot_updates.get("bedrooms")
    if isinstance(bedrooms, int) and 0 <= bedrooms <= 20:
        clean["bedrooms"] = bedrooms
    property_type = slot_updates.get("property_type")
    if isinstance(property_type, str) and 1 <= len(property_type.strip()) <= 40:
        clean["property_type"] = property_type.strip().lower()
    preferences = slot_updates.get("preferences")
    if isinstance(preferences, list):
        clean["preferences"] = [value.strip().lower() for value in preferences if isinstance(value, str) and 1 <= len(value.strip()) <= 80][:10]
    return clean


def conversation_state(previous):
    state = previous.get("state", {}) if isinstance(previous, dict) else {}
    criteria = state.get("criteria", {}) if isinstance(state, dict) else {}
    return {key: criteria.get(key) for key in ("operation", "districts", "budget_max", "currency", "bedrooms", "property_type", "preferences") if criteria.get(key) not in (None, [], "")}


def usable_assistant_reply(observation):
    reply = observation.get("assistant_reply") if isinstance(observation, dict) else None
    if not isinstance(reply, str):
        return None
    reply = reply.strip()
    if not reply or len(reply) > 280:
        return None
    prohibited = ("tenemos disponible", "encontré disponible", "hay disponibilidad")
    return None if any(term in reply.lower() for term in prohibited) else reply


def progressive_reply(previous, observation, fallback):
    """Policy layer: AI extracts; bounded code validates state and chooses the reply."""
    if not observation:
        return fallback, conversation_state(previous), "fallback", "IA no disponible; flujo determinista aplicado."
    criteria = conversation_state(previous)
    criteria.update(validated_slot_updates(observation.get("slot_updates")))
    intent = observation.get("intent")
    if observation.get("handoff") or intent == "human_handoff":
        return "Perfecto. Registraré tu solicitud para que un asesor de ARENZ continúe contigo.", criteria, "handoff", "Solicitud de asesor registrada."
    natural_reply = usable_assistant_reply(observation)
    if intent == "general_question":
        if natural_reply:
            return natural_reply, criteria, "conversation", "Consulta atendida; criterios conservados."
        return "Claro. Puedo orientarte sobre tu búsqueda inmobiliaria. ¿Qué deseas consultar?", criteria, "conversation", "Consulta libre; criterios conservados."
    missing = [("operation", "¿Deseas comprar, alquilar o vender?"), ("property_type", "¿Qué tipo de inmueble buscas?"), ("districts", "¿En qué distrito o zona estás interesado?"), ("budget_max", "¿Cuál es tu presupuesto máximo aproximado?"), ("currency", "¿Tu presupuesto es en USD o PEN?"), ("bedrooms", "¿Cuántos dormitorios necesitas?")]
    for key, question in missing:
        if criteria.get(key) in (None, [], ""):
            if natural_reply:
                return natural_reply, criteria, "qualification", "Respuesta IA validada; faltan criterios por completar."
            return question, criteria, "qualification", "Continuar calificación con el siguiente criterio faltante."
    if natural_reply:
        return natural_reply, criteria, "qualified", "Criterios completos; respuesta IA validada."
    district = ", ".join(criteria["districts"])
    return f"Entendido: {criteria['operation']} de {criteria['property_type']} en {district}, hasta {criteria['currency']} {criteria['budget_max']:,} y {criteria['bedrooms']} dormitorios. ¿Deseas añadir alguna preferencia, como balcón o estacionamiento?", criteria, "qualified", "Criterios básicos completos; sin derivación automática."


def persist_conversation_turn(sender, message_id, inbound, outbound, observation, criteria, stage, summary, previous=None, timings=None):
    memory = get_conversation_memory_store()
    if not memory:
        return
    try:
        turns = recent_conversation_turns(previous) + [{"direction": "user", "content": inbound}, {"direction": "assistant", "content": outbound}]
        memory.record_turn(sender, message_id, inbound, outbound, observation, {"criteria": criteria, "recent_turns": turns[-4:]}, stage, summary, timings)
    except requests.RequestException:
        logger.warning("Conversation memory persistence unavailable")


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
    webhook_started_at = time.monotonic()
    raw = request.get_data(cache=True)
    if not valid_meta_signature(raw, request.headers.get("X-Hub-Signature-256", "")):
        logger.warning("Rejected webhook with invalid signature"); return jsonify({"status":"invalid_signature"}), 401
    messages = iter_incoming_messages(request.get_json(silent=True))
    if not messages: return jsonify({"status":"ignored"}), 200
    store = get_lead_store()
    try:
        processed = 0
        for message in messages:
            timings = {}
            started_at = time.monotonic()
            if not store.claim_message(message.get("id")): continue
            timings["dedupe_ms"] = round((time.monotonic() - started_at) * 1000)
            sender, text = message["from"], message["text"]["body"]
            fallback = generate_reply(sender, text)
            memory = get_conversation_memory_store()
            started_at = time.monotonic()
            try:
                previous = memory.load_session(sender) if memory else None
            except requests.RequestException:
                previous = None
                logger.warning("Conversation memory unavailable; using in-process fallback")
            timings["session_read_ms"] = round((time.monotonic() - started_at) * 1000)
            observation = observe_conversation(sender, text, fallback, timings, previous, True)
            reply, criteria, stage, summary = progressive_reply(previous, observation, fallback)
            persist_conversation_turn(sender, message.get("id"), text, reply, observation, criteria, stage, summary, previous, timings)
            started_at = time.monotonic()
            store.upsert_lead(sender, user_sessions.get(sender, {}), text, reply)
            timings["lead_write_ms"] = round((time.monotonic() - started_at) * 1000)
            started_at = time.monotonic()
            send_whatsapp_message(sender, reply)
            timings["graph_ms"] = round((time.monotonic() - started_at) * 1000)
            timings["total_ms"] = round((time.monotonic() - webhook_started_at) * 1000)
            logger.info("Webhook timing: dedupe_ms=%s session_read_ms=%s context_ms=%s openai_ms=%s session_write_ms=%s messages_write_ms=%s lead_write_ms=%s graph_ms=%s total_ms=%s", timings.get("dedupe_ms"), timings.get("session_read_ms"), timings.get("context_ms"), timings.get("openai_ms"), timings.get("session_write_ms"), timings.get("messages_write_ms"), timings.get("lead_write_ms"), timings.get("graph_ms"), timings.get("total_ms"))
            processed += 1
        return jsonify({"status":"received" if processed else "duplicate", "processed":processed}), 200
    except (requests.RequestException, RuntimeError, sqlite3.Error):
        logger.exception("Webhook processing failed"); return jsonify({"status":"error"}), 500


if __name__ == "__main__": app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
