from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from .security import PiiBox

IST = timezone(timedelta(hours=5, minutes=30))


def load_schemas(schema_dir: Path) -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(schema_dir.glob("*.json"))}


def load_dispositions(path: Path) -> dict[str, dict[str, dict[str, bool]]]:
    result: dict[str, dict[str, dict[str, bool]]] = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            result.setdefault(row["workflow_id"], {})[row["code"]] = {
                "is_terminal": row["is_terminal"].strip().lower() == "true",
                "is_success": row["is_success"].strip().lower() == "true",
            }
    return result


def normalize_header(value: str) -> str:
    return re.sub(r"[_\s]+", " ", value.strip().lower())


def normalize_phone(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] not in "6789":
        return None
    return "+91" + digits


def parse_date(value: str) -> str | None:
    s = value.strip()
    patterns = [
        (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
        (r"\d{2}/\d{2}/\d{4}", "%d/%m/%Y"),
        (r"\d{2}-\d{2}-\d{4}", "%d-%m-%Y"),
        (r"\d{2}-[A-Za-z]{3}-\d{4}", "%d-%b-%Y"),
    ]
    for regex, fmt in patterns:
        if re.fullmatch(regex, s):
            try:
                return datetime.strptime(s.title() if "%b" in fmt else s, fmt).date().isoformat()
            except ValueError:
                return None
    return None


def parse_decimal(value: str) -> str | None:
    s = value.strip()
    s = re.sub(r"^(?:₹|rs\.?|inr)\s*", "", s, flags=re.IGNORECASE)
    if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", s):
        return None
    try:
        n = Decimal(s.replace(",", ""))
        if n < 0:
            return None
        return format(n.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")
    except InvalidOperation:
        return None


def normalize_value(field: dict, value: str) -> tuple[object | None, bool]:
    """Return normalized value and whether a present value was invalid."""
    raw = value.strip()
    if not raw:
        return None, False
    kind = field["type"]
    if kind == "phone":
        normalized = normalize_phone(raw)
        return normalized, normalized is None
    if kind == "date":
        normalized = parse_date(raw)
        return normalized, normalized is None
    if kind == "decimal":
        normalized = parse_decimal(raw)
        return normalized, normalized is None
    if kind == "enum":
        choices = {str(v).lower() for v in field.get("values", [])}
        normalized = raw.lower()
        return (normalized if normalized in choices else None), normalized not in choices
    return raw, False


def parse_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        s = value.strip()
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s[:-1] + "+00:00")
        else:
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, TypeError):
        return None


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_key(drop: Path, path: Path) -> tuple[str, str]:
    return f"{drop.name}/{path.relative_to(drop).as_posix()}", drop.name


def ingest_drop(conn: sqlite3.Connection, drop_path: Path, schemas: dict, dispositions: dict,
                pii: PiiBox) -> dict[str, int]:
    drop = drop_path.resolve()
    if not drop.is_dir():
        raise ValueError(f"Drop directory not found: {drop}")
    metric = {"lead_rows_total": 0, "lead_rows_rejected": 0, "duplicate_lead_rows_merged": 0,
              "field_warnings": 0, "events_total": 0, "malformed_events": 0,
              "duplicate_events_dropped": 0}
    with conn:
        lead_paths = sorted((drop / "leads").glob("*/*.csv"))
        for path in lead_paths:
            rel, drop_id = _file_key(drop, path)
            digest = _file_hash(path)
            prior = conn.execute("SELECT content_sha256 FROM ingest_files WHERE relative_path=?", (rel,)).fetchone()
            if prior:
                if prior["content_sha256"] != digest:
                    raise ValueError(f"Previously ingested file changed on disk: {rel}; use a fresh database for changed input")
                continue
            workflow = path.parent.name
            if workflow not in schemas:
                raise ValueError(f"No schema for workflow {workflow!r}")
            _ingest_lead_file(conn, drop, path, rel, drop_id, workflow, schemas[workflow], dispositions, pii, metric)
            conn.execute("INSERT INTO ingest_files VALUES (?,?,?, 'lead')", (rel, drop_id, digest))

        event_paths = sorted((drop / "events").glob("*.jsonl"))
        for path in event_paths:
            rel, _ = _file_key(drop, path)
            digest = _file_hash(path)
            prior = conn.execute("SELECT content_sha256 FROM ingest_files WHERE relative_path=?", (rel,)).fetchone()
            if prior:
                if prior["content_sha256"] != digest:
                    raise ValueError(f"Previously ingested file changed on disk: {rel}; use a fresh database for changed input")
                continue
            _ingest_event_file(conn, path, rel, dispositions, pii, metric)
            conn.execute("INSERT INTO ingest_files VALUES (?,?,?, 'event')", (rel, drop.name, digest))
    return metric


