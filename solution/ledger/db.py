from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS leads (
    lead_id INTEGER PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    phone_digest TEXT NOT NULL,
    phone_cipher TEXT NOT NULL,
    phone_last4 TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    external_ref TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    UNIQUE(workflow_id, phone_digest)
);
CREATE TABLE IF NOT EXISTS ingest_files (
    relative_path TEXT PRIMARY KEY,
    drop_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('lead','event'))
);
CREATE TABLE IF NOT EXISTS lead_rows (
    id INTEGER PRIMARY KEY,
    source_file TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    rejected_reason TEXT,
    merged_duplicate INTEGER NOT NULL DEFAULT 0,
    warnings INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    reason TEXT NOT NULL,
    source_file TEXT NOT NULL,
    UNIQUE(source_file, row_number)
);
CREATE TABLE IF NOT EXISTS event_files (
    relative_path TEXT PRIMARY KEY,
    events_total INTEGER NOT NULL,
    malformed_events INTEGER NOT NULL,
    duplicate_events_dropped INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_events (
    event_id TEXT PRIMARY KEY,
    workflow_id TEXT,
    call_id TEXT,
    event_type TEXT,
    occurred_at_utc TEXT,
    phone_digest TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK(status IN ('pending','linked','unknown')),
    lead_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_seen_phone ON seen_events(workflow_id, phone_digest, status);
CREATE INDEX IF NOT EXISTS idx_seen_call ON seen_events(workflow_id, call_id, status);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY REFERENCES seen_events(event_id),
    workflow_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    lead_id INTEGER NOT NULL REFERENCES leads(lead_id),
    event_type TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_call ON events(workflow_id, call_id, occurred_at_utc);
CREATE INDEX IF NOT EXISTS idx_events_lead ON events(lead_id, occurred_at_utc);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
