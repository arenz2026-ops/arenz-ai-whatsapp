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
        "intent": {"type": "string", "enum": ["property_search", "new_search", "general_question", "change_criteria", "human_handoff", "greeting", "confirmation", "unknown"]},
        "slot_updates": {"type": "object", "additionalProperties": False, "properties": {"operation": {"type": ["string", "null"]}, "districts": {"type": "array", "items": {"type": "string"}}, "budget_max": {"type": ["number", "null"]}, "currency": {"type": ["string", "null"]}, "bedrooms": {"type": ["integer", "null"]}, "property_type": {"type": ["string", "null"]}, "preferences": {"type": "array", "items": {"type": "string"}}}, "required": ["operation", "districts", "budget_max", "currency", "bedrooms", "property_type", "preferences"]},
        "criteria_change": {"type": "boolean"},
        "criteria_actions": {"type": "array", "items": {"type": "object", "additionalProperties": False, "properties": {"action": {"type": "string", "enum": ["ADD", "UPDATE", "REMOVE"]}, "field": {"type": "string", "enum": ["districts", "budget_max", "currency", "bedrooms", "property_type", "preferences"]}, "values": {"type": "array", "items": {"type": ["string", "number", "integer"]}}}, "required": ["action", "field", "values"]}},
        "user_question": {"type": ["string", "null"]},
        "next_action": {"type": "string", "enum": ["reply", "ask_clarification", "confirm", "search_inventory", "handoff"]},
        "handoff": {"type": "boolean"},
        "assistant_reply": {"type": "string"}
    },
    "required": ["intent", "slot_updates", "criteria_change", "criteria_actions", "user_question", "next_action", "handoff", "assistant_reply"]
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
        rows = self.rpc("commit_webhook_message", payload)
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
    context = {"stage": previous.get("stage") if isinstance(previous, dict) else None, "active_search_id": active_search_id(previous), "criteria": conversation_state(previous), "recent_turns": recent_conversation_turns(previous)}
    return {
        "model": OPENAI_MODEL, "store": False, "max_output_tokens": 1200,
        "instructions": "Analiza una conversación inmobiliaria en Lima. Extrae solo datos explícitos o altamente confiables. No inventes inmuebles, precios ni disponibilidad. Devuelve únicamente el JSON del esquema. NUEVA BÚSQUEDA es intent new_search. Para cambios usa criteria_actions: ADD agrega, UPDATE reemplaza y REMOVE elimina; 'ya no es necesario' es REMOVE, nunca una preferencia negativa. Un cambio explícito entre compra, alquiler y venta inicia un contexto independiente. assistant_reply debe ser natural, útil y máximo 180 caracteres, pero nunca afirmar disponibilidad, recomendación ni características de una propiedad concreta. No preguntes un criterio ya presente en Contexto salvo ambigüedad o conflicto. user_question máximo 120 caracteres; máximo tres distritos y tres preferencias.",
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
INVENTORY_RULES_VERSION = "p1-v1"
INVENTORY_MAX_RESULTS = 3
INVENTORY_VERIFICATION_DAYS = 7


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


def with_explicit_operation(observation, text):
    """Overlay a deterministic explicit operation while preserving extractor output."""
    operation = explicit_operation_from_text(text)
    if not operation:
        return observation
    result = dict(observation) if isinstance(observation, dict) else {"intent": "change_criteria", "handoff": False}
    slots = dict(result.get("slot_updates", {}))
    slots["operation"] = operation
    result["slot_updates"] = slots
    return result


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
    property_type = slot_updates.get("property_type")
    if isinstance(property_type, str) and 1 <= len(property_type.strip()) <= 40:
        clean["property_type"] = property_type.strip().lower()
    preferences = slot_updates.get("preferences")
    if isinstance(preferences, list):
        preferences = [value.strip().lower() for value in preferences if isinstance(value, str) and 1 <= len(value.strip()) <= 80][:10]
        if preferences:
            clean["preferences"] = preferences
    return clean


def conversation_state(previous):
    state = previous.get("state", {}) if isinstance(previous, dict) else {}
    searches = state.get("searches", {}) if isinstance(state, dict) else {}
    active = state.get("active_search_id") if isinstance(state, dict) else None
    if isinstance(searches, dict) and active in searches and isinstance(searches[active], dict):
        criteria = searches[active].get("criteria", {})
    else:
        criteria = state.get("criteria", {}) if isinstance(state, dict) else {}
    return {key: criteria.get(key) for key in ("operation", "districts", "budget_max", "currency", "bedrooms", "property_type", "preferences") if criteria.get(key) not in (None, [], "")}


def active_search_id(previous):
    state = previous.get("state", {}) if isinstance(previous, dict) else {}
    active = state.get("active_search_id") if isinstance(state, dict) else None
    return active if isinstance(active, str) else None


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
        active = active or "legacy"
        searches[active] = {"criteria": conversation_state(previous)}
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
    for action in actions if isinstance(actions, list) else []:
        if not isinstance(action, dict):
            continue
        kind, field, values = action.get("action"), action.get("field"), action.get("values")
        if kind not in {"ADD", "UPDATE", "REMOVE"} or field not in {"districts", "budget_max", "currency", "bedrooms", "property_type", "preferences"}:
            continue
        cleaned = validated_slot_updates({field: values}).get(field)
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


def usable_assistant_reply(observation):
    reply = observation.get("assistant_reply") if isinstance(observation, dict) else None
    if not isinstance(reply, str):
        return None
    reply = reply.strip()
    if not reply or len(reply) > 280:
        return None
    prohibited = ("tenemos disponible", "encontré", "encontre", "hay disponibilidad", "opción recomendada", "opcion recomendada", "la recomendada", "revisando disponibilidad", "cuenta con", "te mostraré las opciones", "te mostrare las opciones")
    return None if any(term in reply.lower() for term in prohibited) else reply


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
        "parking_spaces": property_row.get("parking_spaces"),
        "features": property_row.get("features") or [],
        "public_description": property_row.get("public_description"),
        "availability_confirmed_at": property_row["availability_confirmed_at"],
        "media": approved_media,
    }


