"""Load ARENZ inventory from a structured file.

Dry-run is the default: nothing reaches Supabase unless --apply is given, and even
then the importer only inserts and updates. It never deletes.

Availability is deliberately not renewed by running this tool. A property becomes
invisible INVENTORY_VERIFICATION_DAYS after its last availability_confirmed_at, so
refreshing that timestamp is a claim that someone checked the property is still
available. That claim needs an explicit --publish or --confirm-availability; an
ordinary attribute update leaves the timestamp exactly where it was.

Constants and canonicalization come from app.py on purpose. A second copy of the
eligibility rules here would drift from the ones the bot actually applies.
"""
import argparse, csv, json, os, sys
from datetime import datetime, timedelta, timezone

import requests

from app import (ALLOWED_CURRENCIES, INVENTORY_VERIFICATION_DAYS, canonical_property_type)

REQUIRED_FIELDS = ("public_reference", "operation", "property_type", "district", "price_amount", "currency")
ALLOWED_OPERATIONS = ("compra", "alquiler")
LIFECYCLE_STATES = ("draft", "pending_verification", "active_confirmed", "reserved", "sold_rented", "inactive")
VISIBLE_STATE = "active_confirmed"
TEXT_FIELDS = ("public_reference", "operation", "property_type", "district", "zone", "public_location_reference",
               "exact_address", "internal_owner_contact", "public_description", "source_reference",
               "lifecycle_state", "approved_by", "verified_by", "verification_notes")
INT_FIELDS = ("bedrooms", "parking_spaces")
NUMBER_FIELDS = ("price_amount", "bathrooms", "area_m2")
TIMESTAMP_FIELDS = ("approved_at", "availability_confirmed_at")
AVAILABILITY = "availability_confirmed_at"
# Only these may be rewritten by an ordinary update. Availability and approval are
# operator claims, not attributes, so they are absent by design.
UPDATABLE_FIELDS = tuple(f for f in TEXT_FIELDS if f != "public_reference") + INT_FIELDS + NUMBER_FIELDS + ("features", "source_name")
FRESHNESS_WARNING_DAYS = 2


def read_records(path):
    """Accept JSON (list, or an object with a "properties" list) and CSV alike."""
    suffix = os.path.splitext(path)[1].lower()
    with open(path, encoding="utf-8-sig", newline="") as handle:
        if suffix == ".csv":
            return [dict(row) for row in csv.DictReader(handle)]
        payload = json.load(handle)
    if isinstance(payload, dict):
        payload = payload.get("properties", payload.get("records", []))
    if not isinstance(payload, list):
        raise ValueError("the source must hold a list of properties")
    return [record for record in payload if isinstance(record, dict)]


def _as_number(value):
    if isinstance(value, bool):
        raise ValueError("not a number")
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip().replace(",", "")
    if not text:
        raise ValueError("empty")
    return float(text)


def _as_features(value):
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if text.startswith("["):
        parsed = json.loads(text)
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [part.strip() for part in text.split(";") if part.strip()]


def normalize_record(raw):
    """Trim, canonicalize and type-coerce. Unknown keys are dropped, never guessed."""
    record, errors = {}, []
    for field in TEXT_FIELDS + TIMESTAMP_FIELDS + ("source_name",):
        if field in raw and raw[field] is not None:
            text = str(raw[field]).strip()
            if text:
                record[field] = text
    for field in INT_FIELDS:
        if raw.get(field) not in (None, ""):
            try:
                record[field] = int(_as_number(raw[field]))
            except (ValueError, TypeError):
                errors.append(f"{field}: '{raw[field]}' is not a whole number")
    for field in NUMBER_FIELDS:
        if raw.get(field) not in (None, ""):
            try:
                record[field] = _as_number(raw[field])
            except (ValueError, TypeError):
                errors.append(f"{field}: '{raw[field]}' is not a number")
    if "features" in raw:
        try:
            record["features"] = _as_features(raw["features"])
        except (ValueError, TypeError, json.JSONDecodeError):
            errors.append("features: expected a list or a ';'-separated text")
    if "operation" in record:
        record["operation"] = record["operation"].casefold()
    if "currency" in raw and str(raw["currency"]).strip():
        record["currency"] = str(raw["currency"]).strip().upper()
    if "property_type" in record:
        # The bot compares the stored value directly before canonicalizing, so an
        # 'apartamento' left as-is would never match a 'departamento' search.
        record["property_type"] = canonical_property_type(record["property_type"])
    return record, errors


