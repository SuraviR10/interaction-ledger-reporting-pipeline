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
from solution.normalizers import (
    load_disposition_catalogue,
    normalize_phone,
    parse_event_timestamp,
)
from solution.security import (
    decrypt_string,
    encrypt_string,
    get_encryption_key,
    get_hash_key,
    hash_phone,
)


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


def retry_orphan_linking(
    conn: sqlite3.Connection,
    enc_key: Optional[bytes] = None,
    hash_key: Optional[bytes] = None,
    finalize_unlinked: bool = False,
) -> int:
    """
    Retries linking unlinked events (is_orphan = 1 or lead_id IS NULL) against the leads table.

    1. Checks if any unlinked event has phone_encrypted but no phone_hash yet, populating phone_hash.
    2. Matches events with (workflow_id, phone_hash) against leads(workflow_id, phone_hash).
    3. Resolves disposition.override events via call_id from other linked call events in the same workflow.
    4. Marks any events that still cannot be linked as is_orphan = 1.
    5. Returns count of newly linked events.
    """
    active_enc = enc_key
    active_hash = hash_key

    # Attempt to populate phone_hash for unlinked events that have phone_encrypted but no phone_hash
    if active_enc is None:
        try:
            active_enc = get_encryption_key()
        except Exception:
            pass
    if active_hash is None:
        try:
            active_hash = get_hash_key()
        except Exception:
            pass

    if active_enc and active_hash:
        unhashed = conn.execute(
            """
            SELECT ledger_id, workflow_id, phone_encrypted
            FROM interaction_ledger
            WHERE lead_id IS NULL AND phone_hash IS NULL AND phone_encrypted IS NOT NULL
            """
        ).fetchall()
        for row in unhashed:
            try:
                raw_p = decrypt_string(row["phone_encrypted"], key=active_enc)
                can_p, valid_p = normalize_phone(raw_p)
                if valid_p and can_p:
                    ph = hash_phone(can_p, key=active_hash)
                    conn.execute(
                        "UPDATE interaction_ledger SET phone_hash = ? WHERE ledger_id = ?",
                        (ph, row["ledger_id"]),
                    )
            except Exception:
                # Corrupted or invalid phone remains unhashed orphan
                pass

    # 1. Match phone-based events against leads table in the same workflow
    cursor1 = conn.execute(
        """
        UPDATE interaction_ledger
        SET lead_id = (
            SELECT l.lead_id FROM leads l
            WHERE l.workflow_id = interaction_ledger.workflow_id
              AND l.phone_hash = interaction_ledger.phone_hash
        ),
        is_orphan = 0
        WHERE lead_id IS NULL
          AND phone_hash IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM leads l
              WHERE l.workflow_id = interaction_ledger.workflow_id
                AND l.phone_hash = interaction_ledger.phone_hash
          );
        """
    )
    linked_count = cursor1.rowcount

    # 2. Match disposition.override (or other call events) through call_id within the same workflow
    cursor2 = conn.execute(
        """
        UPDATE interaction_ledger
        SET lead_id = (
            SELECT parent.lead_id FROM interaction_ledger parent
            WHERE parent.workflow_id = interaction_ledger.workflow_id
              AND parent.call_id = interaction_ledger.call_id
              AND parent.lead_id IS NOT NULL
            LIMIT 1
        ),
        phone_hash = COALESCE(interaction_ledger.phone_hash, (
            SELECT parent.phone_hash FROM interaction_ledger parent
            WHERE parent.workflow_id = interaction_ledger.workflow_id
              AND parent.call_id = interaction_ledger.call_id
              AND parent.lead_id IS NOT NULL
            LIMIT 1
        )),
        is_orphan = 0
        WHERE lead_id IS NULL
          AND EXISTS (
              SELECT 1 FROM interaction_ledger parent
              WHERE parent.workflow_id = interaction_ledger.workflow_id
                AND parent.call_id = interaction_ledger.call_id
                AND parent.lead_id IS NOT NULL
          );
        """
    )
    linked_count += cursor2.rowcount

    # 3. Mark unlinked events that cannot be linked as confirmed orphans if requested
    if finalize_unlinked:
        conn.execute(
            """
            UPDATE interaction_ledger
            SET is_orphan = 1
            WHERE lead_id IS NULL AND (is_orphan IS NULL OR is_orphan != 1);
            """
        )

    return linked_count


def classify_unlinked_as_orphans(conn: sqlite3.Connection) -> int:
    """
    Classifies any pending unlinked events (lead_id IS NULL and is_orphan IS NULL)
    as confirmed orphans (is_orphan = 1).
    """
    cursor = conn.execute(
        """
        UPDATE interaction_ledger
        SET is_orphan = 1
        WHERE lead_id IS NULL AND is_orphan IS NULL;
        """
    )
    return cursor.rowcount


