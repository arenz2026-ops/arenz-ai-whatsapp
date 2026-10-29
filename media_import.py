"""Upload property photos to Supabase Storage and register them for the bot.

The source is a folder with one subfolder per public_reference:

    fotos/
      C2-00003A4AD688/
        01-fachada.jpg
        02-sala.png

A folder that holds photos directly is read as a single reference named after it.
Photos are taken in file-name order; the first one is the main photo, the one the
bot sends when it presents the unit among other options.

Each photo is recompressed to JPEG of at most 1 MB and 1600 px on its longest side,
without EXIF (phone photos carry the GPS position of whoever took them), uploaded
to the public bucket "inmuebles" and registered in inventory_property_media.

Dry-run is the default: photos are read and compressed in memory to report what
would happen, and nothing is uploaded or written. Reading the inventory to check
references needs SUPABASE_URL and SUPABASE_KEY; without them the dry-run still
checks the photos. --apply uploads and registers.

A registered photo is NOT shown to clients until someone approves it: rows are
created with approved_for_client=false. --approve-media marks this run's photos as
approved, so use it only once they have been looked at.
"""
import argparse, hashlib, io, os, re, sys, unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

import requests

BUCKET = "inmuebles"
MAX_BYTES = 1_000_000
MAX_SIDE = 1600
MIN_SIDE = 480
QUALITIES = (85, 80, 75, 70, 65, 60, 55, 50)
MEDIA_TYPE = "photo"  # inventory_property_media.media_type admits only 'photo' and 'link'
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
IGNORED_FILES = {"thumbs.db", "desktop.ini"}


def compress_image(data):
    """Return (jpeg_bytes, (width, height)): ≤ MAX_SIDE px, ≤ MAX_BYTES, no EXIF.

    Raises ValueError when the file is not a readable image.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            image = ImageOps.exif_transpose(source)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ValueError(f"no es una imagen legible ({type(error).__name__})") from error
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        image = Image.new("RGB", rgba.size, (255, 255, 255))
        image.paste(rgba, mask=rgba.getchannel("A"))
    else:
        image = image.convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    while True:
        for quality in QUALITIES:
            buffer = io.BytesIO()
            # No exif= argument: the output carries no metadata at all.
            image.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True)
            if buffer.tell() <= MAX_BYTES:
                return buffer.getvalue(), image.size
        if max(image.size) <= MIN_SIDE:
            raise ValueError("no se pudo comprimir por debajo de 1 MB")
        image = image.resize((max(1, round(image.width * 0.8)), max(1, round(image.height * 0.8))), Image.LANCZOS)


def slug(text):
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.casefold()).strip("-")[:40].strip("-") or "foto"


def object_path(reference, filename, source_bytes):
    """Stable per content: re-running the same photo lands on the same object and URL,
    and a replaced photo gets a new URL, so no cache serves the old one."""
    digest = hashlib.sha256(source_bytes).hexdigest()[:10]
    return f"{reference}/{slug(os.path.splitext(filename)[0])}-{digest}.jpg"


def public_url(supabase_url, bucket, path):
    return f"{supabase_url.rstrip('/')}/storage/v1/object/public/{bucket}/{quote(path)}"


def _is_noise(name):
    return name.startswith(".") or name.casefold() in IGNORED_FILES


def scan_folder(root):
    """Return [(reference, [file paths in name order], [skipped names])]."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise SystemExit(f"No existe la carpeta: {root}")
    entries = sorted((name for name in os.listdir(root) if not _is_noise(name)), key=str.casefold)
    folders = [name for name in entries if os.path.isdir(os.path.join(root, name))]
    if not folders:
        # The folder itself is one reference.
        return [_scan_reference(os.path.basename(root), root)]
    return [_scan_reference(name, os.path.join(root, name)) for name in folders]


def _scan_reference(name, path):
    files, skipped = [], []
    for entry in sorted(os.listdir(path), key=str.casefold):
        full = os.path.join(path, entry)
        if _is_noise(entry) or not os.path.isfile(full):
            continue
        if os.path.splitext(entry)[1].casefold() in IMAGE_EXTENSIONS:
            files.append(full)
        else:
            skipped.append(entry)
    return name.strip().upper(), files, skipped