def validate_record(record, confirm_only=False):
    """Every rule here is one the database or the matcher enforces downstream.

    A weekly reconfirmation only names properties that already exist, so it is not
    asked for a full listing; demanding one would push operators to retype a whole
    file just to say "still available".
    """
    if confirm_only:
        return [] if record.get("public_reference") else ["public_reference: required"]
    # `not in record`, not falsiness: a price of 0 was supplied and is wrong for a
    # different reason, and reporting it as missing sends the operator hunting.
    errors = [f"{field}: required" for field in REQUIRED_FIELDS if field not in record]
    if record.get("operation") and record["operation"] not in ALLOWED_OPERATIONS:
        errors.append(f"operation: '{record['operation']}' is not {' or '.join(ALLOWED_OPERATIONS)}")
    if record.get("currency") and record["currency"] not in ALLOWED_CURRENCIES:
        errors.append(f"currency: '{record['currency']}' is not one of {sorted(ALLOWED_CURRENCIES)}")
    if "price_amount" in record and record["price_amount"] <= 0:
        errors.append("price_amount: must be greater than 0")
    if "area_m2" in record and record["area_m2"] <= 0:
        errors.append("area_m2: must be greater than 0")
    for field in INT_FIELDS:
        if field in record and record[field] < 0:
            errors.append(f"{field}: cannot be negative")
    if "bathrooms" in record and record["bathrooms"] < 0:
        errors.append("bathrooms: cannot be negative")
    if record.get("lifecycle_state") and record["lifecycle_state"] not in LIFECYCLE_STATES:
        errors.append(f"lifecycle_state: '{record['lifecycle_state']}' is not a known state")
    for field in TIMESTAMP_FIELDS:
        if field in record and parse_timestamp(record[field]) is None:
            errors.append(f"{field}: '{record[field]}' is not an ISO-8601 timestamp")
    if record.get("lifecycle_state") == VISIBLE_STATE and not (record.get("approved_at") and record.get(AVAILABILITY)):
        errors.append(f"lifecycle_state: '{VISIBLE_STATE}' also needs approved_at and {AVAILABILITY}")
    return errors


