"""
Report generation module for Assignment A: Interaction Ledger & Reporting Pipeline.

Generates the five required reports:
1. batch_summary.csv: Lead status aggregations per workflow and batch.
2. daily_overview.csv: Call volume, connectivity rate, and success leads per workflow and date.
3. lead_report.csv: Lead details, masking, buckets, and disposition status.
4. rejections.csv: Audit log of rejected CSV lead rows (strictly no PII).
5. data_quality.json: Comprehensive data pipeline health and anomaly metrics.
"""

import csv
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional

from solution.config import DEFAULT_REPORTS_DIR
from solution.normalizers import load_disposition_catalogue
from solution.outcomes import (
    CallOutcome,
    LeadOutcome,
    compute_call_outcomes,
    compute_lead_outcomes,
)


def generate_batch_summary(
    lead_outcomes: List[LeadOutcome],
    out_path: Path,
) -> None:
    """
    Generates batch_summary.csv sorted by workflow_id, batch_id.
    Header:
    workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,in_progress_dialed,completed
    """
    batches: Dict[tuple, Dict[str, int]] = {}

    for lead in lead_outcomes:
        key = (lead.workflow_id, lead.batch_id)
        if key not in batches:
            batches[key] = {
                "total_leads": 0,
                "yet_to_reach_out": 0,
                "in_progress_telephony_failed": 0,
                "in_progress_dialed": 0,
                "completed": 0,
            }
        batches[key]["total_leads"] += 1
        if lead.bucket in batches[key]:
            batches[key][lead.bucket] += 1

    sorted_keys = sorted(batches.keys(), key=lambda k: (k[0], k[1]))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "workflow_id",
            "batch_id",
            "total_leads",
            "yet_to_reach_out",
            "in_progress_telephony_failed",
            "in_progress_dialed",
            "completed",
        ])
        for wf, batch in sorted_keys:
            counts = batches[(wf, batch)]
            writer.writerow([
                wf,
                batch,
                counts["total_leads"],
                counts["yet_to_reach_out"],
                counts["in_progress_telephony_failed"],
                counts["in_progress_dialed"],
                counts["completed"],
            ])


def generate_daily_overview(
    call_outcomes: Dict[tuple, CallOutcome],
    out_path: Path,
) -> None:
    """
    Generates daily_overview.csv: one row per workflow and IST date with at least one call.
    Sorted by workflow_id, date.
    Header:
    workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads
    """
    # Group by (workflow_id, call_date_ist)
    daily: Dict[tuple, List[CallOutcome]] = {}
    for outcome in call_outcomes.values():
        if not outcome.call_date_ist:
            continue
        key = (outcome.workflow_id, outcome.call_date_ist)
        if key not in daily:
            daily[key] = []
        daily[key].append(outcome)

    sorted_keys = sorted(daily.keys(), key=lambda k: (k[0], k[1]))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "workflow_id",
            "date",
            "total_calls",
            "connected_calls",
            "connectivity_rate",
            "success_leads",
        ])
        for wf, date_str in sorted_keys:
            calls = daily[(wf, date_str)]
            total = len(calls)
            connected = sum(1 for c in calls if c.is_connected)
            rate = f"{(connected / total):.4f}" if total > 0 else "0.0000"
            # Distinct leads on that date whose effective disposition has is_success=True
            success_leads_count = len(set(c.lead_id for c in calls if c.is_success))

            writer.writerow([
                wf,
                date_str,
                total,
                connected,
                rate,
                success_leads_count,
            ])


def generate_lead_report(
    lead_outcomes: List[LeadOutcome],
    out_path: Path,
) -> None:
    """
    Generates lead_report.csv: one row per lead, sorted by workflow_id, external_ref.
    Header:
    workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,connected_calls,last_call_at,current_disposition,ever_success
    """
    # Sort by workflow_id, external_ref
    sorted_leads = sorted(lead_outcomes, key=lambda l: (l.workflow_id, l.external_ref))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "workflow_id",
            "external_ref",
            "batch_id",
            "phone_masked",
            "bucket",
            "total_calls",
            "connected_calls",
            "last_call_at",
            "current_disposition",
            "ever_success",
        ])
        for l in sorted_leads:
            writer.writerow([
                l.workflow_id,
                l.external_ref,
                l.batch_id,
                l.phone_masked,
                l.bucket,
                l.total_calls,
                l.connected_calls,
                l.last_call_at,
                l.current_disposition,
                "true" if l.ever_success else "false",
            ])


