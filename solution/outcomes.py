"""
Call outcomes and lead bucket evaluation module for Assignment A.

Stage 5:
- Derives call-level facts: call initiation, connection, telephony failure,
  and effective disposition (latest override > latest set).
- Derives lead-level summaries: total calls, connected calls, latest call timestamp,
  current disposition, ever_success flag, and lead bucket.

Bucket Priority Order (Assignment Spec):
1. yet_to_reach_out: lead has no calls (no linked call.initiated).
2. completed: at least one call has a terminal disposition (is_terminal=True).
3. in_progress_telephony_failed: every call has a telephony failure (call.telephony_failed).
4. in_progress_dialed: all other cases.
"""

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from solution.normalizers import load_disposition_catalogue


@dataclass
class CallOutcome:
    """Represents derived call-level metrics."""
    workflow_id: str
    call_id: str
    lead_id: int
    initiated_at_utc: str
    initiated_at_ist: str
    call_date_ist: str  # YYYY-MM-DD
    is_connected: bool
    is_telephony_failed: bool
    effective_disposition: Optional[str]
    is_terminal: bool
    is_success: bool


@dataclass
class LeadOutcome:
    """Represents derived lead-level metrics for reporting."""
    workflow_id: str
    lead_id: int
    external_ref: str
    batch_id: str
    phone_masked: str
    bucket: str
    total_calls: int
    connected_calls: int
    last_call_at: str
    current_disposition: str
    ever_success: bool


def determine_lead_bucket(calls: List[CallOutcome]) -> str:
    """
    Evaluates a lead's operational bucket based on strictly prioritized rules:
    1. yet_to_reach_out: no calls.
    2. completed: any call's effective disposition is terminal (is_terminal=True).
    3. in_progress_telephony_failed: every call has a call.telephony_failed event.
    4. in_progress_dialed: everything else.
    """
    if not calls:
        return "yet_to_reach_out"

    # Priority 2: completed (any call has an effective terminal disposition)
    if any(c.is_terminal for c in calls):
        return "completed"

    # Priority 3: in_progress_telephony_failed (every call had a telephony failure)
    if all(c.is_telephony_failed for c in calls):
        return "in_progress_telephony_failed"

    # Priority 4: in_progress_dialed (all other cases)
    return "in_progress_dialed"


def calculate_effective_disposition(
    events: List[Dict[str, Any]],
    workflow_id: str,
    disposition_catalogue: Dict[Tuple[str, str], Tuple[bool, bool]],
) -> Tuple[Optional[str], bool, bool]:
    """
    Calculates the effective disposition for a call.
    Rules:
    - Latest valid disposition.override by occurred_at_utc (tie-break: ledger_id DESC).
    - Else latest valid disposition.set by occurred_at_utc (tie-break: ledger_id DESC).
    - Else None.
    Returns: (effective_code, is_terminal, is_success)
    """
    overrides = []
    sets = []

    for ev in events:
        etype = ev["event_type"]
        if etype in ("disposition.set", "disposition.override"):
            payload = ev["payload"] if isinstance(ev["payload"], dict) else json.loads(ev["payload"] or "{}")
            code = payload.get("disposition_code")
            if code and (workflow_id, str(code).strip()) in disposition_catalogue:
                valid_code = str(code).strip()
                item = {
                    "code": valid_code,
                    "occurred_at_utc": ev.get("occurred_at_utc") or "",
                    "ledger_id": ev.get("ledger_id", 0),
                }
                if etype == "disposition.override":
                    overrides.append(item)
                else:
                    sets.append(item)

    # Sort descending by occurred_at_utc, then ledger_id
    key_func = lambda x: (x["occurred_at_utc"], x["ledger_id"])

    if overrides:
        overrides.sort(key=key_func, reverse=True)
        chosen_code = overrides[0]["code"]
        is_term, is_succ = disposition_catalogue[(workflow_id, chosen_code)]
        return chosen_code, is_term, is_succ

    if sets:
        sets.sort(key=key_func, reverse=True)
        chosen_code = sets[0]["code"]
        is_term, is_succ = disposition_catalogue[(workflow_id, chosen_code)]
        return chosen_code, is_term, is_succ

    return None, False, False