class SupabaseMedia:
    """Inventory lookups, media rows and Storage uploads over Supabase's REST surface."""

    def __init__(self, url, key, bucket=BUCKET):
        self.url = url.rstrip("/")
        self.bucket = bucket
        self.key = key
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def property_ids(self, references):
        if not references:
            return {}
        quoted = ",".join('"%s"' % reference.replace('"', "") for reference in references)
        response = requests.get(f"{self.url}/rest/v1/inventory_properties", headers=self.headers, timeout=20,
                                params={"select": "property_id,public_reference", "public_reference": f"in.({quoted})"})
        response.raise_for_status()
        return {row["public_reference"]: row["property_id"] for row in response.json()}

    def existing_media(self, property_ids):
        if not property_ids:
            return {}
        response = requests.get(f"{self.url}/rest/v1/inventory_property_media", headers=self.headers, timeout=20,
                                params={"select": "media_id,property_id,media_url,approved_for_client",
                                        "property_id": "in.(" + ",".join(property_ids) + ")"})
        response.raise_for_status()
        return {(row["property_id"], row["media_url"]): row for row in response.json()}

    def upload(self, path, data):
        response = requests.post(f"{self.url}/storage/v1/object/{self.bucket}/{quote(path)}", data=data, timeout=60,
                                 headers={"apikey": self.key, "Authorization": f"Bearer {self.key}",
                                          "Content-Type": "image/jpeg", "x-upsert": "true",
                                          "cache-control": "max-age=31536000"})
        if response.status_code >= 400:
            raise RuntimeError(f"Storage rechazó {path}: HTTP {response.status_code} {response.text[:200]}")

    def insert(self, rows):
        response = requests.post(f"{self.url}/rest/v1/inventory_property_media?on_conflict=property_id,media_url",
                                 headers={**self.headers, "Prefer": "resolution=ignore-duplicates,return=minimal"},
                                 json=rows, timeout=30)
        response.raise_for_status()

    def approve(self, media_ids, approved_at):
        response = requests.patch(f"{self.url}/rest/v1/inventory_property_media",
                                  headers={**self.headers, "Prefer": "return=minimal"},
                                  params={"media_id": "in.(" + ",".join(media_ids) + ")"},
                                  json={"approved_for_client": True, "approved_at": approved_at}, timeout=30)
        response.raise_for_status()


def plan_folder(groups, property_ids, existing, supabase_url, bucket=BUCKET, approve=False, checked=True):
    """One plan per reference. Reads and compresses photos; writes nothing.

    property_ids maps reference -> property_id; existing maps (property_id, url) -> row.
    checked=False means the inventory could not be read: references stay unverified.
    """
    plans = []
    for reference, files, skipped in groups:
        property_id = property_ids.get(reference)
        plan = {"reference": reference, "property_id": property_id, "photos": [], "skipped": skipped, "errors": []}
        if checked and not property_id:
            plan["errors"].append("no existe esa referencia en el inventario")
        if not files:
            plan["errors"].append("la carpeta no tiene fotos")
        for order, path in enumerate(files, 1):
            name = os.path.basename(path)
            with open(path, "rb") as handle:
                source = handle.read()
            photo = {"name": name, "order": order, "source_bytes": len(source)}
            try:
                photo["data"], photo["size"] = compress_image(source)
            except ValueError as error:
                plan["errors"].append(f"{name}: {error}")
                continue
            photo["path"] = object_path(reference, name, source)
            photo["url"] = public_url(supabase_url or "https://SUPABASE_URL", bucket, photo["path"])
            row = existing.get((property_id, photo["url"])) if property_id else None
            if not row:
                photo["action"] = "NUEVA"
            elif approve and not row.get("approved_for_client"):
                photo["action"], photo["media_id"] = "APROBAR", row["media_id"]
            else:
                photo["action"] = "IGUAL"
            plan["photos"].append(photo)
        plans.append(plan)
    return plans