def property_is_eligible(property_row, now=None):
    """The P1 eligibility rule is deterministic and independent of model output."""
    now = now or datetime.now(timezone.utc)
    required = ("public_reference", "operation", "property_type", "district", "price_amount", "currency", "approved_at", "availability_confirmed_at")
    if property_row.get("lifecycle_state") != "active_confirmed" or any(property_row.get(field) in (None, "") for field in required):
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
    if property_row.get("property_type") != criteria.get("property_type"):
        return False
    try:
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
    property_fields = "property_id,public_reference,operation,property_type,district,zone,public_location_reference,price_amount,currency,bedrooms,bathrooms,area_m2,parking_spaces,features,public_description,lifecycle_state,availability_confirmed_at,approved_at"

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


def inventory_reply(matches):
    if not matches:
        return "No encontré opciones verificadas que coincidan exactamente con tus criterios en este momento. Puedo ampliar distrito, presupuesto o derivar tu solicitud a un asesor."
    lines = ["Encontré opciones verificadas que coinciden con tus criterios:"]
    for item in matches:
        public = item["public"]
        details = [f"Ref. {public['public_reference']}", public["property_type"].capitalize(), public["district"], f"{public['currency']} {public['price_amount']}", f"{public['bedrooms']} dormitorios"]
        if public.get("parking_spaces"):
            details.append(f"{public['parking_spaces']} estacionamiento(s)")
        lines.append(" · ".join(details) + ".")
        if public["media"]:
            first_media = public["media"][0]
            lines.append(f"{first_media['media_type'].capitalize()}: {first_media['media_url']}")
    return "\n".join(lines)