def generate_rejections_report(
    conn: sqlite3.Connection,
    out_path: Path,
) -> None:
    """
    Generates rejections.csv sorted by workflow_id, batch_id, row_number.
    Header:
    workflow_id,batch_id,row_number,reason
    """
    rows = conn.execute(
        """
        SELECT workflow_id, batch_id, row_number, reason
        FROM lead_rejections
        ORDER BY workflow_id ASC, batch_id ASC, row_number ASC
        """
    ).fetchall()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["workflow_id", "batch_id", "row_number", "reason"])
        for r in rows:
            writer.writerow([r["workflow_id"], r["batch_id"], r["row_number"], r["reason"]])


def generate_data_quality_report(
    conn: sqlite3.Connection,
    out_path: Path,
) -> Dict[str, Any]:
    """
    Generates data_quality.json containing consolidated ingestion and anomaly metrics.
    """
    # 1. Lead metrics from batches
    lead_totals = conn.execute(
        """
        SELECT
            COALESCE(SUM(total_rows), 0) AS total_rows,
            COALESCE(SUM(duplicate_rows), 0) AS duplicate_rows,
            COALESCE(SUM(warning_count), 0) AS warning_count
        FROM ingestion_batches
        WHERE file_type = 'lead_csv'
        """
    ).fetchone()

    lead_rows_total = lead_totals["total_rows"]
    duplicate_leads_merged = lead_totals["duplicate_rows"]
    field_warnings = lead_totals["warning_count"]

    # 2. Rejections from lead_rejections table
    rejection_rows = conn.execute(
        """
        SELECT reason, COUNT(*) AS cnt
        FROM lead_rejections
        GROUP BY reason
        ORDER BY reason ASC
        """
    ).fetchall()
    lead_rows_rejected = sum(r["cnt"] for r in rejection_rows)
    rejections_by_reason = {r["reason"]: r["cnt"] for r in rejection_rows}

    # 3. Event metrics from batches
    event_totals = conn.execute(
        """
        SELECT
            COALESCE(SUM(total_rows), 0) AS total_rows,
            COALESCE(SUM(rejected_rows), 0) AS malformed_rows,
            COALESCE(SUM(duplicate_rows), 0) AS duplicate_rows
        FROM ingestion_batches
        WHERE file_type = 'event_jsonl'
        """
    ).fetchone()

    events_total = event_totals["total_rows"]
    malformed_events = event_totals["malformed_rows"]
    duplicate_events_dropped = event_totals["duplicate_rows"]

    # 4. Final state orphan events
    orphan_events = conn.execute(
        """
        SELECT COUNT(*) FROM interaction_ledger
        WHERE is_orphan = 1 OR (is_orphan IS NULL AND lead_id IS NULL)
        """
    ).fetchone()[0]

    # 5. Unknown disposition events (disposition.* events whose code is not in catalogue)
    disp_cat = load_disposition_catalogue()
    disp_events = conn.execute(
        """
        SELECT workflow_id, payload FROM interaction_ledger
        WHERE event_type IN ('disposition.set', 'disposition.override')
        """
    ).fetchall()

    unknown_disp_count = 0
    for de in disp_events:
        try:
            pl = json.loads(de["payload"] or "{}")
            code = pl.get("disposition_code")
            if not code or (de["workflow_id"], str(code).strip()) not in disp_cat:
                unknown_disp_count += 1
        except Exception:
            unknown_disp_count += 1

    report_data = {
        "lead_rows_total": lead_rows_total,
        "lead_rows_rejected": lead_rows_rejected,
        "rejections_by_reason": rejections_by_reason,
        "duplicate_lead_rows_merged": duplicate_leads_merged,
        "field_warnings": field_warnings,
        "events_total": events_total,
        "malformed_events": malformed_events,
        "duplicate_events_dropped": duplicate_events_dropped,
        "orphan_events": orphan_events,
        "unknown_disposition_events": unknown_disp_count,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)

    return report_data


def generate_all_reports(
    conn: sqlite3.Connection,
    out_dir: Optional[Path] = None,
) -> Dict[str, Path]:
    """
    Generates all five reports from the current database state.
    Returns a dictionary of report names mapping to their output file paths.
    """
    target_dir = out_dir or DEFAULT_REPORTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. Compute outcomes
    disp_catalogue = load_disposition_catalogue()
    call_outcomes = compute_call_outcomes(conn, disp_catalogue)
    lead_outcomes = compute_lead_outcomes(conn, disp_catalogue)

    paths = {
        "batch_summary": target_dir / "batch_summary.csv",
        "daily_overview": target_dir / "daily_overview.csv",
        "lead_report": target_dir / "lead_report.csv",
        "rejections": target_dir / "rejections.csv",
        "data_quality": target_dir / "data_quality.json",
    }

    generate_batch_summary(lead_outcomes, paths["batch_summary"])
    generate_daily_overview(call_outcomes, paths["daily_overview"])
    generate_lead_report(lead_outcomes, paths["lead_report"])
    generate_rejections_report(conn, paths["rejections"])
    generate_data_quality_report(conn, paths["data_quality"])

    return paths
