"""Decide which property photos go to a client on a turn, and send each one once.

The inventory already carries approved photos (inventory_property_media rows with
approved_for_client=true reach the public snapshot); until now the bot only pasted
the first URL as text. This module turns them into WhatsApp image messages:

- options presented  -> the main photo of each option (at most 3 options)
- one unit selected  -> up to 5 photos of that unit
- "fotos" requested  -> up to 5 photos of the unit the client means

A photo already sent to the same client in the same search is not sent again
(media_deliveries in Supabase; in-process fallback without it). Sending is best
effort: a failed photo is logged and the text reply still goes out.
"""
import logging
import re
from datetime import datetime, timezone

import requests

logger = logging.getLogger("arenz.media")

MAX_OPTIONS_WITH_PHOTO = 3
MAX_UNIT_PHOTOS = 5
IMAGE_TYPES = {"image", "imagen", "foto", "photo", "fotografia", "fotografía"}
PHOTO_REQUEST_PATTERN = re.compile(r"\bfotos?\b|\bfotograf[ií]as?\b|\bim[aá]gen(?:es)?\b")
REFERENCE_PATTERN = re.compile(r"\bref\.?\s*([a-z0-9][a-z0-9-]{1,23})\b", re.IGNORECASE)


def is_photo_request(text):
    return bool(PHOTO_REQUEST_PATTERN.search((text or "").casefold()))


def approved_images(public):
    """Only approved, image-typed, HTTPS media: WhatsApp fetches the link itself."""
    images = []
    for media in (public or {}).get("media") or []:
        kind = str(media.get("media_type") or "").casefold()
        url = str(media.get("media_url") or "")
        if kind in IMAGE_TYPES and url.startswith("https://") and url not in images:
            images.append(url)
    return images


def price_label(public):
    amount = f"{float(public['price_amount']):,.2f}".rstrip("0").rstrip(".")
    amount = amount.translate(str.maketrans({",": ".", ".": ","}))
    symbol = {"PEN": "S/", "USD": "US$"}.get(public.get("currency"), public.get("currency") or "")
    return f"{symbol} {amount}".strip()


def caption_for(public, option_number=None, photo_number=None, photo_total=None):
    """Customer-facing caption. References stay out: the bot keeps them internal."""
    parts = [f"Opción {option_number}"] if option_number else []
    parts.append(str(public.get("property_type") or "Inmueble").capitalize())
    if public.get("district"):
        parts.append(public["district"])
    if public.get("price_amount") is not None:
        parts.append(price_label(public))
    if public.get("bedrooms") is not None:
        parts.append(f"{public['bedrooms']:g} dorm.")
    if photo_number and photo_total and photo_total > 1:
        parts.append(f"foto {photo_number}/{photo_total}")
    return " · ".join(parts)


def references_in(text):
    return list(dict.fromkeys(REFERENCE_PATTERN.findall(text or "")))


def _unit_photos(item):
    public = item["public"]
    urls = approved_images(public)[:MAX_UNIT_PHOTOS]
    return [{"reference": public["public_reference"], "url": url,
             "caption": caption_for(public, photo_number=index, photo_total=len(urls))}
            for index, url in enumerate(urls, 1)]


def plan_photos(reply, stage, inventory_matches, user_text=None, requested_reference=None):
    """Return (photos, requested_item) for this turn.

    `requested_item` is the unit a photo request resolved to (None otherwise), so the
    caller can answer that request in text even when the unit has no photos.
    """
    by_reference = {item["public"]["public_reference"]: item for item in (inventory_matches or [])
                    if isinstance(item.get("public"), dict) and item["public"].get("public_reference")}
    if not by_reference:
        return [], None
    if requested_reference and is_photo_request(user_text) and requested_reference in by_reference:
        item = by_reference[requested_reference]
        return _unit_photos(item), item
    shown = [reference for reference in references_in(reply) if reference in by_reference]
    if stage == "property_detail" and len(shown) == 1:
        return _unit_photos(by_reference[shown[0]]), None
    photos = []
    for number, reference in enumerate(shown[:MAX_OPTIONS_WITH_PHOTO], 1):
        public = by_reference[reference]["public"]
        images = approved_images(public)
        if images:
            photos.append({"reference": reference, "url": images[0], "caption": caption_for(public, option_number=number)})
    return photos, None


def photo_request_reply(item, has_photos):
    reference = item["public"]["public_reference"]
    if has_photos:
        return f"Ref. {reference}\nTe comparto las fotos de esta unidad. ¿Qué más te gustaría saber de ella?"
    return (f"Ref. {reference}\nPor ahora no tengo fotos aprobadas de esta unidad para compartir. "
            "Puedo pedirle a un asesor que te las envíe. ¿Te parece bien?")


class MediaDeliveryLog:
    """Which photos each client already received in a search (Supabase or in-process)."""

    def __init__(self, url=None, key=None):
        self.url = (url or "").rstrip("/")
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"} if key else {}
        self.memory = {}

    def already_sent(self, phone, search_key, urls):
        if not self.url:
            return {url for url in urls if (phone, search_key, url) in self.memory}
        try:
            response = requests.get(f"{self.url}/rest/v1/media_deliveries", headers=self.headers, timeout=10,
                                    params={"select": "media_url", "phone": f"eq.{phone}", "search_key": f"eq.{search_key}"})
            response.raise_for_status()
            return {row["media_url"] for row in response.json()} & set(urls)
        except (requests.RequestException, ValueError, KeyError, TypeError):
            logger.warning("Media delivery log unavailable; photos may repeat")
            return set()

    def record(self, phone, search_key, url):
        if not self.url:
            self.memory[(phone, search_key, url)] = datetime.now(timezone.utc)
            return
        try:
            response = requests.post(f"{self.url}/rest/v1/media_deliveries?on_conflict=phone,search_key,media_url", timeout=10,
                                     headers={**self.headers, "Prefer": "resolution=ignore-duplicates,return=minimal"},
                                     json={"phone": phone, "search_key": search_key, "media_url": url})
            response.raise_for_status()
        except requests.RequestException:
            logger.warning("Unable to record media delivery")


def send_photos(send_image, delivery_log, phone, search_key, photos):
    """Send each photo not sent before in this search; return how many went out. Never raises."""
    if not photos:
        return 0
    search_key = search_key or "sin-busqueda"
    try:
        skip = delivery_log.already_sent(phone, search_key, [photo["url"] for photo in photos])
    except Exception:
        logger.exception("Media delivery check failed"); skip = set()
    sent = 0
    for photo in photos:
        if photo["url"] in skip:
            continue
        try:
            send_image(phone, photo["url"], photo["caption"])
            delivery_log.record(phone, search_key, photo["url"])
            sent += 1
        except Exception as error:  # a photo must never block the text reply
            logger.warning("Photo delivery failed: category=%s", type(error).__name__)
    if sent:
        logger.info("Photos delivered: count=%s", sent)
    return sent
