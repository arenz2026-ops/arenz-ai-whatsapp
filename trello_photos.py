"""Fotos de Trello → Supabase Storage, para que el bot las envíe por WhatsApp.

Las fotos del tablero "Intermediación Inmobiliaria" son adjuntos de Trello: solo se descargan con
TRELLO_KEY y TRELLO_TOKEN (cabecera OAuth) y muchas vienen en HEIC, que WhatsApp no muestra. Este
proceso las descarga, las convierte a JPG (máx. 1600 px), las sube al bucket público `inmuebles`
(`proyectos/<slug>/<slug>_trello_NN.jpg`) y las registra en `inventory_project_media`; al final llama a
`sync_property_media_from_projects()`, que las copia a cada unidad (`inventory_property_media`).

Es idempotente: cada foto ocupa un display_order (1..MAX_PHOTOS_PER_CARD) y las ya cargadas no se
vuelven a procesar, así que se puede repetir sin duplicar. Solo toca tarjetas vinculadas al inventario,
fuera de VENDIDO / DESCARTADO, y que no tengan fotos cargadas por otra vía (las curadas a mano se respetan).
"""
import io
import logging
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

TRELLO_API = "https://api.trello.com/1"
BUCKET = "inmuebles"
MAX_PHOTOS_PER_CARD = 15
MAX_SIDE = 1600
JPEG_QUALITY = 85
MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024
EXCLUDED_LISTS = {"VENDIDO", "DESCARTADO", "PLANTILLA DE CAPTACION"}
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp")
# Adjuntos que pueden ser documentos del propietario: nunca se envían a un cliente.
PRIVATE_NAMES = re.compile(r"dni|documento|partida|contrato|recibo|voucher|minuta|escritura|copia\s*literal|"
                           r"vigencia|ficha\s*registral|captura|screenshot", re.IGNORECASE)
STATUS_SUFFIX = re.compile(r"_(lanzamiento|preventa|construcci[oó]n|construci[oó]n|entrega.*)$", re.IGNORECASE)


def trello_credentials():
    key, token = os.getenv("TRELLO_KEY", "").strip(), os.getenv("TRELLO_TOKEN", "").strip()
    return (key, token) if key and token else None


def slugify(card_name):
    """'03_Casa Herrera_P.Libre' → 'casa-herrera-p-libre'; 'V017_SURCO SS04' → 'v017-surco-ss04'."""
    name = re.sub(r"^\d+_", "", (card_name or "").strip())
    name = STATUS_SUFFIX.sub("", name)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "tarjeta"


def is_client_photo(attachment):
    if not attachment.get("isUpload"):
        return False
    name = attachment.get("name") or attachment.get("fileName") or ""
    mime = (attachment.get("mimeType") or "").lower()
    if not (mime.startswith("image/") or name.lower().endswith(IMAGE_EXTENSIONS)):
        return False
    if (attachment.get("bytes") or 0) > MAX_ATTACHMENT_BYTES:
        return False
    return not PRIVATE_NAMES.search(name)


