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
from typing import Any, Dict, Iterator, Optional, Tuple


@dataclass
class EventIngestionStats:
    """Tracks metrics required for event processing and data quality reporting."""
    events_total: int = 0
    malformed_events: int = 0
    duplicate_events_dropped: int = 0  # Reserved for Checkpoint 4B
    orphan_events: int = 0             # Reserved for Checkpoint 4C
    unknown_disposition_events: int = 0# Reserved for Checkpoint 4C

    def merge(self, other: "EventIngestionStats") -> None:
        self.events_total += other.events_total
        self.malformed_events += other.malformed_events
        self.duplicate_events_dropped += other.duplicate_events_dropped
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
