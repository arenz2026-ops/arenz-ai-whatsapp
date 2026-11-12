"""Fotos HEIC de las tarjetas de Trello -> JPG en Supabase, para que el bot las envíe por WhatsApp.

La carga de fotos del 01/11 tomó solo los JPG de cada tarjeta y descartó los HEIC (las fotos tomadas con iPhone,
como las del departamento piloto de UNICO Magdalena), porque WhatsApp no acepta HEIC. Este módulo completa ese hueco:

1. Lee los adjuntos HEIC/HEIF de la tarjeta (API de Trello, TRELLO_KEY y TRELLO_TOKEN).
2. Los convierte a JPG (orientación corregida, máx. 1600 px, calidad 85) con pillow-heif.
3. Los sube al bucket "inmuebles" con el mismo formato de ruta que las fotos existentes.
4. Los registra en inventory_project_media, ordenados como en los demás proyectos:
   render -> piloto/foto -> general -> plano.
5. Ejecuta sync_property_media_from_projects() para que cada unidad del proyecto las tenga.

Es idempotente: un adjunto ya cargado (misma ruta) no se vuelve a subir.
"""
import io
import logging
import os
import re
import unicodedata

import requests

logger = logging.getLogger("arenz")

TRELLO_API = "https://api.trello.com/1"
BUCKET = "inmuebles"
HEIC_TYPES = {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence"}
HEIC_EXTENSIONS = (".heic", ".heif")
MAX_SIDE = 1600
JPEG_QUALITY = 85
MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024
# Orden en que el bot envía las fotos (igual que en Libertador, Leyendas II y Salaverry District).
CATEGORY_ORDER = ("render", "piloto", "foto", "general", "plano")


def is_heic(attachment):
    name = (attachment.get("name") or "").casefold()
    return bool(attachment.get("isUpload")) and (
        (attachment.get("mimeType") or "").casefold() in HEIC_TYPES or name.endswith(HEIC_EXTENSIONS))


def slugify(text):
    """'01_Unico Magdalena' -> 'unico-magdalena' (sin el número de orden de la tarjeta)."""
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().casefold()
    plain = re.sub(r"^\s*\d+\s*[_.\-)]\s*", "", plain)
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")


def category_for(list_name):
    """En tarjetas de proyectos en construcción, una foto real es del departamento piloto; si no, de la unidad."""
    return "piloto" if "PROYECTO" in (list_name or "").upper() else "foto"


def heic_to_jpeg(data):
    """Bytes HEIC/HEIF -> bytes JPEG listos para WhatsApp. Lanza ValueError si el archivo no es una imagen válida."""
    import pillow_heif
    from PIL import Image, ImageOps

    pillow_heif.register_heif_opener()
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            image = image.convert("RGB")
            image.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            image.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
            return out.getvalue()
    except Exception as error:  # Pillow lanza varios tipos según el archivo
        raise ValueError(f"imagen no válida: {type(error).__name__}") from error


class TrelloPhotos:
    def __init__(self, supabase_url, supabase_key, trello_key, trello_token):
        self.url = supabase_url.rstrip("/")
        self.headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}
        self.trello_auth = {"key": trello_key, "token": trello_token}
        self.trello_header = {"Authorization": f'OAuth oauth_consumer_key="{trello_key}", oauth_token="{trello_token}"'}

    # ---- Trello
    def attachments(self, short_link):
        response = requests.get(f"{TRELLO_API}/cards/{short_link}/attachments", timeout=20,
                                params={**self.trello_auth, "fields": "id,name,mimeType,url,isUpload,bytes"})
        response.raise_for_status()
        return response.json()

    def download(self, attachment):
        if (attachment.get("bytes") or 0) > MAX_DOWNLOAD_BYTES:
            raise ValueError("archivo demasiado grande")
        response = requests.get(attachment["url"], headers=self.trello_header, timeout=60)
        response.raise_for_status()
        return response.content

    # ---- Supabase
    def card(self, short_link):
        response = requests.get(f"{self.url}/rest/v1/inventory_trello_cards", headers=self.headers, timeout=10,
                                params={"select": "short_link,name,list_name", "short_link": f"eq.{short_link}"})
        response.raise_for_status()
        rows = response.json()
        return rows[0] if rows else {}

    def media(self, short_link):
        response = requests.get(f"{self.url}/rest/v1/inventory_project_media", headers=self.headers, timeout=10,
                                params={"select": "storage_path,media_url,project_slug,category,display_order",
                                        "trello_short_link": f"eq.{short_link}", "order": "display_order.asc"})
        response.raise_for_status()
        return response.json()

    def project_cards(self):
        response = requests.get(f"{self.url}/rest/v1/inventory_project_media_keys", headers=self.headers, timeout=10,
                                params={"select": "trello_short_link"})
        response.raise_for_status()
        return sorted({row["trello_short_link"] for row in response.json() if row.get("trello_short_link")})

    def upload(self, path, jpeg):
        response = requests.post(f"{self.url}/storage/v1/object/{BUCKET}/{path}", timeout=60, data=jpeg,
                                 headers={**self.headers, "Content-Type": "image/jpeg", "x-upsert": "true"})
        response.raise_for_status()
        return f"{self.url}/storage/v1/object/public/{BUCKET}/{path}"

    def insert_media(self, row):
        response = requests.post(f"{self.url}/rest/v1/inventory_project_media", timeout=10, json=[row],
                                 headers={**self.headers, "Content-Type": "application/json",
                                          "Prefer": "resolution=ignore-duplicates,return=minimal"})
        response.raise_for_status()

    def set_order(self, storage_path, order):
        response = requests.patch(f"{self.url}/rest/v1/inventory_project_media", timeout=10,
                                  params={"storage_path": f"eq.{storage_path}"}, json={"display_order": order},
                                  headers={**self.headers, "Content-Type": "application/json", "Prefer": "return=minimal"})
        response.raise_for_status()

    def sync_units(self):
        response = requests.post(f"{self.url}/rest/v1/rpc/sync_property_media_from_projects", timeout=60, json={},
                                 headers={**self.headers, "Content-Type": "application/json"})
        response.raise_for_status()
        return response.json()


