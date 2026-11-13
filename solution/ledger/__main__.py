from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .db import connect
from .ingest import ingest_drop, load_dispositions, load_schemas
from .report import generate_reports
from .security import PiiBox, generate_key


def main() -> int:
    parser = argparse.ArgumentParser(prog="assignment-a")
    sub = parser.add_subparsers(dest="command", required=True)
    keygen = sub.add_parser("keygen", help="create a local PII encryption key")
    keygen.add_argument("--out", default=".pii.key")
    ingest = sub.add_parser("ingest", help="ingest one generated data drop")
    ingest.add_argument("drop")
    ingest.add_argument("--db", default="solution/ledger.sqlite")
    ingest.add_argument("--key-file", default=".pii.key")
    ingest.add_argument("--schema-dir", default="assignment-a-data/schemas")
    report = sub.add_parser("report", help="write the five contract report files")
    report.add_argument("--out", default="reports")
    report.add_argument("--db", default="solution/ledger.sqlite")
    report.add_argument("--key-file", default=".pii.key")
    report.add_argument("--schema-dir", default="assignment-a-data/schemas")
    args = parser.parse_args()
    try:
        if args.command == "keygen":
            path = Path(args.out)
            if path.exists():
                raise ValueError(f"Refusing to overwrite existing key file: {path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(generate_key() + b"\n")
            print(f"Created PII key at {path}. Keep it private and back it up securely.")
            return 0
        box = PiiBox.from_env_or_file(args.key_file)
        schemas = load_schemas(Path(args.schema_dir))
        if args.command == "ingest":
            dispositions = load_dispositions(Path(args.schema_dir) / "dispositions.csv")
            with connect(args.db) as conn:
                source = Path(args.drop)
                if (source / "leads").is_dir() or (source / "events").is_dir():
                    drops = [source]
                else:
                    drops = sorted((p for p in source.iterdir() if p.is_dir() and
                                    ((p / "leads").is_dir() or (p / "events").is_dir())), key=lambda p: p.name)
                if not drops:
                    raise ValueError(f"No data drops found under {source}")
                metrics = {}
                for drop in drops:
                    current = ingest_drop(conn, drop, schemas, dispositions, box)
                    for name, value in current.items():
                        metrics[name] = metrics.get(name, 0) + value
                print("Ingestion complete:", ", ".join(f"{k}={v}" for k, v in metrics.items()))
            return 0
        with connect(args.db) as conn:
            quality = generate_reports(conn, Path(args.out), schemas,
                                       load_dispositions(Path(args.schema_dir) / "dispositions.csv"))
        print(f"Reports written to {args.out}. Quality summary: {quality}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