def to_jpeg(data):
    """Any image (HEIC included) → JPEG bytes, upright, at most MAX_SIDE px."""
    from PIL import Image, ImageOps
    from pillow_heif import register_heif_opener
    register_heif_opener()
    with Image.open(io.BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.thumbnail((MAX_SIDE, MAX_SIDE))
        out = io.BytesIO()
        image.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
        return out.getvalue()


class TrelloClient:
    def __init__(self, key, token, session=None):
        self.key, self.token = key, token
        self.http = session or requests.Session()

    def attachments(self, short_link):
        response = self.http.get(f"{TRELLO_API}/cards/{short_link}/attachments",
                                 params={"key": self.key, "token": self.token,
                                         "fields": "id,name,fileName,url,mimeType,isUpload,bytes"}, timeout=20)
        response.raise_for_status()
        return response.json()

    def download(self, url):
        # Desde 2021 Trello solo entrega adjuntos con la clave en la cabecera, no en la URL.
        auth = f'OAuth oauth_consumer_key="{self.key}", oauth_token="{self.token}"'
        response = self.http.get(url, headers={"Authorization": auth}, timeout=60)
        response.raise_for_status()
        return response.content


class MediaStore:
    def __init__(self, url, key, session=None):
        self.url = url.rstrip("/")
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self.http = session or requests.Session()

    def _get(self, table, params):
        response = self.http.get(f"{self.url}/rest/v1/{table}", headers=self.headers, params=params, timeout=20)
        response.raise_for_status()
        return response.json()

    def inventory_cards(self):
        """Tarjetas de Trello vinculadas a unidades del inventario, fuera de VENDIDO/DESCARTADO."""
        linked = {row["short_link"] for row in self._get("inventory_project_cards", {"select": "short_link"}) if row.get("short_link")}
        for row in self._get("inventory_unit_info", {"select": "trello_url", "trello_url": "not.is.null"}):
            match = re.search(r"trello\.com/c/([A-Za-z0-9]+)", row["trello_url"] or "")
            if match:
                linked.add(match.group(1))
        cards = self._get("inventory_trello_cards", {"select": "short_link,name,list_name"})
        return [c for c in cards if c["short_link"] in linked and (c.get("list_name") or "").upper() not in EXCLUDED_LISTS]

    def existing_media(self):
        """({short_link: {display_order, ...} | None}, {slug: short_link}) de lo ya cargado.

        None marca una tarjeta con fotos cargadas por otra vía (curadas): no se le agrega nada.
        """
        orders, slugs = {}, {}
        for row in self._get("inventory_project_media", {"select": "trello_short_link,project_slug,display_order,storage_path"}):
            link = row["trello_short_link"]
            slugs.setdefault(row["project_slug"], link)
            if not is_own_path(row.get("storage_path")):
                orders[link] = None
            elif orders.get(link, set()) is not None:
                orders.setdefault(link, set()).add(row["display_order"])
        return orders, slugs

    def upload(self, path, jpeg):
        response = self.http.post(f"{self.url}/storage/v1/object/{BUCKET}/{path}",
                                  headers={"apikey": self.headers["apikey"], "Authorization": self.headers["Authorization"],
                                           "Content-Type": "image/jpeg", "x-upsert": "true"}, data=jpeg, timeout=60)
        response.raise_for_status()
        return f"{self.url}/storage/v1/object/public/{BUCKET}/{path}"

    def add_media(self, row):
        response = self.http.post(f"{self.url}/rest/v1/inventory_project_media",
                                  headers={**self.headers, "Prefer": "return=minimal"}, json=row, timeout=20)
        response.raise_for_status()

    def sync_units(self):
        response = self.http.post(f"{self.url}/rest/v1/rpc/sync_property_media_from_projects", headers=self.headers, json={}, timeout=120)
        response.raise_for_status()
        return response.json()


def is_own_path(path):
    return bool(re.search(r"_trello_\d+\.jpg$", path or ""))


def card_slug(card, slugs):
    slug = slugify(card["name"])
    owner = slugs.get(slug)
    return slug if owner in (None, card["short_link"]) else f"{slug}-{card['short_link'].lower()}"


def run(trello, store, now=None, log=logger):
    """Carga las fotos que falten. Devuelve un resumen con contadores."""
    now = now or datetime.now(timezone.utc)
    summary = {"tarjetas": 0, "fotos_nuevas": 0, "errores": 0, "unidades_con_fotos": None}
    orders, slugs = store.existing_media()
    for card in store.inventory_cards():
        have = orders.get(card["short_link"], set())
        if have is None or len(have) >= MAX_PHOTOS_PER_CARD:
            continue
        try:
            photos = [a for a in trello.attachments(card["short_link"]) if is_client_photo(a)][:MAX_PHOTOS_PER_CARD]
        except requests.RequestException as error:
            log.warning("Trello attachments failed: card=%s category=%s", card["short_link"], type(error).__name__)
            summary["errores"] += 1
            continue
        pending = [(order, a) for order, a in enumerate(photos, start=1) if order not in have]
        if not pending:
            continue
        summary["tarjetas"] += 1
        slug = card_slug(card, slugs)
        slugs.setdefault(slug, card["short_link"])
        for order, attachment in pending:
            try:
                jpeg = to_jpeg(trello.download(attachment["url"]))
                path = f"proyectos/{slug}/{slug}_trello_{order:02d}.jpg"
                url = store.upload(path, jpeg)
                store.add_media({"media_url": url, "storage_path": path, "project_slug": slug,
                                 "trello_short_link": card["short_link"], "category": "foto", "display_order": order,
                                 "approved_for_client": True, "approved_at": now.isoformat()})
                summary["fotos_nuevas"] += 1
            except Exception as error:  # una foto dañada no detiene el resto
                log.warning("Trello photo skipped: card=%s order=%s category=%s", card["short_link"], order, type(error).__name__)
                summary["errores"] += 1
        log.info("Trello photos: card=%s slug=%s nuevas=%s", card["short_link"], slug, len(pending))
    if summary["fotos_nuevas"]:
        summary["unidades_con_fotos"] = store.sync_units()
    return summary


_lock = threading.Lock()
_state = {"status": "idle", "started_at": None, "finished_at": None, "result": None}


def state():
    return dict(_state)


def start_background(trello, store):
    """Lanza run() en un hilo (puede tardar minutos: gunicorn corta las peticiones a los 30 s)."""
    if not _lock.acquire(blocking=False):
        return False

    def worker():
        _state.update(status="running", started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), finished_at=None, result=None)
        try:
            _state["result"] = run(trello, store)
            _state["status"] = "done"
        except Exception as error:
            logger.warning("Trello photo sync failed: category=%s", type(error).__name__)
            _state.update(status="error", result={"error": type(error).__name__})
        finally:
            _state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _lock.release()

    threading.Thread(target=worker, name="trello-photos", daemon=True).start()
    return True
