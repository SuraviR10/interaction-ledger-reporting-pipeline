"""
Command-Line Interface (CLI) for Assignment A: Interaction Ledger & Reporting Pipeline.

Provides commands according to the assignment specification:
1. ingest <drop_path>:
   Ingests lead CSVs and event JSONL streams from a drop directory (e.g. data/drop_1).
   Idempotent and incremental.
2. report --out <dir>:
   Generates all five reports into the specified output directory from the current database state.
3. run <drop_paths...> --out <dir>:
   Convenience command to ingest one or more drops sequentially and generate reports.
4. init-db:
   Initializes or migrates the SQLite database schema safely without data loss.
"""

import argparse
from pathlib import Path
import sys
from typing import List, Optional

from solution.config import (
    DEFAULT_DB_PATH,
    DEFAULT_REPORTS_DIR,
    load_env_file,
)
from solution.db import get_db, init_db
from solution.event_ingestion import (
    classify_unlinked_as_orphans,
    ingest_event_directory,
    retry_orphan_linking,
)
from solution.lead_ingestion import ingest_lead_directory
from solution.reports import generate_all_reports


def cmd_init_db(db_path: Optional[Path] = None) -> int:
    """Initializes or migrates the database schema."""
    target_db = db_path or DEFAULT_DB_PATH
    print(f"Initializing database at: {target_db}")
    init_db(target_db)
    print("Database initialization and schema verification complete.")
    return 0


def cmd_ingest(drop_path: Path, db_path: Optional[Path] = None) -> int:
    """Ingests all leads and events from a single drop directory."""
    target_db = db_path or DEFAULT_DB_PATH
    init_db(target_db)

    if not drop_path.is_dir():
        print(f"Error: Drop directory not found at '{drop_path}'", file=sys.stderr)
        return 1

    drop_name = drop_path.name
    print(f"--- Ingesting Drop: {drop_name} ({drop_path}) ---")

    # 1. Ingest Leads
    lead_stats = ingest_lead_directory(
        dir_path=drop_path,
        drop_name=drop_name,
        db_path=str(target_db),
    )
    print(
        f"Leads Ingestion: {lead_stats.lead_rows_total} rows processed, "
        f"{lead_stats.leads_created} created, "
        f"{lead_stats.duplicate_lead_rows_merged} merged, "
        f"{lead_stats.lead_rows_rejected} rejected, "
        f"{lead_stats.field_warnings} warnings."
    )

    # 2. Ingest Events
    event_stats = ingest_event_directory(
        dir_path=drop_path,
        drop_name=drop_name,
        db_path=str(target_db),
    )
    print(
        f"Events Ingestion: {event_stats.events_total} rows processed, "
        f"{event_stats.events_recorded} recorded, "
        f"{event_stats.duplicate_events_dropped} duplicates dropped, "
        f"{event_stats.malformed_events} malformed, "
        f"{event_stats.unknown_disposition_events} unknown dispositions."
    )

    # 3. Finalize linkage pass across database
    with get_db(target_db) as conn:
        relinked = retry_orphan_linking(conn)
        if relinked > 0:
            print(f"Orphan Resolution: {relinked} pending events successfully linked to leads.")
        classify_unlinked_as_orphans(conn)
        orphan_count = conn.execute("SELECT COUNT(*) FROM interaction_ledger WHERE is_orphan = 1").fetchone()[0]
        print(f"Current Quarantined Orphan Events: {orphan_count}")

    print(f"Successfully processed {drop_name}.\n")
    return 0


def cmd_report(out_dir: Optional[Path] = None, db_path: Optional[Path] = None) -> int:
    """Generates all five reports from the current database state."""
    target_db = db_path or DEFAULT_DB_PATH
    target_out = out_dir or DEFAULT_REPORTS_DIR
    init_db(target_db)

    print(f"Generating reports into: {target_out}")
    with get_db(target_db) as conn:
        # Final orphan check before reporting
        retry_orphan_linking(conn)
        classify_unlinked_as_orphans(conn)

        paths = generate_all_reports(conn, out_dir=target_out)

    print("Generated 5 business reports successfully:")
    for name, p in paths.items():
        print(f"  - {p.name} ({p})")
    return 0


def cmd_run(
    drop_paths: List[Path],
    out_dir: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> int:
    """Sequentially ingests drops and generates reports."""
    for drop in drop_paths:
        ret = cmd_ingest(drop, db_path=db_path)
        if ret != 0:
            return ret
    return cmd_report(out_dir=out_dir, db_path=db_path)


def main(argv: Optional[List[str]] = None) -> int:
    """Main CLI entrypoint."""
    load_env_file()

    parser = argparse.ArgumentParser(
        description="Assignment A: Interaction Ledger & Reporting Pipeline CLI"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to SQLite database file (defaults to LEDGER_DB_PATH or ledger.db)",
    )

    subparsers = parser.add_subparsers(dest="subcommand", help="Available commands")

    # Command: init-db
    subparsers.add_parser("init-db", help="Initialize or migrate database schema")

    # Command: ingest <drop_path>
    ingest_parser = subparsers.add_parser("ingest", help="Ingest a data drop directory")
    ingest_parser.add_argument("drop_path", type=Path, help="Path to drop directory (e.g. data/drop_1)")

    # Command: report --out <out_dir>
    report_parser = subparsers.add_parser("report", help="Generate all 5 reports")
    report_parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_REPORTS_DIR,
        help=f"Output directory for report files (default: {DEFAULT_REPORTS_DIR})",
    )

    # Command: run <drop_paths...> --out <out_dir>
    run_parser = subparsers.add_parser("run", help="Ingest drops and generate reports in one command")
    run_parser.add_argument("drop_paths", type=Path, nargs="+", help="Drop directories to ingest in order")
    run_parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_REPORTS_DIR,
        help=f"Output directory for report files (default: {DEFAULT_REPORTS_DIR})",
    )

    args = parser.parse_args(argv)

    if not args.subcommand:
        parser.print_help()
        return 0

    if args.subcommand == "init-db":
        return cmd_init_db(args.db)
    elif args.subcommand == "ingest":
        return cmd_ingest(args.drop_path, db_path=args.db)
    elif args.subcommand == "report":
        return cmd_report(out_dir=args.out, db_path=args.db)
    elif args.subcommand == "run":
        return cmd_run(drop_paths=args.drop_paths, out_dir=args.out, db_path=args.db)

    return 0


if __name__ == "__main__":
    sys.exit(main())
