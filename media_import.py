"""Upload property photos to Supabase Storage and register them for the bot.

The folder holds one subfolder per unit, named by its public_reference, or per
project, named as the project appears in the inventory ("Proyecto <name>; unidad ..."):

    fotos/
      C2-37F90956794A/          photos of that unit only
        01-sala.jpg
      CONDOMINIO LOS BOSQUES/   photos shared by every unit of the project
        01-fachada.jpg
        02-piscina.png

Each photo is re-encoded as JPEG (at most 1600 px on its longest side and 1 MB, with
its metadata, GPS location included, removed), uploaded once to the public bucket
"inmuebles" and registered in inventory_property_media, for each unit it belongs to,
with approved_for_client=false. The bot only sends approved photos, so someone looks
at them first and then re-runs with --approve-media, which approves this folder's.
A unit's own photos come before its project's.

Dry-run is the default: without --apply nothing is uploaded or written. The object
name carries a hash of the original file, so running the same folder twice uploads
nothing new, and a replaced photo gets a new link instead of a stale cached one.
"""
import argparse, hashlib, io, os, re, sys, unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from PIL import Image, ImageOps

try:  # iPhone photos (HEIC/HEIF); optional, so everything else works without it
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF = True
except ImportError:
    HEIF = False

BUCKET = "inmuebles"
MAX_SIDE = 1600
MAX_BYTES = 1024 * 1024
QUALITIES = (85, 80, 75, 70, 65, 60, 55, 50)
MIN_SIDE = 400
MEDIA_TYPE = "photo"  # inventory_property_media.media_type accepts only 'photo' or 'link'
HEIF_EXTENSIONS = {".heic", ".heif"}
SUPPORTED = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"} | HEIF_EXTENSIONS
VIDEOS = {".mov", ".mp4", ".m4v", ".avi", ".3gp", ".mkv", ".wmv"}
RAW = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2"}
# Developers' renders run past Pillow's default bomb guard (89 Mpx); these files are
# the operator's own, not uploads from strangers.
Image.MAX_IMAGE_PIXELS = 300_000_000
IGNORED = {"thumbs.db", "desktop.ini"}
PROJECT_FOLDER = "proyectos"
PROJECT_ORDER = 100  # project photos sort after a unit's own ones
PAGE = 1000  # PostgREST returns at most this many rows per request


def natural_key(name):
    """'2.jpg' before '10.jpg', the order a person numbering photos expects."""
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", name)]


def collect(root):
    """[(name, [photo paths])]: one per subfolder, plus the folder itself if it holds files."""
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
            original, icc = source.size, source.info.get("icc_profile")
            source.draft("RGB", (MAX_SIDE, MAX_SIDE))  # a large JPEG decodes straight at a smaller scale
            source.load()
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


