"""Seguimiento automático ARENZ: un mensaje a quien recibió opciones o una ficha y no respondió.

Como en el seguimiento manual de setiembre y octubre ("¿Pudiste revisar las opciones que te
compartí?"), pero con límites:
- Solo conversaciones en etapa qualified (vio un listado) o property_detail (vio una ficha).
  Nunca tras un cierre, un "lo voy a pensar", un "no me contacten" ni una derivación al asesor.
- Un solo seguimiento por silencio: si el cliente no contesta, no se insiste.
- Solo dentro de las 24 h desde su último mensaje (WhatsApp no permite texto libre después)
  y solo en horario comercial de Lima.
- Nunca a quien el equipo marcó en el CRM CREENZIA con «No hacer seguimiento» (crm_leads.follow_up = 'no')
  ni a quien atiende una persona (crm_leads.bot_paused).

Se ejecuta desde scripts/enviar_seguimientos.py (por ejemplo, una tarea programada cada hora).
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import requests

import app

logger = logging.getLogger("arenz.followups")

LIMA = timezone(timedelta(hours=-5))
FOLLOWUP_STAGES = ("qualified", "property_detail")
WHATSAPP_WINDOW_HOURS = 23.5  # margen bajo las 24 h de la ventana de atención de WhatsApp
LISTING_MARK = "Responde con el número de la opción"
VISIT_QUESTION = "¿Quieres que coordinemos una visita al departamento?"


def settings():
    def number(key, default):
        try:
            return float(os.getenv(key, default))
        except ValueError:
            return float(default)
    return {"after_hours": number("SEGUIMIENTO_HORAS", "20"),
            "start_hour": int(number("SEGUIMIENTO_DESDE", "9")), "end_hour": int(number("SEGUIMIENTO_HASTA", "20"))}


def business_hours(now, config):
    hour = now.astimezone(LIMA).hour
    return config["start_hour"] <= hour < config["end_hour"]


def _parse(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def already_followed(session):
    """True if a follow-up went out after the client's last message (no second nudge)."""
    state = session.get("state") if isinstance(session.get("state"), dict) else {}
    sent, last = _parse(state.get("followup_sent_at")), _parse(session.get("updated_at"))
    return bool(sent and last and sent >= last)


def followup_message(previous):
    """(texto para el cliente, texto guardado) según lo último que vio; None si no aplica."""
    name = app.customer_name(previous)
    hello = f"Hola {name}, ¿cómo estás? 😊" if name else "Hola, ¿cómo estás? 😊"
    me = f"Soy {app.ASSISTANT_NAME}, de {app.COMMERCIAL_BRAND}."
    last = app.last_bot_message(previous)
    references = app.latest_presented_references(previous)
    if LISTING_MARK in last or (len(references) > 1 and (previous or {}).get("stage") == "qualified"):
        districts = (app.conversation_state(previous) or {}).get("districts") or []
        where = f" en {', '.join(districts[:-1])} y {districts[-1]}" if len(districts) > 1 else (f" en {districts[0]}" if districts else "")
        text = (f"{hello} {me}\n\nQuería saber si pudiste revisar las opciones de departamentos que te compartí{where}. "
                f"¿Alguna te llamó la atención? {LISTING_MARK} que te interesa y te envío la ficha completa y "
                "fotografías del proyecto 🏡")
        return text, text
    if references:
        text = (f"{hello} {me}\n\nQuería saber qué te pareció el departamento que te compartí. Si te interesa conocerlo "
                f"personalmente, con gusto lo coordinamos. {VISIT_QUESTION} 🏡")
        return text, text + app.photo_reference_marker(references[0])
    return None


