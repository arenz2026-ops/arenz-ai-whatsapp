"""Upload property photos to Supabase Storage and register them for the bot.

The folder holds one subfolder per property, named by its public_reference:

    fotos/
      ARZ-000001/
        01-sala.jpg
        02-cocina.png

Each photo is re-encoded as JPEG (at most 1600 px on its longest side and 1 MB, with
its metadata, GPS location included, removed), uploaded to the public bucket
"inmuebles" and registered in inventory_property_media with approved_for_client=false.
The bot only sends approved photos, so someone looks at them first and then re-runs
with --approve-media, which approves the photos of this folder.

Dry-run is the default: without --apply nothing is uploaded or written. The object
name carries a hash of the original file, so running the same folder twice uploads
nothing new, and a replaced photo gets a new link instead of a stale cached one.
"""
import argparse, hashlib, io, os, re, sys, unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from PIL import Image, ImageOps

BUCKET = "inmuebles"
MAX_SIDE = 1600
MAX_BYTES = 1024 * 1024
QUALITIES = (85, 80, 75, 70, 65, 60, 55, 50)
MIN_SIDE = 400
MEDIA_TYPE = "photo"  # inventory_property_media.media_type accepts only 'photo' or 'link'
SUPPORTED = {".jpg", ".jpeg", ".png", ".webp"}
IGNORED = {"thumbs.db", "desktop.ini"}


def natural_key(name):
    """'2.jpg' before '10.jpg', the order a person numbering photos expects."""
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", name)]


def collect(root):
    """[(reference, [photo paths])]: one per subfolder, plus the folder itself if it holds files."""
    def files_in(folder):
        entries = sorted(os.scandir(folder), key=lambda entry: natural_key(entry.name))
        return [entry.path for entry in entries if entry.is_file()
                and not entry.name.startswith(".") and entry.name.casefold() not in IGNORED]

    groups = []
    direct = files_in(root)
    if direct:
        groups.append((os.path.basename(os.path.abspath(root)), direct))
    for entry in sorted(os.scandir(root), key=lambda entry: natural_key(entry.name)):
        if entry.is_dir() and not entry.name.startswith("."):
            files = files_in(entry.path)
            if files:
                groups.append((entry.name, files))
    return groups


def _as_rgb(image):
    """JPEG has no transparency: flatten it onto white instead of black."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", rgba.size, "white")
        flat.paste(rgba, mask=rgba.getchannel("A"))
        return flat
    return image.convert("RGB")


def compress_photo(data):
    """Return (jpeg bytes, (width, height), original (width, height)). ValueError if unusable."""
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            original, icc = source.size, source.info.get("icc_profile")
            # Phones store "rotate me" as a tag; apply it, since the tag is dropped below.
            image = _as_rgb(ImageOps.exif_transpose(source))
    except Exception as error:  # Pillow raises many types for a broken or exotic file
        raise ValueError("no es una imagen legible") from error
    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    while True:
        for quality in QUALITIES:
            buffer = io.BytesIO()
            # No exif= argument: EXIF (camera, date, GPS) is not carried over.
            image.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True, icc_profile=icc)
            if buffer.tell() <= MAX_BYTES:
                return buffer.getvalue(), image.size, original
        if max(image.size) <= MIN_SIDE:
            raise ValueError("no se pudo comprimir por debajo de 1 MB")
        image = image.resize((max(1, round(image.width * 0.8)), max(1, round(image.height * 0.8))), Image.Resampling.LANCZOS)


def object_path(reference, filename, data):
    """'ARZ-000001/01-sala-3fa2c1d9e0.jpg': readable, and unique to the original's content."""
    stem = unicodedata.normalize("NFKD", os.path.splitext(filename)[0]).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^a-z0-9]+", "-", stem.casefold()).strip("-") or "foto"
    return f"{reference}/{stem}-{hashlib.sha256(data).hexdigest()[:10]}.jpg"


def public_url(base_url, path):
    return f"{base_url.rstrip('/')}/storage/v1/object/public/{BUCKET}/{quote(path)}"


def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


