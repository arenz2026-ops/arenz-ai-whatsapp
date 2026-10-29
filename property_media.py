"""Decide which property photos go to a client on a turn, and send each one once.

The inventory already carries approved photos (inventory_property_media rows with
approved_for_client=true reach the public snapshot); until now the bot only pasted
the first URL as text. This module turns them into WhatsApp image messages:

- options presented  -> the main photo of each option (at most 3 options)
- one unit selected  -> up to 5 photos of that unit
- "fotos" requested  -> up to 5 photos of the unit the client means

A photo already sent to the same client in the same search is not sent again
(media_deliveries in Supabase; in-process fallback without it), so asking again
brings the next ones. Sending is best effort: a failed photo is logged and the
text reply still goes out.
"""
import logging
import re
from datetime import datetime, timezone

import requests

logger = logging.getLogger("arenz.media")

MAX_OPTIONS_WITH_PHOTO = 3
MAX_UNIT_PHOTOS = 5
NO_SEARCH = "sin-busqueda"
# inventory_property_media.media_type only accepts 'photo' (or 'link'); the rest are
# tolerated spellings so a hand-loaded row is not silently ignored.
IMAGE_TYPES = {"photo", "image", "imagen", "foto", "fotografia", "fotografía"}
PHOTO_REQUEST_PATTERN = re.compile(r"\bfotos?\b|\bfotograf[ií]as?\b|\bim[aá]gen(?:es)?\b")
# "no me mandes fotos" mentions photos without asking for them; "¿no tienes fotos?" still asks.
PHOTO_REFUSAL_PATTERN = re.compile(r"\bno\s+(?:me\s+)?(?:mand|env[ií]|quier|necesit|hace(?:n)? falta)")
REFERENCE_PATTERN = re.compile(r"\bref\.?\s*([a-z0-9][a-z0-9-]{1,23})\b", re.IGNORECASE)
OPTION_NUMBER_PATTERN = re.compile(r"\b(?:opci[oó]n|la|el|n[uú]mero|nro\.?)\s*#?\s*([1-9])\b")
OPTION_ORDINALS = {"primera": 1, "primero": 1, "segunda": 2, "segundo": 2, "tercera": 3, "tercero": 3}
# Both "no photos to send" replies offer an advisor in these words; a yes to them is a handoff.
PHOTO_OFFER_PATTERN = re.compile(r"pedirle a un asesor que te (?:las )?env[ií]e")
YES_WORDS = {"sí", "ok", "okay", "oka", "dale", "claro", "perfecto", "listo", "porfa", "excelente", "genial"}
YES_PHRASES = ("de acuerdo", "por favor", "me parece bien", "está bien", "esta bien")
# 'si' is also the conditional "if", and 'ya'/'bueno'/'va' open other sentences too:
# they count as a yes alone or followed by a courtesy, not as "si tienes otra…".
AMBIGUOUS_YES = {"si", "ya", "bueno", "va"}
COURTESY_WORDS = {"por", "porfa", "porfavor", "claro", "gracias", "dale", "ok", "perfecto", "de", "que", "ya", "bueno", "listo"}


def is_photo_request(text):
    normalized = (text or "").casefold()
    return bool(PHOTO_REQUEST_PATTERN.search(normalized)) and not PHOTO_REFUSAL_PATTERN.search(normalized)


def is_affirmative(text):
    """A plain yes ('sí', 'dale', 'ok, gracias', 'si por favor'), never one containing 'no'."""
    words = re.findall(r"[a-záéíóúñü]+", (text or "").casefold())
    if not words or "no" in words:
        return False
    normalized = " ".join(words)
    if words[0] in YES_WORDS or normalized.startswith(YES_PHRASES):
        return True
    return words[0] in AMBIGUOUS_YES and (len(words) == 1 or words[1] in COURTESY_WORDS or normalized.startswith("si me parece"))


