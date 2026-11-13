# Submission

> Replace everything in _italics_. Be concise: bullet points are fine. We read every word.

## Candidate
- **Name:** Pramith Patil
- **Assignment chosen:** A (Data)
- **Time spent:** 4 hours

## How to run
Run this PowerShell block from the repository root with Python 3.11+ available as `python`.

```powershell
python -m pip install -r requirements.txt
if (-not (Test-Path .pii.key)) { python -m solution.ledger keygen --out .pii.key }
python assignment-a-data\tools\generate_data.py --seed 42 --scale small --out data
python -m solution.ledger ingest data\drop_1 --db solution\ledger.sqlite --key-file .pii.key
python -m solution.ledger ingest data\drop_2 --db solution\ledger.sqlite --key-file .pii.key
python -m solution.ledger report --db solution\ledger.sqlite --key-file .pii.key --out reports
python -m pytest -q
```

## Design
I built a Python command-line program that reads customer lists and call-event files,
saves their cleaned data in SQLite, and creates the reports. It keeps call events as a
history: when a supervisor corrects an outcome, the correction is added as a new event
instead of replacing the original. Reports are calculated from that history.

The program checks and cleans each customer row, merges repeat phone numbers within
the same workflow, and keeps the batch where the lead first appeared. If a call event
arrives before its lead, it waits in the database and links when the lead arrives.
Names and phone numbers are encrypted; a keyed lookup value lets the program match
phones without storing a searchable plaintext number. File and event IDs are recorded
so running ingestion again does not add duplicates. See `solution/README.md` for setup
and run commands.

## Decisions & assumptions
I use SQLite because the assignment permits it and it keeps setup local. I count events
with invalid or missing timestamps as malformed because they cannot be ordered safely.
The quality report lists rejection reasons observed in the data. Event IDs break ties
when timestamps are equal. Unchanged files can be safely re-ingested; changed content
at an already-ingested path requires a fresh database.

| Decision | Why | Alternative considered |
|---|---|---|
| Invalid event timestamp | Count as malformed because it cannot safely contribute to time-ordered reports. | Keep it pending as an orphan. |
| Rejection reason keys | Include reasons present in the ingested data. | Emit every possible reason with zero counts. |
| Equal timestamps | Use event ID as a deterministic tie-break. | Depend on file order. |

## Failure modes considered
Malformed JSON and events without IDs are counted and skipped. Duplicate IDs are
skipped across drops. Orphan events remain pending and can link when a lead arrives.
Re-ingesting unchanged files is a no-op. The encryption key must be kept safe and
available. Production key rotation, PostgreSQL, and recovery after losing the key are
outside this implementation.

## Testing
I validated the project under Python 3.15.2. Seven focused tests pass, covering phone,
date, decimal, and timestamp handling; lead merging; event overrides; orphan resolution;
PII encryption; and idempotency. I also ran fresh seed 314 both drop-by-drop and as one
combined input. The five reports matched an in-memory reference calculation based on
the assignment rules. Re-ingesting a drop changed no counters. The repository does not
include official golden reports, so I could not compare against the evaluator's private
expected files.

## Results
On small seed 314, the pipeline read 2,042 nonblank lead rows: 102 were rejected, 42
duplicates were merged, and 117 optional-field warnings were counted. It read 9,248
event lines: 8 malformed, 238 duplicate IDs, 473 final orphans, and 17 unknown
dispositions. The report files matched the reference calculation and were identical
across ingestion modes.

## If I had another day
Next, I would benchmark the large dataset, improve production key management, and run
the evaluator's golden-output comparison when those expected files are available.
