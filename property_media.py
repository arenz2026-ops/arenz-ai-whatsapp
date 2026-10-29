"""Decide which property photos go to a client on a turn, and send each one once.

Approved photos (inventory_property_media rows with approved_for_client=true) reach
the public snapshot of each property; this module turns them into WhatsApp image
messages:

- options presented  -> the main photo of each option (at most 3 options)
- one unit selected  -> up to 5 photos of that unit
- "fotos" requested  -> up to 5 photos of the unit the client means

A photo already sent to the same client in the same search is not sent again
(media_deliveries in Supabase, with an in-process copy that also covers a Supabase
outage). Sending is best effort: a failed photo is logged and the text still goes.
"""
import logging
import re
from collections import OrderedDict
from datetime import datetime, timezone

import requests

logger = logging.getLogger("arenz.media")

MAX_OPTIONS_WITH_PHOTO = 3
MAX_UNIT_PHOTOS = 5
NO_SEARCH = "sin-busqueda"
# inventory_property_media.media_type only admits 'photo' and 'link'; the other
# spellings are accepted so a hand-made snapshot cannot silently drop its photos.
IMAGE_TYPES = {"photo", "image", "imagen", "foto", "fotografia", "fotografía"}
PHOTO_WORDS = r"(?:fotos?|fotograf[ií]as?|im[aá]gen(?:es)?)"
PHOTO_REQUEST_PATTERN = re.compile(rf"\b{PHOTO_WORDS}\b")
PHOTO_REFUSAL_PATTERN = re.compile(
    rf"\bno\s+(?:me\s+)?(?:mandes|manden|env[ií]es|env[ií]en|quiero|necesito)\b(?:\s+\w+){{0,3}}?\s+{PHOTO_WORDS}\b")
OPTION_NUMBER_PATTERN = re.compile(r"\bopci[oó]n\s*(?:n[°ºo.]?\s*)?([1-9])\b|\b(?:la|el|del)\s+([1-9])\b|#\s*([1-9])\b")
OPTION_ORDINAL_PATTERN = re.compile(r"\b(?:la|el|del)\s+(primer[ao]?|segund[ao]|tercer[ao]?)\b")
ORDINALS = {"primer": 1, "primera": 1, "primero": 1, "segunda": 2, "segundo": 2,
            "tercer": 3, "tercera": 3, "tercero": 3}
REFERENCE_PATTERN = re.compile(r"\bref\.?\s*([a-z0-9][a-z0-9-]{1,23})\b", re.IGNORECASE)
UNIT_IDENTITY_PATTERN = re.compile(r"^Proyecto (.*?); unidad (.*?)\.(?:\s|$)")
# The text that offers an advisor for photos; the next "sí" is read against it.
ADVISOR_PHOTO_OFFER = "pedirle a un asesor que te las envíe"
AFFIRMATIVE_PATTERN = re.compile(
    r"(?:s[ií]|dale|ok(?:ay)?|claro|de acuerdo|perfecto|bueno|ya|est[aá] bien|me parece(?: bien)?|por favor|porfa(?:vor)?)"
    r"(?:[\s,]+(?:s[ií]|claro|dale|gracias|perfecto|genial|quiero|me gustar[ií]a|por favor|porfa(?:vor)?|me parece bien))*")


def _normalized(text):
    return " ".join((text or "").casefold().split())


def is_photo_request(text):
    normalized = _normalized(text)
    return bool(PHOTO_REQUEST_PATTERN.search(normalized)) and not PHOTO_REFUSAL_PATTERN.search(normalized)


def option_number(text):
    """The option a client points at in words: "la 2", "opción 3", "la segunda"."""
    normalized = _normalized(text)
    match = OPTION_NUMBER_PATTERN.search(normalized)
    if match:
        return int(next(group for group in match.groups() if group))
    ordinal = OPTION_ORDINAL_PATTERN.search(normalized)
    return ORDINALS[ordinal.group(1)] if ordinal else None


def is_affirmative(text):
    return bool(AFFIRMATIVE_PATTERN.fullmatch(re.sub(r"[^\w\s,]", "", _normalized(text)).strip(" ,")))


def approved_images(public):
    """Only image-typed HTTPS media: WhatsApp downloads the link itself."""
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


def unit_title(public):
    """"Proyecto X; unidad Y." descriptions read as "X · Unidad Y", like the listing."""
    identity = UNIT_IDENTITY_PATTERN.match(public.get("public_description") or "")
    if identity:
        return f"{identity.group(1)} · Unidad {identity.group(2)}"
    return str(public.get("property_type") or "Inmueble").capitalize()


def caption_for(public, option_number=None, photo_number=None, photo_total=None):
    """Customer-facing caption. The internal reference never goes in it."""
    parts = [f"Opción {option_number}"] if option_number else []
    parts.append(unit_title(public))
    if public.get("district"):
        parts.append(public["district"])
    if public.get("price_amount") is not None:
        parts.append(price_label(public))
    if public.get("bedrooms") is not None:
        parts.append(f"{float(public['bedrooms']):g} dorm.")
    if photo_number and photo_total and photo_total > 1:
        parts.append(f"foto {photo_number}/{photo_total}")
    caption = " · ".join(parts)
    reference = public.get("public_reference")
    if reference:
        caption = re.sub(rf"\s*\b{re.escape(reference)}\b", "", caption, flags=re.IGNORECASE)
    return caption.strip(" ·")