def progressive_reply(previous, observation, fallback, user_text=None, prior_criteria=None, inventory_matches=None):
    """Policy layer: AI extracts; bounded code validates state and chooses the reply."""
    observation = with_explicit_operation(observation, user_text)
    observation = sanitize_new_search_observation(observation, user_text)
    if not observation:
        if is_new_search_request(user_text):
            return "Nueva búsqueda iniciada. ¿Deseas comprar, alquilar o vender?", {}, "qualification", "Nueva búsqueda creada con criterios vacíos."
        return fallback, conversation_state(previous), "fallback", "IA no disponible; flujo determinista aplicado."
    if prior_criteria is None:
        _, prior_criteria = search_state_for_turn(previous, observation, user_text)
    criteria = apply_criteria_actions(prior_criteria, observation.get("criteria_actions"))
    slots = validated_slot_updates(observation.get("slot_updates"))
    if observation.get("criteria_change") and "preferences" in slots and not observation.get("criteria_actions"):
        criteria["preferences"] = list(dict.fromkeys(criteria.get("preferences", []) + slots.pop("preferences")))
    criteria.update(slots)
    intent = observation.get("intent")
    if observation.get("handoff") or intent == "human_handoff":
        return "Perfecto. Registraré tu solicitud para que un asesor de ARENZ continúe contigo.", criteria, "handoff", "Solicitud de asesor registrada."
    natural_reply = usable_assistant_reply(observation)
    if intent == "general_question":
        if inventory_matches is not None and inventory_ready(criteria):
            return inventory_reply(inventory_matches), criteria, "qualified", "Consulta de inventario respondida desde registros verificados."
        if natural_reply:
            return natural_reply, criteria, "conversation", "Consulta atendida; criterios conservados."
        return "Claro. Puedo orientarte sobre tu búsqueda inmobiliaria. ¿Qué deseas consultar?", criteria, "conversation", "Consulta libre; criterios conservados."
    missing = [("operation", "¿Deseas comprar, alquilar o vender?"), ("property_type", "¿Qué tipo de inmueble buscas?"), ("districts", "¿En qué distrito o zona estás interesado?"), ("budget_max", "¿Cuál es tu presupuesto máximo aproximado?"), ("currency", "¿Tu presupuesto es en USD o PEN?"), ("bedrooms", "¿Cuántos dormitorios necesitas?")]
    for key, question in missing:
        if criteria.get(key) in (None, [], ""):
            if (is_new_search_request(user_text) or intent == "new_search") and key == "operation":
                return "Nueva búsqueda iniciada. ¿Deseas comprar, alquilar o vender?", criteria, "qualification", "Nueva búsqueda creada con criterios vacíos."
            if natural_reply:
                return natural_reply, criteria, "qualification", "Respuesta IA validada; faltan criterios por completar."
            return question, criteria, "qualification", "Continuar calificación con el siguiente criterio faltante."
    if inventory_matches is not None and inventory_ready(criteria):
        return inventory_reply(inventory_matches), criteria, "qualified", "Consulta de inventario respondida desde registros verificados."
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


def _profile_payload(phone, criteria, inbound, outbound, stage):
    status = "pendiente_asesor" if stage == "handoff" else "en_calificacion"
    return {"phone": phone, "intent": criteria.get("operation"), "district": ", ".join(criteria.get("districts", [])) or None,
            "budget": f"{criteria.get('currency')} {criteria.get('budget_max')}" if criteria.get("currency") and criteria.get("budget_max") is not None else None,
            "bedrooms": str(criteria["bedrooms"]) if criteria.get("bedrooms") is not None else None,
            "conversation": {"last_user_message": inbound, "last_assistant_message": outbound}, "status": status,
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
        payload["p_inventory_results"] = [{"property_id": item["property_id"], "rank_position": index, "presented_to_client": True,
                                            "eligibility_snapshot": item.get("eligibility_snapshot", {}), "public_snapshot": item.get("public", {})}
                                           for index, item in enumerate(inventory_matches, 1)]
    return memory.commit_work(payload)


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