def plan_media(groups, property_ids, existing, base_url, approve=False, read=_read):
    """Decide UPLOAD / APPROVE / NO-OP / REJECT per photo without touching anything.

    property_ids: {public_reference: property_id}; existing: {property_id: [media rows]}.
    """
    plans = []
    for reference, paths in groups:
        property_id = property_ids.get(reference)
        rows = {row["media_url"]: row for row in existing.get(property_id, [])} if property_id else {}
        next_order = max([int(row.get("display_order") or 0) for row in rows.values()], default=0) + 1
        for path in paths:
            plan = {"reference": reference, "property_id": property_id, "file": os.path.basename(path), "source": path,
                    "action": "REJECT", "errors": [], "approve": approve}
            plans.append(plan)
            extension = os.path.splitext(path)[1].casefold()
            if not property_id:
                plan["errors"].append(f"no existe un inmueble con public_reference '{reference}'")
                continue
            if extension not in SUPPORTED:
                plan["errors"].append(f"formato '{extension or 'sin extensión'}' no soportado: usa JPG, PNG o WEBP")
                continue
            data = read(path)
            try:
                jpeg, size, original = compress_photo(data)
            except ValueError as error:
                plan["errors"].append(str(error))
                continue
            plan.update(path=object_path(reference, plan["file"], data), size=size, original=original,
                        bytes_in=len(data), bytes_out=len(jpeg))
            plan["url"] = public_url(base_url, plan["path"])
            row = rows.get(plan["url"])
            if row is None:
                plan.update(action="UPLOAD", display_order=next_order)
                rows[plan["url"]] = {"media_url": plan["url"], "approved_for_client": approve, "display_order": next_order}
                next_order += 1
            elif approve and not row.get("approved_for_client"):
                plan["action"] = "APPROVE"
            else:
                plan.update(action="NO-OP", approved=bool(row.get("approved_for_client")))
    return plans


class SupabaseMedia:
    """inventory_properties / inventory_property_media over REST, photos over Storage."""

    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    def property_ids(self, references):
        if not references:
            return {}
        quoted = ",".join('"%s"' % reference.replace('"', '') for reference in references)
        response = requests.get(f"{self.url}/rest/v1/inventory_properties", headers=self.headers, timeout=20,
                                params={"select": "property_id,public_reference", "public_reference": f"in.({quoted})"})
        response.raise_for_status()
        return {row["public_reference"]: row["property_id"] for row in response.json()}

    def media(self, property_ids):
        if not property_ids:
            return {}
        response = requests.get(f"{self.url}/rest/v1/inventory_property_media", headers=self.headers, timeout=20,
                                params={"select": "property_id,media_url,approved_for_client,display_order",
                                        "property_id": "in.(" + ",".join(property_ids) + ")"})
        response.raise_for_status()
        grouped = {}
        for row in response.json():
            grouped.setdefault(row["property_id"], []).append(row)
        return grouped

    def upload(self, path, data):
        # x-upsert: a re-run after a half-finished one overwrites the same content.
        response = requests.post(f"{self.url}/storage/v1/object/{BUCKET}/{quote(path)}", data=data, timeout=60,
                                 headers={**self.headers, "Content-Type": "image/jpeg", "x-upsert": "true",
                                          "cache-control": "max-age=31536000"})
        response.raise_for_status()

    def register(self, row):
        response = requests.post(f"{self.url}/rest/v1/inventory_property_media?on_conflict=property_id,media_url",
                                 headers={**self.headers, "Content-Type": "application/json",
                                          "Prefer": "resolution=ignore-duplicates,return=minimal"}, json=row, timeout=20)
        response.raise_for_status()

    def approve(self, property_id, media_url, stamp):
        response = requests.patch(f"{self.url}/rest/v1/inventory_property_media", timeout=20,
                                  headers={**self.headers, "Content-Type": "application/json", "Prefer": "return=minimal"},
                                  params={"property_id": f"eq.{property_id}", "media_url": f"eq.{media_url}"},
                                  json={"approved_for_client": True, "approved_at": stamp})
        response.raise_for_status()


def apply_plans(store, plans, now=None, read=_read, done=None):
    """Upload and register new photos, approve the ones asked. One photo at a time, so a
    failure leaves every earlier photo complete and a re-run picks up the rest."""
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    done = {"uploaded": 0, "approved": 0} if done is None else done
    for plan in plans:
        if plan["action"] == "UPLOAD":
            jpeg, _, _ = compress_photo(read(plan["source"]))
            store.upload(plan["path"], jpeg)
            row = {"property_id": plan["property_id"], "media_type": MEDIA_TYPE, "media_url": plan["url"],
                   "display_order": plan["display_order"], "approved_for_client": plan["approve"]}
            if plan["approve"]:
                row["approved_at"] = stamp
            store.register(row)
            done["uploaded"] += 1
            done["approved"] += int(plan["approve"])
        elif plan["action"] == "APPROVE":
            store.approve(plan["property_id"], plan["url"], stamp)
            done["approved"] += 1
    return done


