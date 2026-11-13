from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _write_csv(path: Path, header: list[str], rows: list[list[object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def generate_reports(conn: sqlite3.Connection, out_dir: Path, schemas: dict,
                     dispositions: dict) -> dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    leads = conn.execute("SELECT * FROM leads ORDER BY workflow_id, external_ref").fetchall()
    rows = conn.execute("SELECT * FROM events ORDER BY occurred_at_utc,event_id").fetchall()
    grouped: dict[tuple[int, str], list[sqlite3.Row]] = defaultdict(list)
    for event in rows:
        grouped[(event["lead_id"], event["call_id"])].append(event)

    calls_by_lead: dict[int, list[dict]] = defaultdict(list)
    for (lead_id, call_id), events in grouped.items():
        initiated = [e for e in events if e["event_type"] == "call.initiated"]
        if not initiated:
            continue
        start = max(initiated, key=lambda e: (_dt(e["occurred_at_utc"]), e["event_id"]))
        sorted_events = sorted(events, key=lambda e: (_dt(e["occurred_at_utc"]), e["event_id"]))
        overrides = [e for e in sorted_events if e["event_type"] == "disposition.override"]
        sets = [e for e in sorted_events if e["event_type"] == "disposition.set"]
        disposition_event = (overrides or sets)
        disposition_event = disposition_event[-1] if disposition_event else None
        code = json.loads(disposition_event["payload_json"]).get("disposition_code") if disposition_event else None
        wf = start["workflow_id"]
        calls_by_lead[lead_id].append({
            "call_id": call_id,
            "start": _dt(start["occurred_at_utc"]),
            "connected": any(e["event_type"] == "call.connected" for e in events),
            "telephony_failed": any(e["event_type"] == "call.telephony_failed" for e in events),
            "disposition": code,
            "success": bool(code and dispositions.get(wf, {}).get(code, {}).get("is_success")),
            "terminal": bool(code and dispositions.get(wf, {}).get(code, {}).get("is_terminal")),
        })
    for calls in calls_by_lead.values():
        calls.sort(key=lambda c: (c["start"], c["call_id"]))

    batch_counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for lead in leads:
        bucket = _bucket(calls_by_lead.get(lead["lead_id"], []))
        batch_counts[(lead["workflow_id"], lead["batch_id"])]["total_leads"] += 1
        batch_counts[(lead["workflow_id"], lead["batch_id"])][bucket] += 1
    batch_header = ["workflow_id", "batch_id", "total_leads", "yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"]
    batch_rows = [[wf, batch, counts["total_leads"], counts["yet_to_reach_out"], counts["in_progress_telephony_failed"], counts["in_progress_dialed"], counts["completed"]]
                  for (wf, batch), counts in sorted(batch_counts.items())]
    _write_csv(out_dir / "batch_summary.csv", batch_header, batch_rows)

    daily: dict[tuple[str, str], dict[str, object]] = defaultdict(lambda: {"total": 0, "connected": 0, "success": set()})
    for lead in leads:
        for call in calls_by_lead.get(lead["lead_id"], []):
            day = call["start"].astimezone(IST).date().isoformat()
            stat = daily[(lead["workflow_id"], day)]
            stat["total"] += 1
            stat["connected"] += int(call["connected"])
            if call["success"]:
                stat["success"].add(lead["lead_id"])
    daily_header = ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"]
    daily_rows = []
    for (wf, day), stat in sorted(daily.items()):
        total = stat["total"]
        daily_rows.append([wf, day, total, stat["connected"], f"{stat['connected'] / total:.4f}", len(stat["success"])])
    _write_csv(out_dir / "daily_overview.csv", daily_header, daily_rows)

    lead_header = ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls", "connected_calls", "last_call_at", "current_disposition", "ever_success"]
    lead_rows = []
    for lead in sorted(leads, key=lambda l: (l["workflow_id"], l["external_ref"])):
        calls = calls_by_lead.get(lead["lead_id"], [])
        latest_start = max(calls, key=lambda c: c["start"]) if calls else None
        disposition_calls = [c for c in calls if c["disposition"]]
        current = max(disposition_calls, key=lambda c: c["start"])["disposition"] if disposition_calls else ""
        last_at = latest_start["start"].astimezone(IST).isoformat(timespec="seconds") if latest_start else ""
        lead_rows.append([lead["workflow_id"], lead["external_ref"], lead["batch_id"], "+91******" + lead["phone_last4"],
                          _bucket(calls), len(calls), sum(c["connected"] for c in calls), last_at, current,
                          "true" if any(c["success"] for c in calls) else "false"])
    _write_csv(out_dir / "lead_report.csv", lead_header, lead_rows)

    rejects = conn.execute("SELECT workflow_id,batch_id,row_number,reason FROM rejections ORDER BY workflow_id,batch_id,row_number").fetchall()
    _write_csv(out_dir / "rejections.csv", ["workflow_id", "batch_id", "row_number", "reason"],
               [[r["workflow_id"], r["batch_id"], r["row_number"], r["reason"]] for r in rejects])
    quality = quality_report(conn, schemas)
    (out_dir / "data_quality.json").write_text(json.dumps(quality, indent=2) + "\n", encoding="utf-8")
    return quality


def _bucket(calls: list[dict]) -> str:
    if not calls:
        return "yet_to_reach_out"
    if any(c["terminal"] for c in calls):
        return "completed"
    if all(c["telephony_failed"] for c in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"


def quality_report(conn: sqlite3.Connection, schemas: dict) -> dict[str, object]:
    row = conn.execute("SELECT COUNT(*) total, SUM(rejected_reason IS NOT NULL) rejected, SUM(merged_duplicate) duplicates, SUM(warnings) warnings FROM lead_rows").fetchone()
    reasons = {r["reason"]: r["n"] for r in conn.execute("SELECT reason,COUNT(*) n FROM rejections GROUP BY reason ORDER BY reason")}
    totals = conn.execute("SELECT COALESCE(SUM(events_total),0) total,COALESCE(SUM(malformed_events),0) malformed,COALESCE(SUM(duplicate_events_dropped),0) duplicates FROM event_files").fetchone()
    return {
        "lead_rows_total": row["total"] or 0,
        "lead_rows_rejected": row["rejected"] or 0,
        "rejections_by_reason": reasons,
        "duplicate_lead_rows_merged": row["duplicates"] or 0,
        "field_warnings": row["warnings"] or 0,
        "events_total": totals["total"],
        "malformed_events": totals["malformed"],
        "duplicate_events_dropped": totals["duplicates"],
        "orphan_events": conn.execute("SELECT COUNT(*) FROM seen_events WHERE status='pending'").fetchone()[0],
        "unknown_disposition_events": conn.execute("SELECT COUNT(*) FROM seen_events WHERE status='unknown'").fetchone()[0],
    }
