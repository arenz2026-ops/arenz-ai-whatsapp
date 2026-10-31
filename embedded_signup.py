"""Meta Embedded Signup for WhatsApp Business app coexistence.

Lets the owner connect the existing WhatsApp Business app number to Cloud API
from /conectar-whatsapp while the phone keeps working. The callback exchanges
the one-time code for a business token, refuses any WABA not owned by the
allowed business portfolio, subscribes this app to the WABA's webhooks and
stores the connection in Supabase, where the bot reads it on every send.
"""
import html
import logging
import os
import time
from datetime import datetime, timezone

import requests
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
bp = Blueprint("embedded_signup", __name__)

GRAPH = "https://graph.facebook.com/v26.0"
META_APP_ID = os.getenv("META_APP_ID", "1016931507632124")
ES_CONFIG_ID = os.getenv("ES_CONFIG_ID", "1878514219986650")
# Only a WABA owned by this business portfolio (Arenz) may take over the bot.
ALLOWED_BUSINESS_ID = os.getenv("ALLOWED_BUSINESS_ID", "4420949641567478")
TABLE = "whatsapp_connection"
# "at" is None until the first lookup. A 0.0 sentinel looked fresh whenever
# time.monotonic() was still under CACHE_SECONDS, i.e. just after a container boot.
_CACHE = {"at": None, "row": None}
CACHE_SECONDS = 60


