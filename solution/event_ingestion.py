"""
Event ingestion module for Assignment A: Interaction Ledger & Reporting Pipeline.

Checkpoint 4A:
- Reads JSONL event logs line-by-line.
- Performs structural parsing and validation.
- Detects malformed records (invalid JSON, non-dict payloads, missing required core fields).
- Handles optional phone numbers (absent on disposition.override).
- Preserves raw timestamps and payloads without mutation.
- Tracks metrics: events_total and malformed_events.
"""

from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, Iterator, List, Optional, Tuple

from solution.db import get_db
from solution.security import encrypt_string, get_encryption_key


@dataclass
class EventIngestionStats:
    """Tracks metrics required for event processing and data quality reporting."""
    events_total: int = 0
    malformed_events: int = 0
    duplicate_events_dropped: int = 0
    events_recorded: int = 0
    orphan_events: int = 0              # Reserved for Checkpoint 4C
    unknown_disposition_events: int = 0 # Reserved for Checkpoint 4C

    def merge(self, other: "EventIngestionStats") -> None:
        self.events_total += other.events_total
        self.malformed_events += other.malformed_events
        self.duplicate_events_dropped += other.duplicate_events_dropped
        self.events_recorded += other.events_recorded
        self.orphan_events += other.orphan_events
        self.unknown_disposition_events += other.unknown_disposition_events


@dataclass
class ParsedEvent:
    """Represents a structurally validated event in memory."""
    event_id: str
    event_type: str
    workflow_id: str
    call_id: str
    occurred_at_raw: str
    phone_raw: Optional[str]
    payload: Dict[str, Any] = field(default_factory=dict)


def parse_event_line(line: str) -> Tuple[str, Optional[ParsedEvent]]:
    """
    Parses a single JSON line from an event stream.

    Returns:
    - ('empty', None) if line is blank or whitespace.
    - ('malformed', None) if JSON is invalid, not a dict, or missing required core fields.
    - ('valid', ParsedEvent) if structurally valid.
    """
    stripped = line.strip()
    if not stripped:
        return "empty", None

    try:
        data = json.loads(stripped)
    except Exception:
        # Invalid JSON syntax (e.g. truncated line)
        return "malformed", None

    # Must be a dictionary (reject arrays, numbers, strings, booleans, null)
    if not isinstance(data, dict):
        return "malformed", None

    # Required core fields: event_id, event_type, workflow_id, call_id, occurred_at
    required_keys = ["event_id", "event_type", "workflow_id", "call_id", "occurred_at"]
    for key in required_keys:
        val = data.get(key)
        if val is None or not isinstance(val, str) or not val.strip():
            return "malformed", None

    # phone is optional (e.g. intentionally omitted on disposition.override)
    phone_raw = data.get("phone")
    if phone_raw is not None:
        if isinstance(phone_raw, str):
            phone_raw = phone_raw.strip() if phone_raw.strip() else None
        else:
            phone_raw = str(phone_raw).strip()

    # payload defaults to empty dictionary if omitted or not a dict
    payload = data.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    event = ParsedEvent(
        event_id=data["event_id"].strip(),
        event_type=data["event_type"].strip(),
        workflow_id=data["workflow_id"].strip(),
        call_id=data["call_id"].strip(),
        occurred_at_raw=data["occurred_at"].strip(),
        phone_raw=phone_raw,
        payload=payload,
    )
    return "valid", event


def read_event_jsonl(
    file_path: Path,
    stats: Optional[EventIngestionStats] = None,
) -> Iterator[ParsedEvent]:
    """
    Reads a JSONL file line-by-line.
    Increments stats.events_total for every non-empty line.
    Increments stats.malformed_events for malformed lines.
    Yields valid ParsedEvent instances.
    """
    if stats is None:
        stats = EventIngestionStats()

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            status, event = parse_event_line(line)
            if status == "empty":
                # Blank lines are skipped and not counted
                continue

            # Every non-empty line is counted in events_total
            stats.events_total += 1

            if status == "malformed":
                stats.malformed_events += 1
                continue

            if status == "valid" and event is not None:
                yield event


