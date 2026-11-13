from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from solution.ledger.db import connect
from solution.ledger.ingest import (
    ingest_drop, load_dispositions, load_schemas, normalize_phone, parse_date,
    parse_decimal, parse_timestamp,
)
from solution.ledger.report import _bucket, generate_reports, quality_report
from solution.ledger.security import PiiBox, generate_key


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "assignment-a-data" / "schemas"


def test_phone_normalization():
    assert normalize_phone("+91 98765-43210") == "+919876543210"
    assert normalize_phone("09876543210") == "+919876543210"
    assert normalize_phone("91-98765-43210") == "+919876543210"
    assert normalize_phone("5876543210") is None
    assert normalize_phone("N/A") is None


def test_date_and_decimal_parsing():
    assert parse_date("01-Dec-2026") == "2026-12-01"
    assert parse_date("31/02/2026") is None
    assert parse_date("2026-10-05") == "2026-10-05"
    assert parse_decimal("₹12,345.675") == "12345.68"
    assert parse_decimal("Rs. 1200") == "1200.00"
    assert parse_decimal("-2") is None
    assert parse_decimal("1,23") is None


def test_bucket_precedence():
    assert _bucket([]) == "yet_to_reach_out"
    assert _bucket([{"terminal": True, "telephony_failed": True}]) == "completed"
    assert _bucket([{"terminal": False, "telephony_failed": True}, {"terminal": False, "telephony_failed": True}]) == "in_progress_telephony_failed"
    assert _bucket([{"terminal": False, "telephony_failed": True}, {"terminal": False, "telephony_failed": False}]) == "in_progress_dialed"


def test_timestamps_convert_to_same_utc_instant():
    utc = parse_timestamp("2026-10-05T04:30:00Z")
    offset = parse_timestamp("2026-10-05T10:00:00+05:30")
    naive = parse_timestamp("2026-10-05 10:00:00")
    assert utc == offset == naive
    assert parse_timestamp(naive).endswith("+00:00")


def _write_drop(root: Path, drop_name: str, *, lead: bool = False, events: list[dict] | None = None):
    drop = root / drop_name
    if lead:
        folder = drop / "leads" / "motor_renewal"
        folder.mkdir(parents=True)
        with (folder / "batch_001.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Insured Name", "Mobile No", "Policy No", "Premium"])
            writer.writerow(["Test Customer", "98765 43210", "POL-1", "Rs. 1,200"])
    if events is not None:
        folder = drop / "events"
        folder.mkdir(parents=True)
        with (folder / "events_0001.jsonl").open("w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")
    return drop


def test_orphans_resolve_after_later_lead_and_override_is_append_only(tmp_path):
    key = PiiBox(generate_key())
    schemas = load_schemas(SCHEMA_DIR)
    dispositions = load_dispositions(SCHEMA_DIR / "dispositions.csv")
    conn = connect(tmp_path / "db.sqlite")
    events = [
        {"event_id": "e1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-05T10:00:00", "phone": "+91 98765 43210"},
        {"event_id": "e2", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-05T10:01:00+05:30", "phone": "9876543210"},
        {"event_id": "e3", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-05T10:02:00+05:30", "phone": "9876543210", "payload": {"disposition_code": "renewed"}},
        {"event_id": "e4", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-05T11:02:00+05:30", "payload": {"disposition_code": "payment_link_sent"}},
    ]
    drop1 = _write_drop(tmp_path, "drop_1", events=events)
    drop2 = _write_drop(tmp_path, "drop_2", lead=True)
    ingest_drop(conn, drop1, schemas, dispositions, key)
    assert quality_report(conn, schemas)["orphan_events"] == 4
    ingest_drop(conn, drop2, schemas, dispositions, key)
    assert quality_report(conn, schemas)["orphan_events"] == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 4
    encrypted = conn.execute("SELECT fields_json,phone_cipher FROM leads").fetchone()
    assert "Test Customer" not in encrypted["fields_json"]
    assert "9876543210" not in encrypted["phone_cipher"]
    out = tmp_path / "reports"
    generate_reports(conn, out, schemas, dispositions)
    lead_report = list(csv.DictReader((out / "lead_report.csv").open(encoding="utf-8")))[0]
    assert lead_report["current_disposition"] == "payment_link_sent"
    assert lead_report["ever_success"] == "false"
    assert lead_report["connected_calls"] == "1"
    before = quality_report(conn, schemas)
    assert ingest_drop(conn, drop1, schemas, dispositions, key)["events_total"] == 0
    assert quality_report(conn, schemas) == before
    conn.close()


def test_duplicate_events_and_bad_rows_are_counted_once(tmp_path):
    key = PiiBox(generate_key())
    schemas = load_schemas(SCHEMA_DIR)
    dispositions = load_dispositions(SCHEMA_DIR / "dispositions.csv")
    conn = connect(tmp_path / "db.sqlite")
    drop = _write_drop(tmp_path, "drop_1", lead=True, events=[
        {"event_id": "same", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-05T10:00:00Z", "phone": "9876543210"},
        {"event_id": "same", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-05T10:00:00Z", "phone": "9876543210"},
    ])
    # Include a malformed line in the same event file.
    with (drop / "events" / "events_0001.jsonl").open("a", encoding="utf-8") as f:
        f.write("{bad json\n")
    ingest_drop(conn, drop, schemas, dispositions, key)
    q = quality_report(conn, schemas)
    assert q["events_total"] == 3
    assert q["duplicate_events_dropped"] == 1
    assert q["malformed_events"] == 1
    assert q["orphan_events"] == 0
    conn.close()


def test_duplicate_lead_merges_non_null_fields_and_keeps_original_batch(tmp_path):
    key = PiiBox(generate_key())
    schemas = load_schemas(SCHEMA_DIR)
    dispositions = load_dispositions(SCHEMA_DIR / "dispositions.csv")
    conn = connect(tmp_path / "db.sqlite")
    first = tmp_path / "drop_1" / "leads" / "motor_renewal"
    second = tmp_path / "drop_2" / "leads" / "motor_renewal"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    for path, name, reference, premium in [
        (first / "batch_001.csv", "Original", "POL-1", "1200"),
        (second / "batch_002.csv", "Updated", "POL-2", ""),
    ]:
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["customer_name", "phone", "policy_number", "premium_amount"])
            writer.writerow([name, "9876543210", reference, premium])
    ingest_drop(conn, tmp_path / "drop_1", schemas, dispositions, key)
    ingest_drop(conn, tmp_path / "drop_2", schemas, dispositions, key)
    lead = conn.execute("SELECT * FROM leads").fetchone()
    fields = json.loads(lead["fields_json"])
    assert lead["batch_id"] == "batch_001"
    assert lead["external_ref"] == "POL-2"
    assert key.decrypt(fields["customer_name"]) == "Updated"
    assert fields["premium_amount"] == "1200.00"
    assert quality_report(conn, schemas)["duplicate_lead_rows_merged"] == 1
    conn.close()