def option_number(text):
    """The option a client points at in words: 'la 2', 'opción 3', 'la primera'."""
    normalized = (text or "").casefold()
    match = OPTION_NUMBER_PATTERN.search(normalized)
    if match:
        return int(match.group(1))
    return next((OPTION_ORDINALS[word] for word in re.findall(r"[a-záéíóúñ]+", normalized) if word in OPTION_ORDINALS), None)


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
    """Every approved photo of a unit, numbered; the caller keeps the unsent ones and caps them."""
    public = item["public"]
    urls = approved_images(public)
    return [{"reference": public["public_reference"], "url": url,
             "caption": caption_for(public, photo_number=index, photo_total=len(urls))}
            for index, url in enumerate(urls, 1)]


def plan_photos(reply, stage, inventory_matches, unit_reference=None):
    """Return (photos, unit_item) for this turn.

    With `unit_reference` (the unit the client chose or asked about) every approved
    photo of that unit, uncapped so a later request can bring the next ones. Otherwise,
    when the reply lists options, the main photo of each, at most MAX_OPTIONS_WITH_PHOTO.
    `unit_item` is the unit resolved from `unit_reference`, or None.
    """
    by_reference = {item["public"]["public_reference"]: item for item in (inventory_matches or [])
                    if isinstance(item.get("public"), dict) and item["public"].get("public_reference")}
    if unit_reference:
        item = by_reference.get(unit_reference)
        return (_unit_photos(item), item) if item else ([], None)
    if stage != "qualified":
        return [], None
    photos = []
    shown = [reference for reference in references_in(reply) if reference in by_reference]
    for number, reference in enumerate(shown[:MAX_OPTIONS_WITH_PHOTO], 1):
        public = by_reference[reference]["public"]
        images = approved_images(public)
        # Units of one project share its photos: the same picture goes out once.
        if images and images[0] not in {photo["url"] for photo in photos}:
            photos.append({"reference": reference, "url": images[0], "caption": caption_for(public, option_number=number)})
    return photos, None


def photo_request_reply(item, available, sending):
    """Answer a photo request honestly: what goes out now, or why nothing does."""
    reference = item["public"]["public_reference"]
    if sending and sending < available:
        text = "Te comparto más fotos de esta unidad. ¿Qué más te gustaría saber de ella?"
    elif sending:
        text = "Te comparto las fotos de esta unidad. ¿Qué más te gustaría saber de ella?"
    elif available:
        text = ("Ya te envié todas las fotos que tengo de esta unidad. Si quieres ver más, "
                "puedo pedirle a un asesor que te envíe otras. ¿Te parece bien?")
    else:
        text = ("Por ahora no tengo fotos de esta unidad para compartirte. "
                "Puedo pedirle a un asesor que te las envíe. ¿Te parece bien?")
    return f"Ref. {reference}\n{text}"


def which_option_reply(references):
    """Several options on screen and no unit named: ask which one, keeping them selectable."""
    listed = "\n".join(f"Ref. {reference}" for reference in references)
    return f"{listed}\n¿De cuál de estas opciones quieres ver las fotos? Respóndeme con su número."


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


def unsent(delivery_log, phone, search_key, photos):
    """The photos this client has not received yet in this search. Never raises."""
    if not photos:
        return []
    try:
        skip = delivery_log.already_sent(phone, search_key or NO_SEARCH, [photo["url"] for photo in photos])
    except Exception:
        logger.exception("Media delivery check failed")
        skip = set()
    return [photo for photo in photos if photo["url"] not in skip]


def send_photos(send_image, delivery_log, phone, search_key, photos):
    """Send each photo not sent before in this search; return how many went out. Never raises."""
    search_key = search_key or NO_SEARCH
    sent = 0
    for photo in unsent(delivery_log, phone, search_key, photos):
        try:
            send_image(phone, photo["url"], photo["caption"])
            delivery_log.record(phone, search_key, photo["url"])
            sent += 1
        except Exception as error:  # a photo must never block the text reply
            logger.warning("Photo delivery failed: category=%s", type(error).__name__)
    if sent:
        logger.info("Photos delivered: count=%s", sent)
    return sent