def references_in(text):
    return list(dict.fromkeys(REFERENCE_PATTERN.findall(text or "")))


def unit_photos(item):
    """Every approved photo of one unit, in display order and numbered over all of them.

    The caller drops the ones already sent and then keeps MAX_UNIT_PHOTOS, so a
    second request sends the next photos instead of none.
    """
    public = item["public"]
    urls = approved_images(public)
    return [{"reference": public["public_reference"], "url": url,
             "caption": caption_for(public, photo_number=index, photo_total=len(urls))}
            for index, url in enumerate(urls, 1)]


def option_photos(items):
    """The main photo of each option presented, numbered as in the listing."""
    photos = []
    for number, item in enumerate(items[:MAX_OPTIONS_WITH_PHOTO], 1):
        images = approved_images(item["public"])
        if images:
            photos.append({"reference": item["public"]["public_reference"], "url": images[0],
                           "caption": caption_for(item["public"], option_number=number)})
    return photos


def photo_status_text(status):
    """status: "sending", "already_sent" or "none" (the unit has no approved photos)."""
    if status == "sending":
        return "Te comparto las fotos de esta unidad. ¿Qué más te gustaría saber de ella?"
    if status == "already_sent":
        return ("Ya te envié todas las fotos que tengo de esta unidad. "
                f"Si quieres más, puedo {ADVISOR_PHOTO_OFFER}. ¿Te parece bien?")
    return f"Por ahora no tengo fotos de esta unidad para compartirte. Puedo {ADVISOR_PHOTO_OFFER}. ¿Te parece bien?"


def photo_request_reply(item, status):
    """Text that answers a photo request.

    The "Ref." line is stripped before WhatsApp; it stays in memory so the next turn
    still knows which unit is on screen.
    """
    return f"Ref. {item['public']['public_reference']}\n{photo_status_text(status)}"


def offers_advisor_for_photos(message):
    return ADVISOR_PHOTO_OFFER in (message or "").casefold()


def photo_choice_reply(references):
    """Several options on screen and no way to tell which one: ask, keeping their order."""
    hidden = "\n".join(f"Ref. {reference}" for reference in references)
    numbers = ", ".join(str(n) for n in range(1, len(references))) + f" o {len(references)}"
    return f"{hidden}\n¿De cuál opción quieres ver las fotos: {numbers}?"


class MediaDeliveryLog:
    """Which photos each client already received in a search.

    Supabase (media_deliveries) is the durable record. Every delivery is also kept
    in process, so a missing table or a Supabase outage does not repeat photos
    within the same run.
    """

    MEMORY_LIMIT = 5000

    def __init__(self, url=None, key=None):
        self.url = (url or "").rstrip("/")
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"} if key else {}
        self.memory = OrderedDict()

    def already_sent(self, phone, search_key, urls):
        """Never raises: an unreadable log means "nothing known", not an error."""
        urls = list(urls)
        sent = {url for url in urls if (phone, search_key, url) in self.memory}
        if not self.url or not urls:
            return sent
        try:
            response = requests.get(f"{self.url}/rest/v1/media_deliveries", headers=self.headers, timeout=10,
                                    params={"select": "media_url", "phone": f"eq.{phone}", "search_key": f"eq.{search_key}"})
            response.raise_for_status()
            return sent | ({row["media_url"] for row in response.json()} & set(urls))
        except (requests.RequestException, ValueError, KeyError, TypeError):
            logger.warning("Media delivery log unavailable; using the in-process copy")
            return sent

    def record(self, phone, search_key, url):
        self.memory[(phone, search_key, url)] = datetime.now(timezone.utc)
        while len(self.memory) > self.MEMORY_LIMIT:
            self.memory.popitem(last=False)
        if not self.url:
            return
        try:
            response = requests.post(f"{self.url}/rest/v1/media_deliveries?on_conflict=phone,search_key,media_url", timeout=10,
                                     headers={**self.headers, "Prefer": "resolution=ignore-duplicates,return=minimal"},
                                     json={"phone": phone, "search_key": search_key, "media_url": url})
            response.raise_for_status()
        except requests.RequestException:
            logger.warning("Unable to record media delivery")


def unsent(photos, sent_before):
    """Drop photos the client already has. `sent_before(urls)` returns a set of URLs."""
    if not photos:
        return []
    try:
        skip = sent_before([photo["url"] for photo in photos]) or set()
    except Exception:
        logger.exception("Media delivery check failed")
        skip = set()
    return [photo for photo in photos if photo["url"] not in skip]


def send_photos(send_image, delivery_log, phone, search_key, photos):
    """Send each photo and record it; return how many went out. Never raises.

    `photos` is already filtered with unsent(); a photo that fails is not recorded,
    so a later turn may offer it again.
    """
    search_key = search_key or NO_SEARCH
    sent = 0
    for photo in photos or []:
        try:
            send_image(phone, photo["url"], photo["caption"])
        except Exception as error:  # a photo must never block the text reply
            logger.warning("Photo delivery failed: category=%s", type(error).__name__)
            continue
        try:
            delivery_log.record(phone, search_key, photo["url"])
        except Exception:
            logger.exception("Media delivery record failed")
        sent += 1
    if sent:
        logger.info("Photos delivered: count=%s", sent)
    return sent