class FollowupStore:
    """Lecturas y escrituras de Supabase que necesita el seguimiento (fuera del flujo del webhook)."""

    def __init__(self, memory):
        self.memory = memory
        self.url, self.headers = memory.url, memory.headers

    def due_sessions(self, now, config):
        newest = (now - timedelta(hours=config["after_hours"])).isoformat()
        oldest = (now - timedelta(hours=WHATSAPP_WINDOW_HOURS)).isoformat()
        response = requests.get(f"{self.url}/rest/v1/conversation_sessions", headers=self.headers, timeout=10, params=[
            ("select", "phone,stage,state,summary,updated_at"), ("stage", f"in.({','.join(FOLLOWUP_STAGES)})"),
            ("updated_at", f"lt.{newest}"), ("updated_at", f"gt.{oldest}")])
        response.raise_for_status()
        blocked = self.blocked_phones()
        if blocked is None:
            return []  # sin saber qué marcó el equipo en el CRM, no se escribe a nadie
        return [row for row in response.json() if not already_followed(row) and row.get("phone") not in blocked]

    def blocked_phones(self):
        """Números marcados en el CRM CREENZIA con «No hacer seguimiento» o con el bot en pausa. None si no se pudo leer."""
        try:
            response = requests.get(f"{self.url}/rest/v1/crm_leads", headers=self.headers, timeout=10,
                                    params={"select": "phone", "or": "(follow_up.eq.no,bot_paused.is.true)"})
            response.raise_for_status()
            return {row["phone"] for row in response.json() if row.get("phone")}
        except requests.RequestException as error:
            logger.warning("CRM follow-up decisions unavailable: category=%s", type(error).__name__)
            return None

    def claim(self, session, now):
        """Marca el seguimiento solo si el cliente no escribió desde la consulta (updated_at sin cambios)."""
        state = {**(session.get("state") or {}), "followup_sent_at": now.isoformat()}
        response = requests.patch(f"{self.url}/rest/v1/conversation_sessions", headers={**self.headers, "Prefer": "return=representation"},
                                  params={"phone": f"eq.{session['phone']}", "updated_at": f"eq.{session['updated_at']}"},
                                  json={"state": state}, timeout=10)
        response.raise_for_status()
        return bool(response.json())

    def record(self, phone, previous, stored, now):
        """El seguimiento entra en el historial, para que un 'sí' o un número se entiendan en el siguiente turno."""
        search_id = app.active_search_id(previous or {})
        if search_id:
            search = self.memory.load_search(search_id) or {}
            state = dict(search.get("state") or {})
            state["recent_turns"] = (list(state.get("recent_turns") or []) + [{"direction": "assistant", "content": stored}])[-4:]
            response = requests.patch(f"{self.url}/rest/v1/conversation_searches", headers={**self.headers, "Prefer": "return=minimal"},
                                      params={"search_id": f"eq.{search_id}"}, json={"state": state}, timeout=10)
            response.raise_for_status()
        response = requests.post(f"{self.url}/rest/v1/conversation_messages?on_conflict=message_key",
                                 headers={**self.headers, "Prefer": "resolution=ignore-duplicates,return=minimal"}, timeout=10,
                                 json=[{"phone": phone, "search_id": search_id, "message_key": f"followup:{phone}:{now.isoformat()}",
                                        "direction": "outbound", "content": stored, "extraction": None}])
        response.raise_for_status()


def run(store, now=None, dry_run=False, send=None):
    """Envía los seguimientos pendientes. Devuelve [(teléfono, resultado, texto)]."""
    now = now or datetime.now(timezone.utc)
    config = settings()
    if not business_hours(now, config):
        return []
    send = send or app.send_whatsapp_message
    results = []
    for session in store.due_sessions(now, config):
        phone = session["phone"]
        try:
            previous = store.memory.load_session(phone)
            message = followup_message(previous)
            if not message:
                results.append((phone, "sin_seguimiento", None))
                continue
            text, stored = message
            if dry_run:
                results.append((phone, "simulado", text))
                continue
            if not store.claim(session, now):
                results.append((phone, "cliente_escribio", None))
                continue
            send(phone, text)
            store.record(phone, previous, stored, now)
            results.append((phone, "enviado", text))
            logger.info("Follow-up sent: stage=%s", session.get("stage"))
        except requests.RequestException as error:
            # Uno que falla no detiene a los demás; no se reintenta para no duplicar mensajes.
            logger.warning("Follow-up failed: category=%s", type(error).__name__)
            results.append((phone, "error", None))
    return results