def reorder(existing):
    """display_order final: render -> piloto -> foto -> general -> plano; dentro de cada grupo, el orden que ya tenían."""
    rank = {category: index for index, category in enumerate(CATEGORY_ORDER)}
    ordered = sorted(existing, key=lambda row: (rank.get(row.get("category"), len(rank)), row.get("display_order") or 0,
                                                row.get("storage_path") or ""))
    return {row["storage_path"]: index for index, row in enumerate(ordered, 1)}


def load_card(client, short_link, approve=True):
    """Carga los HEIC de una tarjeta. Devuelve un resumen sin datos sensibles."""
    summary = {"tarjeta": short_link, "heic": 0, "cargadas": [], "ya_estaban": 0, "errores": []}
    existing = client.media(short_link)
    card = client.card(short_link)
    slug = next((row["project_slug"] for row in existing if row.get("project_slug")), None) or slugify(card.get("name"))
    if not slug:
        summary["errores"].append("tarjeta sin nombre ni fotos previas")
        return summary
    category = category_for(card.get("list_name"))
    known = {row["storage_path"] for row in existing}
    for attachment in client.attachments(short_link):
        if not is_heic(attachment):
            continue
        summary["heic"] += 1
        path = f"proyectos/{slug}/{slug}_{category}_heic_{attachment['id'][-10:]}.jpg"
        if path in known:
            summary["ya_estaban"] += 1
            continue
        try:
            url = client.upload(path, heic_to_jpeg(client.download(attachment)))
            row = {"media_url": url, "storage_path": path, "project_slug": slug, "trello_short_link": short_link,
                   "category": category, "display_order": 1000 + len(existing), "approved_for_client": approve}
            if approve:
                row["approved_at"] = _now()
            client.insert_media(row)
            existing.append(row)
            known.add(path)
            summary["cargadas"].append(url)
        except (requests.RequestException, ValueError) as error:
            summary["errores"].append(f"{attachment.get('name')}: {type(error).__name__}: {error}"[:200])
    if summary["cargadas"]:
        for path, order in reorder(existing).items():
            current = next(row.get("display_order") for row in existing if row["storage_path"] == path)
            if current != order:
                client.set_order(path, order)
    return summary


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def run(client, short_links=None, approve=True):
    """Procesa las tarjetas indicadas (o todas las de proyectos) y sincroniza las fotos de las unidades."""
    cards = short_links or client.project_cards()
    results = []
    for short_link in cards:
        try:
            results.append(load_card(client, short_link, approve))
        except requests.RequestException as error:
            results.append({"tarjeta": short_link, "heic": 0, "cargadas": [], "ya_estaban": 0,
                            "errores": [f"{type(error).__name__}"]})
    synced = client.sync_units() if any(result["cargadas"] for result in results) else None
    return {"tarjetas": len(results), "heic": sum(r["heic"] for r in results),
            "cargadas": sum(len(r["cargadas"]) for r in results), "ya_estaban": sum(r["ya_estaban"] for r in results),
            "fotos_por_unidad_sincronizadas": synced,
            "detalle": [r for r in results if r["heic"] or r["errores"]]}


def from_env():
    values = [os.getenv(name, "").strip() for name in ("SUPABASE_URL", "SUPABASE_KEY", "TRELLO_KEY", "TRELLO_TOKEN")]
    return TrelloPhotos(*values) if all(values) else None