def compute_call_outcomes(
    conn: sqlite3.Connection,
    disposition_catalogue: Optional[Dict[Tuple[str, str], Tuple[bool, bool]]] = None,
) -> Dict[Tuple[str, str], CallOutcome]:
    """
    Queries all linked events from the ledger and computes derived facts for every valid call.
    Returns a dict mapping (workflow_id, call_id) -> CallOutcome.
    """
    disp_cat = disposition_catalogue if disposition_catalogue is not None else load_disposition_catalogue()

    # Query all events that are linked to leads
    rows = conn.execute(
        """
        SELECT ledger_id, event_id, event_type, workflow_id, call_id,
               occurred_at_raw, occurred_at_utc, occurred_at_ist,
               lead_id, payload
        FROM interaction_ledger
        WHERE is_orphan = 0 AND lead_id IS NOT NULL
        ORDER BY workflow_id, call_id, occurred_at_utc ASC, ledger_id ASC
        """
    ).fetchall()

    events_by_call: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        key = (r["workflow_id"], r["call_id"])
        if key not in events_by_call:
            events_by_call[key] = []
        events_by_call[key].append(dict(r))

    call_outcomes: Dict[Tuple[str, str], CallOutcome] = {}

    for (wf, cid), call_evs in events_by_call.items():
        # A call is counted only if its call.initiated event is present and linked
        init_events = [e for e in call_evs if e["event_type"] == "call.initiated"]
        if not init_events:
            continue

        # Earliest initiated event defines the call's timestamp and date
        init_event = init_events[0]
        init_utc = init_event["occurred_at_utc"] or ""
        init_ist = init_event["occurred_at_ist"] or ""
        call_date_ist = init_ist[:10] if len(init_ist) >= 10 else ""

        is_connected = any(e["event_type"] == "call.connected" for e in call_evs)
        is_telephony_failed = any(e["event_type"] == "call.telephony_failed" for e in call_evs)

        eff_code, is_term, is_succ = calculate_effective_disposition(call_evs, wf, disp_cat)

        outcome = CallOutcome(
            workflow_id=wf,
            call_id=cid,
            lead_id=init_event["lead_id"],
            initiated_at_utc=init_utc,
            initiated_at_ist=init_ist,
            call_date_ist=call_date_ist,
            is_connected=is_connected,
            is_telephony_failed=is_telephony_failed,
            effective_disposition=eff_code,
            is_terminal=is_term,
            is_success=is_succ,
        )
        call_outcomes[(wf, cid)] = outcome

    return call_outcomes


def compute_lead_outcomes(
    conn: sqlite3.Connection,
    disposition_catalogue: Optional[Dict[Tuple[str, str], Tuple[bool, bool]]] = None,
) -> List[LeadOutcome]:
    """
    Computes reporting outcomes for every lead in the database.
    Returns list of LeadOutcome objects sorted by workflow_id, external_ref.
    """
    call_outcomes = compute_call_outcomes(conn, disposition_catalogue)

    # Group calls by lead_id
    calls_by_lead: Dict[int, List[CallOutcome]] = {}
    for outcome in call_outcomes.values():
        if outcome.lead_id not in calls_by_lead:
            calls_by_lead[outcome.lead_id] = []
        calls_by_lead[outcome.lead_id].append(outcome)

    # Fetch all leads sorted by workflow_id, external_ref
    leads_cursor = conn.execute(
        """
        SELECT lead_id, workflow_id, external_ref, batch_id, phone_last4
        FROM leads
        ORDER BY workflow_id ASC, external_ref ASC
        """
    )

    lead_outcomes = []
    for lead in leads_cursor.fetchall():
        lid = lead["lead_id"]
        wf = lead["workflow_id"]
        ref = lead["external_ref"]
        batch = lead["batch_id"]
        p_last4 = lead["phone_last4"]
        phone_masked = f"+91******{p_last4}"

        lead_calls = calls_by_lead.get(lid, [])
        # Sort calls chronologically by initiated timestamp
        lead_calls.sort(key=lambda c: (c.initiated_at_utc, c.call_id))

        bucket = determine_lead_bucket(lead_calls)
        total_calls = len(lead_calls)
        connected_calls = sum(1 for c in lead_calls if c.is_connected)

        last_call_at = lead_calls[-1].initiated_at_ist if lead_calls else ""

        # Most recent call with an effective disposition
        calls_with_disp = [c for c in lead_calls if c.effective_disposition]
        current_disp = calls_with_disp[-1].effective_disposition if calls_with_disp else ""

        ever_succ = any(c.is_success for c in lead_calls)

        lead_outcomes.append(
            LeadOutcome(
                workflow_id=wf,
                lead_id=lid,
                external_ref=ref,
                batch_id=batch,
                phone_masked=phone_masked,
                bucket=bucket,
                total_calls=total_calls,
                connected_calls=connected_calls,
                last_call_at=last_call_at,
                current_disposition=current_disp,
                ever_success=ever_succ,
            )
        )

    return lead_outcomes