def ingest_event_jsonl(
    file_path: Path,
    drop_name: str = "drop_1",
    db_path: Optional[str] = None,
    enc_key: Optional[bytes] = None,
) -> EventIngestionStats:
    """
    Ingests a single JSONL event file into the SQLite interaction ledger.

    Guarantees:
    - Atomicity: File processing and batch tracking run in one transaction.
    - Idempotency: Previously processed batches are skipped safely.
    - Deduplication: Duplicates (within or across files) are dropped and tracked.
    - Security: Raw phone numbers are encrypted at rest with Fernet (never plaintext).
    - Linkage state: is_orphan is set to NULL (unprocessed linkage, not confirmed orphan).
    - Append-only: Inserts new records; never deletes or modifies original event history.
    """
    stats = EventIngestionStats()
    batch_key = f"{drop_name}:events:{file_path.name}"
    active_enc_key = enc_key if enc_key is not None else get_encryption_key()

    with get_db(db_path) as conn:
        # Check idempotency tracking
        existing_batch = conn.execute(
            "SELECT batch_key FROM ingestion_batches WHERE batch_key = ?",
            (batch_key,),
        ).fetchone()
        if existing_batch:
            # File already successfully processed, return zero stats
            return stats

        for event in read_event_jsonl(file_path, stats=stats):
            # Encrypt phone if present, store NULL for events without phone (e.g. override)
            phone_enc = (
                encrypt_string(event.phone_raw, key=active_enc_key)
                if event.phone_raw
                else None
            )

            # Fast-path check: is event_id already in the ledger?
            existing_event = conn.execute(
                "SELECT 1 FROM interaction_ledger WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if existing_event:
                stats.duplicate_events_dropped += 1
                continue

            try:
                # is_orphan = NULL signifies linkage is pending (not confirmed orphan)
                conn.execute(
                    """
                    INSERT INTO interaction_ledger (
                        event_id, event_type, workflow_id, call_id,
                        occurred_at_raw, phone_encrypted, payload, is_orphan
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        event.event_id,
                        event.event_type,
                        event.workflow_id,
                        event.call_id,
                        event.occurred_at_raw,
                        phone_enc,
                        json.dumps(event.payload),
                    ),
                )
                stats.events_recorded += 1
            except sqlite3.IntegrityError as exc:
                # Narrow duplicate event_id handling: suppress only duplicate event_id
                err_msg = str(exc)
                if "UNIQUE constraint failed" in err_msg and "event_id" in err_msg:
                    stats.duplicate_events_dropped += 1
                    continue
                # Re-raise any other integrity errors
                raise

        # Record batch completion in the same transaction
        conn.execute(
            """
            INSERT INTO ingestion_batches (
                batch_key, drop_name, file_type, total_rows,
                rejected_rows, duplicate_rows, warning_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_key,
                drop_name,
                "event_jsonl",
                stats.events_total,
                stats.malformed_events,
                stats.duplicate_events_dropped,
                0,
            ),
        )

    return stats


def discover_event_files(drop_or_events_dir: Path) -> List[Path]:
    """
    Discovers all .jsonl event files in a drop or events directory.
    Returns them sorted strictly in filename order.
    """
    search_dir = drop_or_events_dir
    events_sub = drop_or_events_dir / "events"
    if events_sub.is_dir():
        search_dir = events_sub

    discovered = list(search_dir.glob("*.jsonl"))
    discovered.sort(key=lambda p: p.name)
    return discovered


def ingest_event_directory(
    dir_path: Path,
    drop_name: str = "drop_1",
    db_path: Optional[str] = None,
    enc_key: Optional[bytes] = None,
) -> EventIngestionStats:
    """
    Discovers and ingests all event JSONL files from a directory in filename order.
    Aggregates and returns overall ingestion statistics.
    """
    files = discover_event_files(dir_path)
    overall_stats = EventIngestionStats()

    for event_file in files:
        file_stats = ingest_event_jsonl(
            file_path=event_file,
            drop_name=drop_name,
            db_path=db_path,
            enc_key=enc_key,
        )
        overall_stats.merge(file_stats)

    return overall_stats


