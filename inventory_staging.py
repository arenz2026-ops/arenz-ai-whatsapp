"""Receive raw rows of a partner price list into inventory_staging.

The bot's operator reads C2's price lists in a browser (Looker Studio, Power BI)
and posts the rows here so they can be compared with the live inventory in SQL
before anything visible changes. Nothing here touches inventory_properties.

A post is accepted only with a one-time token present and unexpired in
inventory_import_tokens, a table only the service key can read or write. The
body is sent as text/plain so the browser does not need a CORS preflight.
"""
import json
import logging
import os
from datetime import datetime, timezone

import requests
from flask import Blueprint, jsonify, request

logger = logging.getLogger("arenz.staging")
bp = Blueprint("inventory_staging", __name__)
SOURCES = {"lista_precios", "power_bi_tale", "trello"}
MAX_ROWS = 1000


def _supabase():
    url, key = os.getenv("SUPABASE_URL", "").rstrip("/"), os.getenv("SUPABASE_KEY", "")
    if not (url and key):
        return None, None
    return url, {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def token_is_valid(url, headers, token):
    if not token or len(token) < 24:
        return False
    now = datetime.now(timezone.utc).isoformat()
    r = requests.get(f"{url}/rest/v1/inventory_import_tokens", headers=headers,
                     params={"token": f"eq.{token}", "expires_at": f"gt.{now}", "select": "token"}, timeout=10)
    return r.ok and bool(r.json())


@bp.route("/admin/inventario-staging", methods=["POST"])
def receive_rows():
    cors = {"Access-Control-Allow-Origin": "*"}
    try:
        body = json.loads(request.get_data(as_text=True) or "{}")
    except ValueError:
        return jsonify({"ok": False, "message": "JSON inválido"}), 400, cors
    source, rows = body.get("source"), body.get("rows")
    if source not in SOURCES or not isinstance(rows, list) or not 0 < len(rows) <= MAX_ROWS:
        return jsonify({"ok": False, "message": "source o rows inválidos"}), 400, cors
    url, headers = _supabase()
    if not url:
        return jsonify({"ok": False, "message": "Supabase no configurado"}), 503, cors
    try:
        if not token_is_valid(url, headers, str(body.get("token") or "")):
            return jsonify({"ok": False, "message": "token inválido"}), 403, cors
        payload = [{"source": source, "r": row} for row in rows if isinstance(row, (dict, list))]
        saved = requests.post(f"{url}/rest/v1/inventory_staging", headers={**headers, "Prefer": "return=minimal"},
                              json=payload, timeout=30)
    except requests.RequestException:
        logger.exception("Staging upload failed")
        return jsonify({"ok": False, "message": "fallo de red"}), 502, cors
    if not saved.ok:
        return jsonify({"ok": False, "message": f"Supabase {saved.status_code}"}), 502, cors
    logger.info("Staged %s rows from %s", len(payload), source)
    return jsonify({"ok": True, "staged": len(payload)}), 200, cors
