"""Tell a human advisor, on WhatsApp, the moment a client asks for one.

Until now a handoff only landed in Supabase (handoff_requests) and nobody was told,
so the promise "un asesor coordinará contigo" depended on someone checking the
table. This module turns each handoff into an immediate WhatsApp notice to the
advisor numbers in ADVISOR_PHONE.

WhatsApp only allows free text to a number that wrote to the bot in the last 24 h.
An advisor who has not, receives nothing unless ADVISOR_TEMPLATE names an approved
template; then the notice is sent as that template. A notice is best effort: it is
sent after the client has their reply, and a failure is logged, never raised.
"""
import logging
import os
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("arenz.advisor")

REQUEST_LABELS = {
    "callback": "Pidió que lo llamen",
    "visit": "Pidió una visita",
    "property_interest": "Mostró interés en un inmueble",
    "advisor": "Pidió hablar con un asesor",
}
PAYMENT_LABELS = {"contado": "contado", "credito": "crédito hipotecario", "mixto": "mixto",
                  "por_definir": "por definir", "prefiere_no_indicar": "prefiere no indicar"}
CREDIT_LABELS = {"aprobado": "crédito aprobado", "en_tramite": "crédito en trámite",
                 "por_evaluar": "crédito por evaluar", "prefiere_no_indicar": "no indicó estado del crédito"}
NOT_GIVEN = "no indicado"
LIMA = timezone(timedelta(hours=-5))  # Perú no cambia de hora
# Nota CRM al estilo ARENZ: situación actual y próxima acción según lo que pidió el cliente.
CRM_STEPS = {
    "visit": ("Interés en visita – pendiente de coordinar día, hora e ingreso.",
              "Escribir hoy al cliente para coordinar la visita y confirmar la unidad que desea ver."),
    "callback": ("Pidió que lo llamen.", "Llamar hoy al cliente durante la jornada comercial."),
    "property_interest": ("Interés en un inmueble concreto.",
                          "Escribir hoy al cliente, resolver sus dudas sobre la unidad y proponer una visita."),
    "advisor": ("Pidió hablar con un asesor.", "Escribir hoy al cliente por WhatsApp para continuar la atención."),
}
# Meta rejects template parameters with line breaks, tabs or 4+ consecutive spaces.
_TEMPLATE_UNSAFE = re.compile(r"[\r\n\t]+| {4,}")


def advisor_phones():
    """Advisor numbers from ADVISOR_PHONE, comma separated, digits only (E.164 without +)."""
    phones = []
    for raw in os.getenv("ADVISOR_PHONE", "").split(","):
        digits = re.sub(r"\D", "", raw)
        if len(digits) == 9 and digits.startswith("9"):
            digits = "51" + digits  # Peruvian mobile written without country code
        if digits and digits not in phones:
            phones.append(digits)
    return phones


def _money(criteria, unit=None):
    """CUÁNTO: with a counteroffer for a unit, only what the client offers (the search budget would confuse it)."""
    if criteria.get("price_offer"):
        return _offer(criteria, unit)
    amount, currency = criteria.get("budget_max"), criteria.get("currency")
    if amount is None:
        return NOT_GIVEN
    amount = int(amount) if float(amount).is_integer() else amount
    return f"{currency or ''} {amount:,}".strip()


def _offer(criteria, unit_details=None):
    offer = criteria.get("price_offer") or {}
    name = (unit_details["short"] if unit_details and unit_details.get("reference") == offer.get("reference")
            else offer.get("reference"))
    unit = f" por {name}" if name else ""
    if offer.get("amount"):
        return f"Ofrece {offer.get('currency') or ''} {int(offer['amount']):,}{unit}".replace("  ", " ")
    return f"Pide rebaja{unit}, sin monto"


def criteria_lines(criteria, unit=None):
    """The five filters (qué, dónde, cuánto, cómo, cuándo) as short labelled lines."""
    criteria = criteria or {}
    que = [value for value in (criteria.get("operation"), criteria.get("property_type")) if value]
    if criteria.get("bedrooms") is not None:
        que.append(f"{criteria['bedrooms']} dorm.")
    if criteria.get("parking_required") is True:
        que.append("con cochera")
    if criteria.get("preferences"):
        que.append(", ".join(criteria["preferences"]))
    como = [PAYMENT_LABELS.get(criteria.get("payment_method"), criteria.get("payment_method"))]
    como.append(CREDIT_LABELS.get(criteria.get("credit_status"), criteria.get("credit_status")))
    como = [value for value in como if value]
    return [
        ("Qué", " · ".join(que) or NOT_GIVEN),
        ("Dónde", ", ".join(criteria.get("districts") or []) or NOT_GIVEN),
        ("Cuánto", _money(criteria, unit)),
        ("Cómo", " · ".join(como) or NOT_GIVEN),
        ("Cuándo", criteria.get("purchase_timeline") or NOT_GIVEN),
    ]


