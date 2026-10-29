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
_CACHE = {"at": 0.0, "row": None}
CACHE_SECONDS = 60


def _supabase():
    url, key = os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")
    if not (url and key):
        return None, None
    return url.rstrip("/"), {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def stored_connection():
    """Return the active connection row from Supabase (cached), or None."""
    if time.monotonic() - _CACHE["at"] < CACHE_SECONDS:
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


def complete_signup(code, waba_id, phone_number_id, app_secret):
    """Run the server half of Embedded Signup. Returns (ok, message)."""
    if not (code and waba_id and phone_number_id):
        return False, "Faltan datos del registro (code, waba_id o phone_number_id)."
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
    _CACHE.update(at=0.0, row=None)
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
      if (d.event === "FINISH" || d.event === "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING") sesion = d.data || {};
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
      extras: {setup: {}, featureType: "whatsapp_business_app_onboarding", sessionInfoVersion: "3"}});
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