def _ingest_lead_file(conn, drop, path, rel, drop_id, workflow, schema, dispositions, pii, metric):
    fields = schema["fields"]
    aliases: dict[str, str] = {}
    for field in fields:
        for name in [field["name"], *field.get("aliases", [])]:
            aliases[normalize_header(name)] = field["name"]
    batch = path.stem
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        mapped_headers = {h: aliases.get(normalize_header(h)) for h in (reader.fieldnames or [])}
        row_number = 0
        for row in reader:
            if not row or all(not (v or "").strip() for v in row.values()):
                continue
            row_number += 1
            metric["lead_rows_total"] += 1
            values_by_name: dict[str, str] = {}
            for header, value in row.items():
                key = mapped_headers.get(header)
                if key and key not in values_by_name:
                    values_by_name[key] = value or ""
            clean: dict[str, object | None] = {}
            warnings = 0
            reason = None
            phone = None
            for field in fields:
                name = field["name"]
                raw = values_by_name.get(name, "").strip()
                normalized, invalid = normalize_value(field, raw)
                if field["type"] == "phone":
                    phone = normalized
                if field.get("required") and normalized is None:
                    if field["type"] == "phone" and raw:
                        reason = "invalid_phone"
                    else:
                        reason = f"missing_required:{name}"
                    break
                if invalid:
                    if field["type"] == "phone":
                        reason = "invalid_phone"
                        break
                    warnings += 1
                clean[name] = normalized
            if reason:
                metric["lead_rows_rejected"] += 1
                conn.execute("INSERT INTO lead_rows(source_file,workflow_id,batch_id,row_number,rejected_reason) VALUES(?,?,?,?,?)",
                             (rel, workflow, batch, row_number, reason))
                conn.execute("INSERT INTO rejections(workflow_id,batch_id,row_number,reason,source_file) VALUES(?,?,?,?,?)",
                             (workflow, batch, row_number, reason, rel))
                continue
            metric["field_warnings"] += warnings
            duplicate = _upsert_lead(conn, workflow, batch, clean, fields, pii, dispositions)
            metric["duplicate_lead_rows_merged"] += duplicate
            conn.execute("INSERT INTO lead_rows(source_file,workflow_id,batch_id,row_number,merged_duplicate,warnings) VALUES(?,?,?,?,?,?)",
                         (rel, workflow, batch, row_number, duplicate, warnings))


def _upsert_lead(conn, workflow, batch, clean, fields, pii, dispositions) -> int:
    phone = clean.get("phone")
    digest = pii.phone_digest(phone)
    existing = conn.execute("SELECT * FROM leads WHERE workflow_id=? AND phone_digest=?", (workflow, digest)).fetchone()
    if existing:
        current = json.loads(existing["fields_json"])
        for field in fields:
            name = field["name"]
            prior = current.get(name)
            if field.get("pii") and prior is not None:
                prior = pii.decrypt(prior)
            new = clean.get(name)
            if new is not None:
                current[name] = new
            elif prior is not None:
                current[name] = prior
            else:
                current[name] = field.get("default")
        encrypted = {f["name"]: (pii.encrypt(str(current[f["name"]])) if f.get("pii") and current.get(f["name"]) is not None else current.get(f["name"])) for f in fields}
        reference = next(f["name"] for f in fields if f.get("reference"))
        conn.execute("UPDATE leads SET external_ref=?,fields_json=? WHERE lead_id=?",
                     (str(current.get(reference) or ""), json.dumps(encrypted, ensure_ascii=False), existing["lead_id"]))
        return 1
    for field in fields:
        name = field["name"]
        if clean.get(name) is None:
            clean[name] = field.get("default")
    encrypted = {f["name"]: (pii.encrypt(str(clean[f["name"]])) if f.get("pii") and clean.get(f["name"]) is not None else clean.get(f["name"])) for f in fields}
    reference = next(f["name"] for f in fields if f.get("reference"))
    lead_id = conn.execute("INSERT INTO leads(workflow_id,phone_digest,phone_cipher,phone_last4,batch_id,external_ref,fields_json) VALUES(?,?,?,?,?,?,?)",
        (workflow, digest, pii.encrypt(phone), phone[-4:], batch, str(clean.get(reference) or ""), json.dumps(encrypted, ensure_ascii=False))).lastrowid
    # Resolve anything that arrived before this lead.
    _resolve_pending(conn, dispositions, workflow_id=workflow, phone_digest=digest)
    return 0