def advisor_text(client_phone, handoff, criteria, last_message, name=None, broker=False, now=None, unit=None):
    """Nota CRM consolidada (formato ARENZ) para el asesor que atiende el lead."""
    now = (now or datetime.now(timezone.utc)).astimezone(LIMA)
    situation, next_step = CRM_STEPS.get(handoff["request_type"], ("Pidió atención.", "Escribir hoy al cliente por WhatsApp."))
    lines = [f"📋 Nota CRM – {name or 'Nombre por confirmar'}",
             f"{now:%d/%m/%Y %H:%M} – {REQUEST_LABELS.get(handoff['request_type'], 'Pidió atención')} (bot de WhatsApp)",
             f"Cliente: +{client_phone} (https://wa.me/{client_phone})"]
    if broker:
        lines.append("Tipo de contacto: agente inmobiliario que busca para un cliente (colaboración comercial).")
    if handoff.get("property_reference"):
        details = unit if unit and unit.get("reference") == handoff["property_reference"] else None
        lines.append(f"Inmueble: {details['label'] if details else handoff['property_reference']}")
        if details and details.get("address"):
            lines.append(f"Dirección: {details['address']}")
    if last_message:
        lines.append(f"Último mensaje: “{last_message.strip()[:300]}”")
    lines += ["", f"Situación actual: {situation}", f"Próxima acción: {next_step}", "Canal: WhatsApp.",
              "Semáforo ARENZ: 🟢 Avanzar – interés comercial activo.", "", "Filtros comerciales ARENZ"]
    lines += [f"{label}: {value}" for label, value in criteria_lines(criteria, unit)]
    if not name:
        lines += ["", "Observación: confirmar el nombre del cliente."]
    return "\n".join(lines)


def _template_param(value):
    return _TEMPLATE_UNSAFE.sub(" ", str(value)).strip()[:500] or "-"


def template_payload(advisor_phone, template_name, language, client_phone, handoff, criteria, name=None, unit=None):
    """Template body parameters: {{1}} cliente, {{2}} solicitud, {{3}} inmueble, {{4}} criterios."""
    summary = " | ".join(f"{label}: {value}" for label, value in criteria_lines(criteria, unit))
    reference = handoff.get("property_reference")
    details = unit if unit and reference and unit.get("reference") == reference else None
    place = (details["label"] + (f" – {details['address']}" if details.get("address") else "")) if details else reference
    params = [f"{name} (+{client_phone})" if name else f"+{client_phone}", REQUEST_LABELS.get(handoff["request_type"], "Pidió atención"),
              place or "sin referencia", summary]
    return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": advisor_phone, "type": "template",
            "template": {"name": template_name, "language": {"code": language},
                         "components": [{"type": "body", "parameters": [{"type": "text", "text": _template_param(p)} for p in params]}]}}


def text_payload(advisor_phone, body):
    return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": advisor_phone,
            "type": "text", "text": {"body": body}}


def notify_advisors(post, client_phone, handoff, criteria, last_message, name=None, broker=False, unit=None):
    """Send the notice to every advisor; return how many accepted it. Never raises.

    `post` sends one Graph API messages payload and raises on rejection.
    """
    phones = [phone for phone in advisor_phones() if phone != client_phone]
    if not handoff or not phones:
        return 0
    template = os.getenv("ADVISOR_TEMPLATE", "").strip()
    language = os.getenv("ADVISOR_TEMPLATE_LANGUAGE", "es").strip() or "es"
    sent = 0
    for phone in phones:
        payload = (template_payload(phone, template, language, client_phone, handoff, criteria, name, unit) if template
                   else text_payload(phone, advisor_text(client_phone, handoff, criteria, last_message, name, broker, unit=unit)))
        try:
            post(payload)
            sent += 1
        except Exception as error:  # a notice must never break the client's turn
            logger.warning("Advisor notice failed: kind=%s template=%s category=%s",
                           handoff.get("request_type"), bool(template), type(error).__name__)
    if sent:
        logger.info("Advisor notified: kind=%s advisors=%s", handoff.get("request_type"), sent)
    return sent
