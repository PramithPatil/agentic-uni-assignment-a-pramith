# Assignment A: Interaction Ledger & Reporting Pipeline

## Design

The implementation uses Python's standard-library CSV/JSON/SQLite support, plus
`cryptography` for authenticated encryption. The CLI lives in `solution/ledger`:

- `ingest.py` normalizes and validates lead rows, ingests event lines, and resolves
  events that were initially orphaned.
- `db.py` creates the SQLite schema.
- `security.py` encrypts PII with Fernet and derives a keyed HMAC phone index.
- `report.py` derives call metrics from linked ledger events and writes the five reports.
- `__main__.py` exposes `keygen`, `ingest`, and `report` commands.

The `events` table contains linked, known events and is append-only. `seen_events`
tracks event IDs for deduplication and holds unresolved or unknown events. When a
lead arrives, pending events with the matching workflow and keyed phone digest are
linked; overrides are resolved by workflow and call ID. Reports only include events
in the ledger.

SQLite tables include `leads`, `events`, `seen_events`, `lead_rows`, `rejections`,
`ingest_files`, and `event_files`. Phone numbers and schema fields marked PII are
encrypted in the database. A keyed HMAC supports exact phone matching without
storing a plaintext phone index. The last four digits are stored separately to
produce the required masked phone report. The encryption key is stored outside the
database and must be backed up securely.

## Setup and run (PowerShell)

Run from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m solution.ledger keygen --out .pii.key
python assignment-a-data\tools\generate_data.py --seed 42 --scale small --out data
python -m solution.ledger ingest data\drop_1 --db solution\ledger.sqlite --key-file .pii.key
python -m solution.ledger ingest data\drop_2 --db solution\ledger.sqlite --key-file .pii.key
python -m solution.ledger report --db solution\ledger.sqlite --key-file .pii.key --out reports
python -m pytest -q
```

You can also ingest both drops in name order with one command:

```powershell
python -m solution.ledger ingest data --db solution\ledger.sqlite --key-file .pii.key
```

Reingesting an unchanged file is a no-op. If a previously ingested file changes,
ingestion stops with an error; use a fresh database for a new input set or seed.

Validation was run with Python 3.15.2, the latest stable release available during
development, and the project's full `requirements.txt` installed.

## Decisions and assumptions

- SQLite is used because the assignment accepts it and it keeps setup local. The same
  logical schema can be ported to PostgreSQL.
- Dates are stored as ISO calendar dates. Timestamps are stored in UTC and converted
  to IST for report dates and display.
- A JSON event with an invalid or missing timestamp is counted as malformed because
  it cannot be ordered or included safely in derived facts.
- `rejections_by_reason` contains only reasons observed in the ingested data.
- Equal-time disposition events are ordered by event ID as a deterministic tie-break.
- File identity is based on drop name and relative path. Changed contents at an
  already-ingested identity are rejected rather than silently skipped.

## Scope

Core ingestion, reports, PII protection, idempotency, and focused tests are included.
Large-scale performance, SQL-side report aggregation, and production scheduling are
not implemented as stretch goals.