def parse_timestamp(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def visibility_gaps(record):
    """What still keeps a valid property from ever reaching a client."""
    gaps = []
    if record.get("lifecycle_state") != VISIBLE_STATE:
        gaps.append(f"lifecycle_state is '{record.get('lifecycle_state', 'draft')}', not '{VISIBLE_STATE}'")
    if not record.get("approved_at"):
        gaps.append("approved_at is empty")
    if not record.get(AVAILABILITY):
        gaps.append(f"{AVAILABILITY} is empty")
    return gaps


def freshness_status(row, now=None):
    """vigente / por_vencer / vencida, by the same window the matcher applies."""
    now = now or datetime.now(timezone.utc)
    confirmed = parse_timestamp(row.get(AVAILABILITY)) if row.get(AVAILABILITY) else None
    if confirmed is None:
        return "vencida"
    expires_at = confirmed + timedelta(days=INVENTORY_VERIFICATION_DAYS)
    if expires_at <= now:
        return "vencida"
    return "por_vencer" if expires_at - now <= timedelta(days=FRESHNESS_WARNING_DAYS) else "vigente"


def _changed_fields(record, existing):
    changes = {}
    for field in UPDATABLE_FIELDS:
        if field not in record:
            continue
        current = existing.get(field)
        proposed = record[field]
        if field in NUMBER_FIELDS and current is not None:
            try:
                if float(current) == float(proposed):
                    continue
            except (TypeError, ValueError):
                pass
        elif current == proposed:
            continue
        changes[field] = proposed
    return changes


def plan_records(records, existing_by_reference, now=None, publish=False, confirm_availability=False, source_name=None):
    """Decide INSERT / UPDATE / NO-OP / REJECT per record without touching anything."""
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()
    plans, seen = [], {}
    for position, raw in enumerate(records, start=1):
        record, errors = normalize_record(raw)
        if source_name and "source_name" not in record:
            record["source_name"] = source_name
        errors = errors + validate_record(record, confirm_only=confirm_availability)
        reference = record.get("public_reference")
        if reference and reference in seen:
            errors.append(f"public_reference: duplicated inside the source, first seen at row {seen[reference]}")
        elif reference:
            seen[reference] = position
        if errors:
            plans.append({"row": position, "reference": reference, "action": "REJECT",
                          "errors": errors, "changes": {}, "record": record, "gaps": []})
            continue
        existing = existing_by_reference.get(reference)
        if confirm_availability and existing is None:
            plans.append({"row": position, "reference": reference, "action": "REJECT", "changes": {}, "gaps": [],
                          "errors": ["reconfirmación: no existe ese public_reference; reconfirmar no da de alta"],
                          "record": record})
            continue
        if publish:
            # An operator asserting, right now, that this property is verified and
            # available. Never inferred from the file merely being imported.
            record["lifecycle_state"] = VISIBLE_STATE
            record.setdefault("approved_at", stamp)
            record[AVAILABILITY] = stamp
        if existing is None:
            plans.append({"row": position, "reference": reference, "action": "INSERT", "errors": [],
                          "changes": dict(record), "record": record, "gaps": visibility_gaps(record)})
            continue
        if confirm_availability:
            # The weekly reconfirmation writes availability and nothing else, so it
            # can never smuggle an attribute change in behind a "still available".
            changes = {AVAILABILITY: record.get(AVAILABILITY, stamp)}
        else:
            changes = _changed_fields(record, existing)
            if publish:
                for field in ("lifecycle_state", "approved_at", AVAILABILITY):
                    if record.get(field) and record[field] != existing.get(field):
                        changes[field] = record[field]
        merged = {**existing, **changes}
        action = "UPDATE" if changes else "NO-OP"
        plans.append({"row": position, "reference": reference, "action": action, "errors": [],
                      "changes": changes, "record": record, "gaps": visibility_gaps(merged)})
    return plans


class SupabaseInventory:
    """Read and write inventory_properties over the same REST surface app.py uses."""

    columns = ("property_id,public_reference,source_reference,operation,property_type,district,zone,"
               "public_location_reference,exact_address,internal_owner_contact,price_amount,currency,bedrooms,"
               "bathrooms,area_m2,parking_spaces,features,public_description,lifecycle_state,"
               "availability_confirmed_at,verified_by,approved_at,approved_by,verification_notes,source_name")

    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def by_reference(self, references):
        if not references:
            return {}
        quoted = ",".join('"%s"' % reference.replace('"', '') for reference in references)
        response = requests.get(f"{self.url}/rest/v1/inventory_properties", headers=self.headers,
                                params={"select": self.columns, "public_reference": f"in.({quoted})"}, timeout=20)
        response.raise_for_status()
        return {row["public_reference"]: row for row in response.json()}

    def all_rows(self):
        response = requests.get(f"{self.url}/rest/v1/inventory_properties", headers=self.headers,
                                params={"select": self.columns, "order": "public_reference.asc"}, timeout=30)
        response.raise_for_status()
        return response.json()

    def insert(self, rows):
        response = requests.post(f"{self.url}/rest/v1/inventory_properties",
                                 headers={**self.headers, "Prefer": "return=minimal"}, json=rows, timeout=30)
        response.raise_for_status()

    def update(self, reference, changes):
        response = requests.patch(f"{self.url}/rest/v1/inventory_properties",
                                  headers={**self.headers, "Prefer": "return=minimal"},
                                  params={"public_reference": f"eq.{reference}"},
                                  json={**changes, "updated_at": datetime.now(timezone.utc).isoformat()}, timeout=30)
        response.raise_for_status()


def apply_plans(store, plans):
    """Insert and update only. Rejected and unchanged records are left alone."""
    inserts = [plan["record"] for plan in plans if plan["action"] == "INSERT"]
    if inserts:
        store.insert(inserts)
    for plan in plans:
        if plan["action"] == "UPDATE":
            store.update(plan["reference"], plan["changes"])
    return {"inserted": len(inserts), "updated": sum(1 for plan in plans if plan["action"] == "UPDATE")}


def render_report(plans):
    lines, counts = [], {"INSERT": 0, "UPDATE": 0, "NO-OP": 0, "REJECT": 0}
    for plan in plans:
        counts[plan["action"]] += 1
        lines.append(f"[{plan['action']:<6}] fila {plan['row']}  {plan['reference'] or '(sin public_reference)'}")
        for error in plan["errors"]:
            lines.append(f"           ! {error}")
        for field, value in sorted(plan["changes"].items()):
            lines.append(f"           · {field} = {value!r}")
        for gap in plan["gaps"]:
            lines.append(f"           ~ no será visible: {gap}")
    lines.append("")
    lines.append("  ".join(f"{action}={count}" for action, count in counts.items()))
    return "\n".join(lines), counts


def _store_from_env():
    url, key = os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")
    if not (url and key):
        raise SystemExit("SUPABASE_URL and SUPABASE_KEY must be set")
    return SupabaseInventory(url, key)


def main(argv=None):
    # This machine has already lost a script to the Windows console codepage;
    # the report carries accents, so force UTF-8 where the stream allows it.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Load ARENZ inventory. Dry-run unless --apply.")
    parser.add_argument("source", nargs="?", help="path to a .json or .csv file of properties")
    parser.add_argument("--apply", action="store_true", help="actually write; without it nothing is sent")
    parser.add_argument("--publish", action="store_true",
                        help="assert these properties are verified and available right now")
    parser.add_argument("--confirm-availability", action="store_true",
                        help="refresh availability only, for properties that already exist")
    parser.add_argument("--source-name", default=None, help="where these listings came from, for traceability")
    parser.add_argument("--freshness", action="store_true", help="report inventory freshness and exit")
    args = parser.parse_args(argv)

    if args.freshness:
        rows = _store_from_env().all_rows()
        buckets = {"vigente": [], "por_vencer": [], "vencida": []}
        for row in rows:
            buckets[freshness_status(row)].append(row)
        for status in ("vencida", "por_vencer", "vigente"):
            print(f"{status}: {len(buckets[status])}")
            for row in buckets[status]:
                print(f"    {row['public_reference']}  confirmada: {row.get(AVAILABILITY) or '(nunca)'}")
        return 0

    if not args.source:
        parser.error("a source file is required unless --freshness is used")
    records = read_records(args.source)
    store = _store_from_env()
    references = [str(record.get("public_reference", "")).strip() for record in records]
    existing = store.by_reference([reference for reference in references if reference])
    plans = plan_records(records, existing, publish=args.publish,
                         confirm_availability=args.confirm_availability, source_name=args.source_name)
    report, counts = render_report(plans)
    print(report)
    if not args.apply:
        print("\nDRY-RUN: no se escribió nada. Repite con --apply para aplicarlo.")
        return 1 if counts["REJECT"] else 0
    if counts["REJECT"]:
        print("\nHay registros rechazados: corrige la fuente antes de aplicar.")
        return 1
    result = apply_plans(store, plans)
    print(f"\nAplicado: {result['inserted']} altas, {result['updated']} actualizaciones.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