def _size(count):
    return f"{count / 1048576:.1f} MB".replace(".", ",") if count >= 1048576 else f"{round(count / 1024)} KB"


def render_report(plans, show_urls=False):
    lines, counts, current = [], {"UPLOAD": 0, "APPROVE": 0, "NO-OP": 0, "REJECT": 0}, None
    for plan in plans:
        counts[plan["action"]] += 1
        if plan["reference"] != current:
            current = plan["reference"]
            lines.append(current)
        lines.append(f"  [{plan['action']:<7}] {plan['file']}")
        for error in plan["errors"]:
            lines.append(f"            ! {error}")
        if plan["action"] == "UPLOAD":
            (w0, h0), (w1, h1) = plan["original"], plan["size"]
            state = "aprobada" if plan["approve"] else "sin aprobar"
            lines.append(f"            · {w0}×{h0} → {w1}×{h1}, {_size(plan['bytes_in'])} → {_size(plan['bytes_out'])}, "
                         f"orden {plan['display_order']}, {state}")
            lines.append(f"            · {plan['url'] if show_urls else plan['path']}")
        elif plan["action"] == "APPROVE":
            lines.append("            · ya subida; se marcará aprobada para el cliente")
        elif plan["action"] == "NO-OP":
            lines.append("            · ya subida y aprobada" if plan["approved"]
                         else "            · ya subida, pendiente de aprobar (--approve-media)")
    lines.append("")
    lines.append("  ".join(f"{action}={count}" for action, count in counts.items()))
    return "\n".join(lines), counts


def _store_from_env():
    url, key = os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")
    if not (url and key):
        raise SystemExit("SUPABASE_URL and SUPABASE_KEY must be set")
    return SupabaseMedia(url, key)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # the report carries accents; Windows consoles may not
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Sube fotos de inmuebles a Supabase Storage. Dry-run salvo --apply.")
    parser.add_argument("folder", help="carpeta con una subcarpeta por public_reference (p. ej. fotos/ARZ-000001/)")
    parser.add_argument("--apply", action="store_true", help="sube y registra; sin esto no se escribe nada")
    parser.add_argument("--approve-media", action="store_true",
                        help="marca como aprobadas para el cliente las fotos de esta carpeta")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.folder):
        parser.error(f"no existe la carpeta {args.folder}")

    groups = collect(args.folder)
    if not groups:
        print("No hay fotos en la carpeta.")
        return 1
    store = _store_from_env()
    property_ids = store.property_ids([reference for reference, _ in groups])
    plans = plan_media(groups, property_ids, store.media(list(property_ids.values())), store.url, args.approve_media)
    report, counts = render_report(plans, show_urls=args.apply)
    print(report)
    if not args.apply:
        print("\nDRY-RUN: no se subió ni se escribió nada. Repite con --apply para aplicarlo.")
        return 1 if counts["REJECT"] else 0
    if counts["REJECT"]:
        print("\nHay fotos rechazadas: corrige o quita esos archivos antes de aplicar.")
        return 1
    result = {"uploaded": 0, "approved": 0}
    try:
        apply_plans(store, plans, done=result)
    except requests.RequestException as error:
        response = getattr(error, "response", None)
        detail = f"HTTP {response.status_code}: {response.text[:200]}" if response is not None else type(error).__name__
        print(f"\nSe detuvo por un error de Supabase ({detail}).")
        print(f"Quedaron completas {result['uploaded']} subidas y {result['approved']} aprobaciones; "
              f"corrige el problema (¿existe el bucket '{BUCKET}'?, ver README) y vuelve a ejecutar.")
        return 1
    print(f"\nAplicado: {result['uploaded']} fotos subidas, {result['approved']} aprobadas para el cliente.")
    if result["uploaded"] and not args.approve_media:
        print("Revisa las fotos en los enlaces de arriba y apruébalas con --apply --approve-media.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