def slug(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


def bucket_folder(name, kind):
    """A unit's photos live under its reference; a project's under proyectos/<slug>."""
    return f"{PROJECT_FOLDER}/{slug(name) or 'proyecto'}" if kind == "proyecto" else name


def object_path(folder, filename, data):
    """'C2-37F9…/01-sala-3fa2c1d9e0.jpg': readable, and unique to the original's content."""
    stem = slug(os.path.splitext(filename)[0]) or "foto"
    return f"{folder}/{stem}-{hashlib.sha256(data).hexdigest()[:10]}.jpg"


def public_url(base_url, path):
    return f"{base_url.rstrip('/')}/storage/v1/object/public/{BUCKET}/{quote(path)}"


def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


def skip_reason(extension):
    """Why a file in a photo folder is left out, or None if it is a photo the script reads."""
    if extension in VIDEOS:
        return "video: el bot solo envía fotos"
    if extension in RAW:
        return "foto RAW: expórtala a JPG si la quieres usar"
    if extension in HEIF_EXTENSIONS and not HEIF:
        return "foto HEIC: falta instalar pillow-heif (pip install -r requirements.txt)"
    if extension not in SUPPORTED:
        return f"'{extension or 'sin extensión'}' no es un formato de foto compatible"
    return None


def plan_media(groups, targets, existing, base_url, approve=False, read=_read, limit=None):
    """Decide UPLOAD / APPROVE / NO-OP / SKIP / REJECT per file without touching anything.

    targets: {folder name: {"kind": "unidad"|"proyecto", "property_ids": [...]}};
    existing: {folder name: [media rows already stored under that folder's links]}.
    A folder that is no reference or project is REJECTed (it blocks --apply); a file
    that is no usable photo (video, RAW, unreadable, past `limit`) is only SKIPped.
    """
    plans = []
    for name, paths in groups:
        target = targets.get(name)
        rows = existing.get(name, [])
        taken = 0
        for position, path in enumerate(paths, 1):
            plan = {"group": name, "kind": target and target["kind"], "units": len(target["property_ids"]) if target else 0,
                    "file": os.path.basename(path), "source": path, "action": "REJECT", "errors": [], "approve": approve}
            plans.append(plan)
            extension = os.path.splitext(path)[1].casefold()
            if not target:
                plan["errors"].append(f"'{name}' no es una referencia ni un proyecto del inventario")
                continue
            reason = skip_reason(extension)
            if not reason and limit and taken >= limit:
                reason = f"pasa el límite de {limit} fotos por carpeta (--max-per-folder)"
            if reason:
                plan.update(action="SKIP", note=reason)
                continue
            data = read(path)
            try:
                jpeg, size, original = compress_photo(data)
            except ValueError as error:
                plan.update(action="SKIP", note=f"{error}; se omite")
                continue
            taken += 1
            plan.update(path=object_path(bucket_folder(name, target["kind"]), plan["file"], data), size=size,
                        original=original, bytes_in=len(data), bytes_out=len(jpeg),
                        display_order=position + (PROJECT_ORDER if target["kind"] == "proyecto" else 0))
            plan["url"] = public_url(base_url, plan["path"])
            stored = [row for row in rows if row["media_url"] == plan["url"]]
            registered = {row["property_id"] for row in stored}
            plan["missing"] = [pid for pid in target["property_ids"] if pid not in registered]
            plan["unapproved"] = sum(1 for row in stored if not row.get("approved_for_client"))
            if plan["missing"]:
                plan["action"] = "UPLOAD"
            elif approve and plan["unapproved"]:
                plan["action"] = "APPROVE"
            else:
                plan.update(action="NO-OP", approved=not plan["unapproved"])
    return plans


class SupabaseMedia:
    """inventory_properties / inventory_property_media over REST, photos over Storage."""

    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    def _all(self, table, params):
        rows = []
        while True:
            response = requests.get(f"{self.url}/rest/v1/{table}", headers=self.headers, timeout=30,
                                    params={**params, "limit": PAGE, "offset": len(rows)})
            response.raise_for_status()
            page = response.json()
            rows += page
            if len(page) < PAGE:
                return rows

    def resolve(self, names):
        """{name: target} for folder names that are a public_reference or a project name."""
        targets = {}
        if names:
            quoted = ",".join('"%s"' % name.replace('"', '') for name in names)
            for row in self._all("inventory_properties", {"select": "property_id,public_reference",
                                                          "public_reference": f"in.({quoted})", "order": "property_id"}):
                targets[row["public_reference"]] = {"kind": "unidad", "property_ids": [row["property_id"]]}
        for name in names:
            if name in targets:
                continue
            # Unit descriptions start "Proyecto <name>; unidad <unit>." (see inventory_reply).
            units = self._all("inventory_properties", {"select": "property_id", "order": "property_id",
                                                       "public_description": f"ilike.Proyecto {name}; unidad*"})
            if units:
                targets[name] = {"kind": "proyecto", "property_ids": [row["property_id"] for row in units]}
        return targets

    def media_under(self, url_prefix):
        return self._all("inventory_property_media", {"select": "property_id,media_url,approved_for_client",
                                                      "media_url": f"like.{url_prefix}*", "order": "media_id"})

    def upload(self, path, data):
        # x-upsert: a re-run after a half-finished one overwrites the same content.
        response = requests.post(f"{self.url}/storage/v1/object/{BUCKET}/{quote(path)}", data=data, timeout=60,
                                 headers={**self.headers, "Content-Type": "image/jpeg", "x-upsert": "true",
                                          "cache-control": "max-age=31536000"})
        response.raise_for_status()

    def register(self, rows):
        for start in range(0, len(rows), 500):
            response = requests.post(f"{self.url}/rest/v1/inventory_property_media?on_conflict=property_id,media_url",
                                     headers={**self.headers, "Content-Type": "application/json",
                                              "Prefer": "resolution=ignore-duplicates,return=minimal"},
                                     json=rows[start:start + 500], timeout=30)
            response.raise_for_status()

    def approve(self, media_url, stamp):
        """Approve one photo for every unit it is registered to."""
        response = requests.patch(f"{self.url}/rest/v1/inventory_property_media", timeout=30,
                                  headers={**self.headers, "Content-Type": "application/json", "Prefer": "return=minimal"},
                                  params={"media_url": f"eq.{media_url}", "approved_for_client": "is.false"},
                                  json={"approved_for_client": True, "approved_at": stamp})
        response.raise_for_status()


def apply_plans(store, plans, now=None, read=_read, done=None):
    """Upload and register new photos, approve the ones asked. One photo at a time, so a
    failure leaves every earlier photo complete and a re-run picks up the rest."""
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    done = {"uploaded": 0, "rows": 0, "approved": 0} if done is None else done
    for plan in plans:
        if plan["action"] == "UPLOAD":
            jpeg, _, _ = compress_photo(read(plan["source"]))
            store.upload(plan["path"], jpeg)
            extra = {"approved_at": stamp} if plan["approve"] else {}
            store.register([{"property_id": property_id, "media_type": MEDIA_TYPE, "media_url": plan["url"],
                             "display_order": plan["display_order"], "approved_for_client": plan["approve"], **extra}
                            for property_id in plan["missing"]])
            if plan["approve"] and plan["unapproved"]:
                store.approve(plan["url"], stamp)
            done["uploaded"] += 1
            done["rows"] += len(plan["missing"])
            done["approved"] += int(plan["approve"])
        elif plan["action"] == "APPROVE":
            store.approve(plan["url"], stamp)
            done["approved"] += 1
    return done


def _size(count):
    return f"{count / 1048576:.1f} MB".replace(".", ",") if count >= 1048576 else f"{round(count / 1024)} KB"


def _header(plan):
    if plan["kind"] == "proyecto":
        return f"{plan['group']} (proyecto, {plan['units']} unidades)"
    return f"{plan['group']} (unidad)" if plan["kind"] else plan["group"]


def render_report(plans, show_urls=False):
    lines, counts, current = [], {"UPLOAD": 0, "APPROVE": 0, "NO-OP": 0, "SKIP": 0, "REJECT": 0}, None
    for plan in plans:
        counts[plan["action"]] += 1
        if plan["group"] != current:
            current = plan["group"]
            lines.append(_header(plan))
        lines.append(f"  [{plan['action']:<7}] {plan['file']}")
        for error in plan["errors"]:
            lines.append(f"            ! {error}")
        if plan["action"] == "UPLOAD":
            (w0, h0), (w1, h1) = plan["original"], plan["size"]
            state = "aprobada" if plan["approve"] else "sin aprobar"
            units = f", en {len(plan['missing'])} unidades" if plan["kind"] == "proyecto" else ""
            lines.append(f"            · {w0}×{h0} → {w1}×{h1}, {_size(plan['bytes_in'])} → {_size(plan['bytes_out'])}, "
                         f"orden {plan['display_order']}, {state}{units}")
            lines.append(f"            · {plan['url'] if show_urls else plan['path']}")
        elif plan["action"] == "SKIP":
            lines.append(f"            · se omite: {plan['note']}")
        elif plan["action"] == "APPROVE":
            lines.append("            · ya subida; se marcará aprobada para el cliente")
        elif plan["action"] == "NO-OP":
            lines.append("            · ya subida y aprobada" if plan["approved"]
                         else "            · ya subida, pendiente de aprobar (--approve-media)")
    lines.append("")
    lines.append("  ".join(f"{action}={count}" for action, count in counts.items()))
    return "\n".join(lines), counts


def load_env_file(path):
    """KEY=VALUE lines from a local .env (kept out of Git) for variables not already set."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().removeprefix("export ").strip()
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def _store_from_env():
    url, key = os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")
    if not (url and key):
        raise SystemExit("Faltan SUPABASE_URL y SUPABASE_KEY (en el entorno o en un archivo .env junto al script)")
    return SupabaseMedia(url, key)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # the report carries accents; Windows consoles may not
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Sube fotos de inmuebles a Supabase Storage. Dry-run salvo --apply.")
    parser.add_argument("folder", help="carpeta con una subcarpeta por referencia o por proyecto (p. ej. fotos/MONET/)")
    parser.add_argument("--apply", action="store_true", help="sube y registra; sin esto no se escribe nada")
    parser.add_argument("--approve-media", action="store_true",
                        help="marca como aprobadas para el cliente las fotos de esta carpeta")
    parser.add_argument("--max-per-folder", type=int, default=None, metavar="N",
                        help="usa solo las primeras N fotos de cada carpeta (por orden de nombre)")
    args = parser.parse_args(argv)
    if args.max_per_folder is not None and args.max_per_folder < 1:
        parser.error("--max-per-folder debe ser 1 o más")
    if not os.path.isdir(args.folder):
        parser.error(f"no existe la carpeta {args.folder}")

    groups = collect(args.folder)
    if not groups:
        print("No hay fotos en la carpeta.")
        return 1
    for env_file in (os.path.join(os.getcwd(), ".env"), os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
        load_env_file(env_file)
    store = _store_from_env()
    targets = store.resolve([name for name, _ in groups])
    existing = {name: store.media_under(public_url(store.url, bucket_folder(name, target["kind"]) + "/"))
                for name, target in targets.items()}
    plans = plan_media(groups, targets, existing, store.url, args.approve_media, limit=args.max_per_folder)
    report, counts = render_report(plans, show_urls=args.apply)
    print(report)
    if not args.apply:
        print("\nDRY-RUN: no se subió ni se escribió nada. Repite con --apply para aplicarlo.")
        return 1 if counts["REJECT"] else 0
    if counts["REJECT"]:
        print("\nHay carpetas rechazadas: renómbralas como la referencia o el proyecto del inventario, o quítalas.")
        return 1
    result = {"uploaded": 0, "rows": 0, "approved": 0}
    try:
        apply_plans(store, plans, done=result)
    except requests.RequestException as error:
        response = getattr(error, "response", None)
        detail = f"HTTP {response.status_code}: {response.text[:200]}" if response is not None else type(error).__name__
        print(f"\nSe detuvo por un error de Supabase ({detail}).")
        print(f"Quedaron completas {result['uploaded']} subidas y {result['approved']} aprobaciones; "
              f"corrige el problema (¿existe el bucket '{BUCKET}'?, ver README) y vuelve a ejecutar.")
        return 1
    print(f"\nAplicado: {result['uploaded']} fotos subidas ({result['rows']} registros), "
          f"{result['approved']} aprobadas para el cliente.")
    if result["uploaded"] and not args.approve_media:
        print("Revisa las fotos en los enlaces de arriba y apruébalas con --apply --approve-media.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