def ingest_event_jsonl(
    file_path: Path,
    drop_name: str = "drop_1",
    db_path: Optional[str] = None,
    enc_key: Optional[bytes] = None,
    hash_key: Optional[bytes] = None,
) -> EventIngestionStats:
    """
    Ingests a single JSONL event file into the SQLite interaction ledger.

    Guarantees:
    - Atomicity: File processing and batch tracking run in one transaction.
    - Idempotency: Previously processed batches are skipped safely.
    - Deduplication: Duplicates (within or across files) are dropped and tracked.
    - Security: Raw phone numbers are encrypted at rest with Fernet (never plaintext).
    - Linkage state: Events are linked to leads via (workflow_id, phone_hash) or call_id.
    - Orphan quarantine: Events that cannot be linked are marked is_orphan = 1.
    - Append-only: Inserts new records; never deletes or modifies original event history.
    """
    stats = EventIngestionStats()
    batch_key = f"{drop_name}:events:{file_path.name}"
    active_enc_key = enc_key if enc_key is not None else get_encryption_key()
    active_hash_key = hash_key if hash_key is not None else get_hash_key()
    disp_catalogue = load_disposition_catalogue()

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
            # Check disposition code validity against the workflow catalogue
            if event.event_type in ("disposition.set", "disposition.override"):
                code = event.payload.get("disposition_code")
                if not code or (event.workflow_id, str(code).strip()) not in disp_catalogue:
                    stats.unknown_disposition_events += 1

            # Parse and normalize timestamps (UTC and IST)
            utc_ts, ist_ts, _ = parse_event_timestamp(event.occurred_at_raw)

            # Fast-path check: is event_id already in the ledger?
            existing_event = conn.execute(
                "SELECT 1 FROM interaction_ledger WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if existing_event:
                stats.duplicate_events_dropped += 1
                continue

            # Phone processing & lead linking
            phone_enc = None
            phone_h = None
            lead_id = None
            is_orphan = None  # None signifies pending linkage

            if event.phone_raw:
                phone_enc = encrypt_string(event.phone_raw, key=active_enc_key)
                canonical_phone, is_valid_phone = normalize_phone(event.phone_raw)
                if is_valid_phone and canonical_phone:
                    phone_h = hash_phone(canonical_phone, key=active_hash_key)
                    # Query matching lead in the same workflow
                    lead_row = conn.execute(
                        "SELECT lead_id FROM leads WHERE workflow_id = ? AND phone_hash = ?",
                        (event.workflow_id, phone_h),
                    ).fetchone()
                    if lead_row:
                        lead_id = lead_row["lead_id"]
                        is_orphan = 0
                    else:
                        is_orphan = None  # Pending lead upload
                else:
                    is_orphan = 1  # Malformed phone is a confirmed orphan
            else:
                # Events without phone (e.g. disposition.override): link via call_id
                parent_row = conn.execute(
                    """
                    SELECT lead_id, phone_hash FROM interaction_ledger
                    WHERE workflow_id = ? AND call_id = ? AND lead_id IS NOT NULL
                    LIMIT 1
                    """,
                    (event.workflow_id, event.call_id),
                ).fetchone()
                if parent_row:
                    lead_id = parent_row["lead_id"]
                    phone_h = parent_row["phone_hash"]
                    is_orphan = 0
                else:
                    is_orphan = None  # Pending parent call linkage

            try:
                conn.execute(
                    """
                    INSERT INTO interaction_ledger (
                        event_id, event_type, workflow_id, call_id,
                        occurred_at_raw, occurred_at_utc, occurred_at_ist,
                        lead_id, phone_hash, phone_encrypted, payload, is_orphan
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.event_type,
                        event.workflow_id,
                        event.call_id,
                        event.occurred_at_raw,
                        utc_ts,
                        ist_ts,
                        lead_id,
                        phone_h,
                        phone_enc,
                        json.dumps(event.payload),
                        is_orphan,
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

        # Retry orphan linking in case overrides or events in the batch resolve against each other or existing leads
        retry_orphan_linking(conn, enc_key=active_enc_key, hash_key=active_hash_key)

        # Update remaining orphan count
        stats.orphan_events = conn.execute(
            "SELECT COUNT(*) FROM interaction_ledger WHERE is_orphan = 1"
        ).fetchone()[0]

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
                stats.unknown_disposition_events,
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
    hash_key: Optional[bytes] = None,
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
            hash_key=hash_key,
        )
        overall_stats.merge(file_stats)

    return overall_stats