def _ingest_event_file(conn, path, rel, dispositions, pii, metric):
    total = malformed = duplicate = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            total += 1
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict) or not obj.get("event_id"):
                    raise ValueError("event_id missing")
                occurred = parse_timestamp(obj.get("occurred_at"))
                if occurred is None:
                    raise ValueError("occurred_at missing or invalid")
            except (json.JSONDecodeError, ValueError, TypeError):
                malformed += 1
                continue
            event_id = str(obj["event_id"])
            if conn.execute("SELECT 1 FROM seen_events WHERE event_id=?", (event_id,)).fetchone():
                duplicate += 1
                continue
            workflow = obj.get("workflow_id")
            call_id = obj.get("call_id")
            event_type = obj.get("event_type")
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            phone_digest = None
            if obj.get("phone"):
                canonical = normalize_phone(str(obj["phone"]))
                if canonical:
                    phone_digest = pii.phone_digest(canonical)
            conn.execute("INSERT INTO seen_events(event_id,workflow_id,call_id,event_type,occurred_at_utc,phone_digest,payload_json,status) VALUES(?,?,?,?,?,?,?,'pending')",
                         (event_id, workflow, call_id, event_type, occurred, phone_digest, json.dumps(payload, ensure_ascii=False)))
            _resolve_pending(conn, dispositions, event_id=event_id)
    conn.execute("INSERT INTO event_files VALUES(?,?,?,?)", (rel, total, malformed, duplicate))
    metric["events_total"] += total
    metric["malformed_events"] += malformed
    metric["duplicate_events_dropped"] += duplicate


def _resolve_pending(conn, dispositions, *, event_id=None, workflow_id=None, phone_digest=None):
    """Resolve only the event just received or those matching a newly arrived lead."""
    if event_id is not None:
        candidates = conn.execute("SELECT * FROM seen_events WHERE status='pending' AND event_id=?", (event_id,)).fetchall()
    elif phone_digest is not None:
        candidates = conn.execute("SELECT * FROM seen_events WHERE status='pending' AND workflow_id=? AND phone_digest=? ORDER BY event_id",
                                  (workflow_id, phone_digest)).fetchall()
    else:
        return
    linked_calls = set()
    for event in candidates:
        if not event["workflow_id"] or not event["call_id"] or not event["event_type"] or not event["occurred_at_utc"]:
            continue
        if event["event_type"] == "disposition.override":
            lead = conn.execute("SELECT lead_id FROM seen_events WHERE workflow_id=? AND call_id=? AND status IN ('linked','unknown') AND lead_id IS NOT NULL ORDER BY event_id LIMIT 1",
                                (event["workflow_id"], event["call_id"])).fetchone()
        elif event["phone_digest"]:
            lead = conn.execute("SELECT lead_id FROM leads WHERE workflow_id=? AND phone_digest=?",
                                (event["workflow_id"], event["phone_digest"])).fetchone()
        else:
            lead = None
        if not lead:
            continue
        payload = json.loads(event["payload_json"])
        unknown = False
        if event["event_type"].startswith("disposition."):
            code = payload.get("disposition_code")
            unknown = code not in dispositions.get(event["workflow_id"], {})
        status = "unknown" if unknown else "linked"
        conn.execute("UPDATE seen_events SET status=?,lead_id=? WHERE event_id=?", (status, lead["lead_id"], event["event_id"]))
        if not unknown:
            conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                         (event["event_id"], event["workflow_id"], event["call_id"], lead["lead_id"], event["event_type"], event["occurred_at_utc"], event["payload_json"]))
        linked_calls.add((event["workflow_id"], event["call_id"]))
    # An override can arrive before the phone-bearing events for its call.
    for wf, call_id in linked_calls:
        overrides = conn.execute("SELECT event_id FROM seen_events WHERE status='pending' AND workflow_id=? AND call_id=? AND event_type='disposition.override' ORDER BY event_id",
                                 (wf, call_id)).fetchall()
        for override in overrides:
            _resolve_pending(conn, dispositions, event_id=override["event_id"])