def _supabase():
    url, key = os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")
    if not (url and key):
        return None, None
    return url.rstrip("/"), {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def stored_connection():
    """Return the active connection row from Supabase (cached), or None."""
    if _CACHE["at"] is not None and time.monotonic() - _CACHE["at"] < CACHE_SECONDS:
        return _CACHE["row"]
    url, headers = _supabase()
    row = None
    if url:
        try:
            response = requests.get(f"{url}/rest/v1/{TABLE}", headers=headers,
                                    params={"id": "eq.1", "select": "phone_number_id,access_token,waba_id"}, timeout=10)
            response.raise_for_status()
            rows = response.json()
            row = rows[0] if rows and rows[0].get("access_token") and rows[0].get("phone_number_id") else None
        except (requests.RequestException, ValueError):
            logger.warning("Stored WhatsApp connection unavailable; using environment credentials")
    _CACHE.update(at=time.monotonic(), row=row)
    return row


def active_credentials(env_token, env_phone_id):
    """Credentials from Embedded Signup win; environment variables are the fallback."""
    row = stored_connection()
    if row:
        return row["access_token"], row["phone_number_id"]
    return env_token, env_phone_id


def _graph_error(response):
    try:
        return response.json().get("error", {}).get("message", "")
    except ValueError:
        return ""


def _granted_waba(token, app_secret):
    """WABA the user granted, read from the token's granular scopes (v5 may omit it)."""
    debug = requests.get(f"{GRAPH}/debug_token", params={
        "input_token": token, "access_token": f"{META_APP_ID}|{app_secret}"}, timeout=20)
    scopes = debug.json().get("data", {}).get("granular_scopes", []) if debug.ok else []
    for scope in scopes:
        if scope.get("scope") == "whatsapp_business_management" and scope.get("target_ids"):
            return scope["target_ids"][0]
    return ""


def complete_signup(code, waba_id, phone_number_id, app_secret):
    """Run the server half of Embedded Signup. Returns (ok, message)."""
    if not code:
        return False, "Meta no devolvió el código de autorización."
    if not app_secret:
        return False, "APP_SECRET no está configurado en el servidor."
    response = requests.get(f"{GRAPH}/oauth/access_token", params={
        "client_id": META_APP_ID, "client_secret": app_secret, "code": code}, timeout=20)
    if not response.ok:
        return False, f"Meta rechazó el código: {_graph_error(response)}"
    token = response.json().get("access_token")
    if not token:
        return False, "Meta no devolvió un token."
    auth = {"Authorization": f"Bearer {token}"}
    if not waba_id:
        waba_id = _granted_waba(token, app_secret)
    if waba_id and not phone_number_id:
        numbers = requests.get(f"{GRAPH}/{waba_id}/phone_numbers", headers=auth, params={"fields": "id"}, timeout=20)
        rows = numbers.json().get("data", []) if numbers.ok else []
        phone_number_id = rows[0]["id"] if len(rows) == 1 else ""
    if not (waba_id and phone_number_id):
        return False, "No se pudo identificar la cuenta o el número conectado."
    owner = requests.get(f"{GRAPH}/{waba_id}", headers=auth, params={"fields": "owner_business_info"}, timeout=20)
    owner_id = (owner.json().get("owner_business_info") or {}).get("id") if owner.ok else None
    if str(owner_id) != str(ALLOWED_BUSINESS_ID):
        logger.warning("Embedded Signup refused: WABA %s owned by %s", waba_id, owner_id)
        return False, "Esta cuenta de WhatsApp no pertenece al portafolio autorizado (Arenz)."
    subscribe = requests.post(f"{GRAPH}/{waba_id}/subscribed_apps", headers=auth, timeout=20)
    if not subscribe.ok:
        return False, f"No se pudo suscribir la app a los webhooks: {_graph_error(subscribe)}"
    url, headers = _supabase()
    if not url:
        return False, "Supabase no está configurado; no hay dónde guardar la conexión."
    saved = requests.post(f"{url}/rest/v1/{TABLE}?on_conflict=id",
                          headers={**headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
                          json={"id": 1, "waba_id": waba_id, "phone_number_id": phone_number_id,
                                "access_token": token, "business_id": ALLOWED_BUSINESS_ID,
                                "connected_at": datetime.now(timezone.utc).isoformat()}, timeout=10)
    if not saved.ok:
        return False, "No se pudo guardar la conexión en Supabase."
    _CACHE.update(at=None, row=None)
    logger.info("Embedded Signup completed: waba=%s phone_number_id=%s", waba_id, phone_number_id)
    return True, "Número conectado. El bot ya responde desde este número."


PAGE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Conectar WhatsApp | Arenz</title>
<style>body{font-family:system-ui,sans-serif;max-width:560px;margin:48px auto;padding:0 16px;color:#1c1c1c}
button{background:#1877f2;color:#fff;border:0;border-radius:6px;padding:12px 20px;font-size:16px;cursor:pointer}
#estado{margin-top:24px;padding:12px;border-radius:6px;background:#f2f2f2;white-space:pre-wrap}</style></head>
<body><h1>Conectar el WhatsApp de Arenz</h1>
<p>1. Pulsa el botón e inicia sesión con tu Facebook.<br>2. Elige <b>Conectar tu app de WhatsApp Business existente</b> y el portafolio <b>Arenz</b>.<br>
3. Confirma desde la app WhatsApp Business del celular (código QR o aviso).</p>
<button id="conectar" onclick="conectar()">Conectar con Facebook</button><div id="estado">Listo para empezar.</div>
<script>
const estado = t => document.getElementById("estado").textContent = t;
let sesion = {};
window.addEventListener("message", e => {
  if (!e.origin.endsWith("facebook.com")) return;
  try { const d = JSON.parse(e.data);
    if (d.type === "WA_EMBEDDED_SIGNUP") {
      if (String(d.event || "").startsWith("FINISH")) { const x = d.data || {};
        sesion = {waba_id: x.waba_id || (x.waba || {}).id, phone_number_id: x.phone_number_id || (x.phone_number || {}).id}; }
      else if (d.event === "CANCEL") estado("Proceso cancelado en el paso: " + ((d.data||{}).current_step||"desconocido"));
      else if (d.event === "ERROR") estado("Error de Meta: " + ((d.data||{}).error_message||""));
    } } catch (_) {}
});
window.fbAsyncInit = () => FB.init({appId: "__APP_ID__", autoLogAppEvents: true, xfbml: false, version: "v26.0"});
function conectar() {
  estado("Abriendo la ventana de Meta…");
  FB.login(r => {
    const code = r.authResponse && r.authResponse.code;
    if (!code) { estado("No se completó el inicio de sesión."); return; }
    estado("Registrando la conexión…");
    setTimeout(() => fetch("/conectar-whatsapp/callback", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({code, waba_id: sesion.waba_id, phone_number_id: sesion.phone_number_id})})
      .then(x => x.json()).then(j => estado(j.message)).catch(() => estado("No se pudo contactar al servidor.")), 1500);
  }, {config_id: "__CONFIG_ID__", response_type: "code", override_default_response_type: true,
      extras: {version: "v5"}});
}
</script><script async defer crossorigin="anonymous" src="https://connect.facebook.net/es_LA/sdk.js"></script>
</body></html>"""


@bp.route("/conectar-whatsapp", methods=["GET"])
def connect_page():
    page = PAGE.replace("__APP_ID__", html.escape(META_APP_ID)).replace("__CONFIG_ID__", html.escape(ES_CONFIG_ID))
    return page, 200, {"Content-Type": "text/html; charset=utf-8"}


@bp.route("/conectar-whatsapp/callback", methods=["POST"])
def connect_callback():
    data = request.get_json(silent=True) or {}
    try:
        ok, message = complete_signup(str(data.get("code") or ""), str(data.get("waba_id") or ""),
                                      str(data.get("phone_number_id") or ""), os.getenv("APP_SECRET", ""))
    except requests.RequestException:
        logger.exception("Embedded Signup network failure")
        ok, message = False, "Fallo de red al hablar con Meta o Supabase. Intenta de nuevo."
    return jsonify({"ok": ok, "message": message}), 200 if ok else 400


def activate_owned_number(waba_id, phone_number_id, token):
    """Register a number of our own WABA on Cloud API and make the bot send from it.

    Uses the server's WHATSAPP_TOKEN, never one from the request. Only numbers of a
    WABA owned by ALLOWED_BUSINESS_ID are accepted. The two-step PIN is generated
    here; once set, Meta refuses re-registration without it, so repeat calls fail.
    """
    import secrets
    if not (waba_id and phone_number_id):
        return False, "Faltan waba_id o phone_number_id.", None
    if not token:
        return False, "WHATSAPP_TOKEN no está configurado.", None
    auth = {"Authorization": f"Bearer {token}"}
    owner = requests.get(f"{GRAPH}/{waba_id}", headers=auth, params={"fields": "owner_business_info"}, timeout=20)
    if not owner.ok:
        return False, f"El token del servidor no accede a la cuenta: {_graph_error(owner)}", None
    if str((owner.json().get("owner_business_info") or {}).get("id")) != str(ALLOWED_BUSINESS_ID):
        return False, "La cuenta no pertenece al portafolio autorizado.", None
    numbers = requests.get(f"{GRAPH}/{waba_id}/phone_numbers", headers=auth, params={"fields": "id"}, timeout=20)
    if not numbers.ok or phone_number_id not in [n.get("id") for n in numbers.json().get("data", [])]:
        return False, "El número no pertenece a esa cuenta.", None
    pin = f"{secrets.randbelow(10**6):06d}"
    register = requests.post(f"{GRAPH}/{phone_number_id}/register", headers=auth,
                             json={"messaging_product": "whatsapp", "pin": pin}, timeout=30)
    if not register.ok:
        return False, f"Meta rechazó el registro: {_graph_error(register)}", None
    subscribe = requests.post(f"{GRAPH}/{waba_id}/subscribed_apps", headers=auth, timeout=20)
    if not subscribe.ok:
        return False, f"Registrado, pero falló la suscripción a webhooks: {_graph_error(subscribe)}", pin
    url, headers = _supabase()
    if not url:
        return False, "Registrado, pero Supabase no está configurado.", pin
    saved = requests.post(f"{url}/rest/v1/{TABLE}?on_conflict=id",
                          headers={**headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
                          json={"id": 1, "waba_id": waba_id, "phone_number_id": phone_number_id,
                                "access_token": token, "business_id": ALLOWED_BUSINESS_ID,
                                "connected_at": datetime.now(timezone.utc).isoformat()}, timeout=10)
    if not saved.ok:
        return False, "Registrado, pero no se pudo guardar en Supabase.", pin
    _CACHE.update(at=None, row=None)
    logger.info("Owned number activated: waba=%s phone_number_id=%s", waba_id, phone_number_id)
    return True, "Número registrado y activo en el bot.", pin


@bp.route("/admin/activar-numero", methods=["POST"])
def activate_number():
    data = request.get_json(silent=True) or {}
    try:
        ok, message, pin = activate_owned_number(str(data.get("waba_id") or ""), str(data.get("phone_number_id") or ""),
                                                 os.getenv("WHATSAPP_TOKEN", ""))
    except requests.RequestException:
        logger.exception("Number activation network failure")
        ok, message, pin = False, "Fallo de red con Meta o Supabase.", None
    body = {"ok": ok, "message": message}
    if pin:
        body["pin"] = pin
    return jsonify(body), 200 if ok else 400


@bp.route("/admin/diagnostico-token", methods=["GET"])
def token_diagnostics():
    """Report what the server token can reach: scopes and asset IDs only, never the token."""
    token, app_secret = os.getenv("WHATSAPP_TOKEN", ""), os.getenv("APP_SECRET", "")
    if not (token and app_secret):
        return jsonify({"ok": False, "message": "Falta WHATSAPP_TOKEN o APP_SECRET."}), 400
    try:
        debug = requests.get(f"{GRAPH}/debug_token", params={
            "input_token": token, "access_token": f"{META_APP_ID}|{app_secret}"}, timeout=20)
        body = debug.json()
        data = body.get("data", {}) if debug.ok else {}
        if not debug.ok:
            data = {"error": body.get("error", {})}
    except (requests.RequestException, ValueError):
        return jsonify({"ok": False, "message": "No se pudo consultar a Meta."}), 502
    return jsonify({"ok": bool(data), "app_id": data.get("app_id"), "type": data.get("type"),
                    "is_valid": data.get("is_valid"), "expires_at": data.get("expires_at"),
                    "scopes": data.get("scopes"),
                    "granular_scopes": data.get("granular_scopes"),
                    "error": (data.get("error") or {}).get("message")})