def apply_plans(store, plans, approve=False, now=None):
    """Upload new photos, register them, then approve when asked. Never deletes."""
    approved_at = (now or datetime.now(timezone.utc)).isoformat()
    totals = {"subidas": 0, "aprobadas": 0}
    for plan in plans:
        new = [photo for photo in plan["photos"] if photo["action"] == "NUEVA"]
        for photo in new:
            store.upload(photo["path"], photo["data"])
        if new:
            store.insert([{"property_id": plan["property_id"], "media_type": MEDIA_TYPE, "media_url": photo["url"],
                           "display_order": photo["order"], "approved_for_client": approve,
                           "approved_at": approved_at if approve else None} for photo in new])
        to_approve = [photo["media_id"] for photo in plan["photos"] if photo["action"] == "APROBAR"]
        if to_approve:
            store.approve(to_approve, approved_at)
        totals["subidas"] += len(new)
        totals["aprobadas"] += (len(new) if approve else 0) + len(to_approve)
    return totals


def _kb(size):
    return f"{size / 1024:,.0f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:,.1f} MB"


def render_report(plans):
    lines, counts = [], {"NUEVA": 0, "APROBAR": 0, "IGUAL": 0, "OMITIDA": 0, "ERROR": 0}
    for plan in plans:
        state = "ERROR" if plan["errors"] else ("sin verificar" if not plan["property_id"] else "ok")
        lines.append(f"{plan['reference']}  ({len(plan['photos'])} foto(s), {state})")
        for photo in plan["photos"]:
            counts[photo["action"]] += 1
            width, height = photo["size"]
            lines.append(f"  [{photo['action']:<7}] {photo['order']:>2}. {photo['name']} → {photo['path']}  "
                         f"({_kb(photo['source_bytes'])} → {_kb(len(photo['data']))}, {width}×{height})")
        for name in plan["skipped"]:
            counts["OMITIDA"] += 1
            lines.append(f"  [OMITIDA] {name}: formato no admitido")
        for error in plan["errors"]:
            counts["ERROR"] += 1
            lines.append(f"  ! {error}")
    lines.append("")
    lines.append("  ".join(f"{action}={count}" for action, count in counts.items()))
    return "\n".join(lines), counts


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Sube fotos de inmuebles a Supabase Storage. Dry-run salvo --apply.")
    parser.add_argument("folder", help="carpeta con una subcarpeta por public_reference")
    parser.add_argument("--apply", action="store_true", help="subir y registrar; sin esto no se escribe nada")
    parser.add_argument("--approve-media", action="store_true",
                        help="marcar las fotos como aprobadas para enviarlas a clientes (approved_for_client=true)")
    parser.add_argument("--bucket", default=BUCKET, help=f"bucket público de Storage (por defecto {BUCKET})")
    args = parser.parse_args(argv)

    url, key = os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")
    if args.apply and not (url and key):
        raise SystemExit("SUPABASE_URL y SUPABASE_KEY son obligatorias con --apply")
    groups = scan_folder(args.folder)
    store = SupabaseMedia(url, key, args.bucket) if url and key else None
    property_ids = store.property_ids([reference for reference, _, _ in groups]) if store else {}
    existing = store.existing_media(list(property_ids.values())) if store else {}
    plans = plan_folder(groups, property_ids, existing, url, args.bucket, approve=args.approve_media, checked=bool(store))
    report, counts = render_report(plans)
    print(report)
    if not store:
        print("\nSin SUPABASE_URL/SUPABASE_KEY: las referencias no se verificaron contra el inventario.")
    if not args.approve_media and counts["NUEVA"]:
        print("\nLas fotos nuevas quedan sin aprobar (approved_for_client=false): el bot no las enviará "
              "hasta revisarlas y repetir con --approve-media.")
    if not args.apply:
        print("\nDRY-RUN: no se subió ni se escribió nada. Repite con --apply para aplicarlo.")
        return 1 if counts["ERROR"] else 0
    if counts["ERROR"]:
        print("\nHay errores: corrígelos antes de aplicar. No se subió nada.")
        return 1
    totals = apply_plans(store, plans, approve=args.approve_media)
    print(f"\nAplicado: {totals['subidas']} foto(s) subidas, {totals['aprobadas']} aprobada(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
