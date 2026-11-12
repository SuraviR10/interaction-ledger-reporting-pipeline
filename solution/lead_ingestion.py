"""
Lead CSV Ingestion and Normalisation module for Assignment A.

Handles:
- CSV discovery and workflow schema mapping.
- Header normalisation and alias resolution.
- Row filtering (blank row skipping, non-blank row numbering starting at 1).
- Field validation in schema order.
- Rejection recording (strictly without PII).
- Warning tracking on valid rows with invalid optional fields.
- Duplicate identification and last-write-wins merging.
- Application of schema defaults to fields that remain null after merging.
- PII authenticated encryption before database insertion.
"""

import csv
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from solution.config import SCHEMAS_DIR
from solution.db import get_db
from solution.normalizers import (
    match_header_to_field,
    normalize_date,
    normalize_decimal,
    normalize_enum,
    normalize_phone,
)
from solution.security import (
    encrypt_string,
    get_encryption_key,
    get_hash_key,
    hash_phone,
)


@dataclass
class LeadIngestionStats:
    """Tracks metrics required for data quality reporting."""
    lead_rows_total: int = 0
    lead_rows_rejected: int = 0
    rejections_by_reason: Dict[str, int] = field(default_factory=dict)
    duplicate_lead_rows_merged: int = 0
    field_warnings: int = 0
    leads_created: int = 0
    batches_processed: int = 0

    def add_rejection(self, reason: str) -> None:
        self.lead_rows_rejected += 1
        self.rejections_by_reason[reason] = self.rejections_by_reason.get(reason, 0) + 1

    def merge(self, other: "LeadIngestionStats") -> None:
        self.lead_rows_total += other.lead_rows_total
        self.lead_rows_rejected += other.lead_rows_rejected
        self.duplicate_lead_rows_merged += other.duplicate_lead_rows_merged
        self.field_warnings += other.field_warnings
        self.leads_created += other.leads_created
        self.batches_processed += other.batches_processed
        for reason, count in other.rejections_by_reason.items():
            self.rejections_by_reason[reason] = self.rejections_by_reason.get(reason, 0) + count


def load_schema(workflow_id: str, schemas_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Loads a JSON workflow schema by workflow_id."""
    target_dir = schemas_dir or SCHEMAS_DIR
    schema_path = target_dir / f"{workflow_id}.json"
    if not schema_path.is_file():
        raise FileNotFoundError(f"Schema not found for workflow '{workflow_id}' at {schema_path}")

    with open(schema_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_all_schemas(schemas_dir: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Loads all workflow JSON schemas available in the schemas directory."""
    target_dir = schemas_dir or SCHEMAS_DIR
    schemas = {}
    for schema_file in target_dir.glob("*.json"):
        with open(schema_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "workflow_id" in data:
                schemas[data["workflow_id"]] = data
    return schemas


def identify_workflow(csv_path: Path, schemas: Dict[str, Dict[str, Any]]) -> str:
    """
    Identifies the workflow for a given CSV file:
    1. Checks if parent directory matches a known workflow ID (convention: leads/<workflow_id>/<batch>.csv).
    2. Fallback: inspects CSV header row and matches against schema field aliases.
    """
    parent_name = csv_path.parent.name
    if parent_name in schemas:
        return parent_name

    # Fallback to header inspection
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            headers = next(reader)
        except StopIteration:
            raise ValueError(f"CSV file is empty: {csv_path}")

    best_match_workflow = None
    best_match_score = -1

    for wf_id, schema in schemas.items():
        score = sum(
            1 for h in headers if match_header_to_field(h, schema.get("fields", [])) is not None
        )
        if score > best_match_score:
            best_match_score = score
            best_match_workflow = wf_id

    if best_match_workflow is None or best_match_score <= 0:
        raise ValueError(f"Could not identify workflow for CSV file: {csv_path}")

    return best_match_workflow


def build_header_mapping(headers: List[str], schema: Dict[str, Any]) -> Dict[int, str]:
    """
    Maps CSV column indices to canonical schema field names.
    Columns that match nothing are ignored.
    """
    fields = schema.get("fields", [])
    mapping = {}
    for idx, header in enumerate(headers):
        canonical_name = match_header_to_field(header, fields)
        if canonical_name is not None:
            mapping[idx] = canonical_name
    return mapping


def evaluate_lead_row(
    row_values: List[str],
    header_mapping: Dict[int, str],
    schema: Dict[str, Any],
) -> Tuple[str, Optional[Dict[str, Any]], int]:
    """
    Evaluates a single non-blank CSV row against the workflow schema.

    Returns:
    - ('blank', None, 0) if row is completely blank.
    - ('rejected', {'reason': reason}, 0) if rejected (first failure in schema order).
    - ('valid', parsed_fields_dict, warnings_count) if valid.
    """
    # 1. Blank row check: all cells empty or whitespace
    if all(not cell.strip() for cell in row_values):
        return "blank", None, 0

    # Extract raw values mapped to canonical field names
    raw_mapped: Dict[str, str] = {}
    for idx, cell in enumerate(row_values):
        if idx in header_mapping:
            field_name = header_mapping[idx]
            trimmed = cell.strip()
            if trimmed:
                raw_mapped[field_name] = trimmed

    schema_fields = schema.get("fields", [])
    parsed_values: Dict[str, Any] = {}
    warnings = 0

    # Walk fields strictly in schema order
    for f_def in schema_fields:
        f_name = f_def["name"]
        f_type = f_def.get("type", "string")
        f_required = f_def.get("required", False)
        raw_val = raw_mapped.get(f_name)

        # Check required fields
        if raw_val is None:
            if f_required:
                return "rejected", {"reason": f"missing_required:{f_name}"}, 0
            parsed_values[f_name] = None
            continue

        # Normalise based on field type
        if f_type == "phone":
            canonical_phone, is_valid = normalize_phone(raw_val)
            if not is_valid:
                # Present phone is invalid -> reject immediately
                return "rejected", {"reason": "invalid_phone"}, 0
            parsed_values[f_name] = canonical_phone

        elif f_type == "date":
            iso_date, is_valid = normalize_date(raw_val)
            if not is_valid:
                if f_required:
                    return "rejected", {"reason": f"missing_required:{f_name}"}, 0
                # Optional field invalid: store null and count warning
                parsed_values[f_name] = None
                warnings += 1
            else:
                parsed_values[f_name] = iso_date

        elif f_type == "decimal":
            dec_val, is_valid = normalize_decimal(raw_val)
            if not is_valid:
                if f_required:
                    return "rejected", {"reason": f"missing_required:{f_name}"}, 0
                parsed_values[f_name] = None
                warnings += 1
            else:
                parsed_values[f_name] = dec_val

        elif f_type == "enum":
            allowed = f_def.get("values", [])
            enum_val, is_valid = normalize_enum(raw_val, allowed)
            if not is_valid:
                if f_required:
                    return "rejected", {"reason": f"missing_required:{f_name}"}, 0
                parsed_values[f_name] = None
                warnings += 1
            else:
                parsed_values[f_name] = enum_val

        else:
            # Default string type: trimmed value
            parsed_values[f_name] = raw_val

    return "valid", parsed_values, warnings


def ingest_lead_csv(
    csv_path: Path,
    workflow_id: Optional[str] = None,
    drop_name: str = "drop_1",
    db_path: Optional[str] = None,
    enc_key: Optional[bytes] = None,
    hash_key: Optional[bytes] = None,
) -> LeadIngestionStats:
    """
    Ingests a single CSV lead batch into SQLite.
    Guarantees:
    - Atomicity: executed inside a database transaction.
    - Idempotency: skips files already registered in ingestion_batches.
    - Preserves batch_id of first valid occurrence.
    - Last-write-wins merging for non-null fields on duplicate leads.
    - Encrypts PII fields before writing to the database.
    """
    schemas = load_all_schemas()
    wf_id = workflow_id or identify_workflow(csv_path, schemas)
    schema = schemas[wf_id]

    batch_id = csv_path.stem
    batch_key = f"{drop_name}:{wf_id}:{csv_path.name}"
    stats = LeadIngestionStats()

    active_enc_key = enc_key if enc_key is not None else get_encryption_key()
    active_hash_key = hash_key if hash_key is not None else get_hash_key()

    # Identify the reference field (e.g. policy_number or order_id)
    ref_field_name = None
    for f in schema.get("fields", []):
        if f.get("reference", False):
            ref_field_name = f["name"]
            break
    if not ref_field_name:
        raise ValueError(f"Schema for workflow '{wf_id}' has no field marked 'reference: true'")

    # Pre-collect schema field defaults
    field_defaults = {
        f["name"]: f["default"]
        for f in schema.get("fields", [])
        if "default" in f
    }

    with get_db(db_path) as conn:
        # Check idempotency
        existing_batch = conn.execute(
            "SELECT batch_key FROM ingestion_batches WHERE batch_key = ?",
            (batch_key,),
        ).fetchone()
        if existing_batch:
            # Already ingested, return empty stats without reprocessing
            return stats

        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            try:
                headers = next(reader)
            except StopIteration:
                return stats

            header_mapping = build_header_mapping(headers, schema)
            row_number = 0

            for raw_row in reader:
                # 1. Blank row check
                if all(not cell.strip() for cell in raw_row):
                    continue

                # Increment row number for nonblank rows
                row_number += 1
                stats.lead_rows_total += 1

                status, parsed, row_warnings = evaluate_lead_row(raw_row, header_mapping, schema)

                if status == "rejected":
                    reason = parsed["reason"]
                    stats.add_rejection(reason)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO lead_rejections
                        (workflow_id, batch_id, row_number, reason)
                        VALUES (?, ?, ?, ?)
                        """,
                        (wf_id, batch_id, row_number, reason),
                    )
                    continue

                if status == "valid":
                    # Warnings are only counted for valid (non-rejected) rows
                    stats.field_warnings += row_warnings

                    canonical_phone = parsed["phone"]
                    p_hash = hash_phone(canonical_phone, key=active_hash_key)
                    customer_name = parsed["customer_name"]
                    external_ref = parsed[ref_field_name]
                    preferred_lang = parsed.get("preferred_language")

                    # Extra attributes (all fields except name, phone, ref, language)
                    extra_keys = [
                        k for k in parsed.keys()
                        if k not in ("customer_name", "phone", ref_field_name, "preferred_language")
                    ]
                    incoming_extras = {k: parsed[k] for k in extra_keys if parsed[k] is not None}

                    # Check for duplicate lead: identity is (workflow_id, phone_hash)
                    existing_lead = conn.execute(
                        """
                        SELECT lead_id, batch_id, name_encrypted, external_ref,
                               preferred_language, extra_attributes
                        FROM leads
                        WHERE workflow_id = ? AND phone_hash = ?
                        """,
                        (wf_id, p_hash),
                    ).fetchone()

                    if existing_lead:
                        # Duplicate lead: merge non-null fields, preserve batch_id
                        stats.duplicate_lead_rows_merged += 1

                        lead_id = existing_lead["lead_id"]
                        # Last write wins for non-null incoming values
                        updated_name_enc = (
                            encrypt_string(customer_name, key=active_enc_key)
                            if customer_name is not None
                            else existing_lead["name_encrypted"]
                        )
                        updated_ref = (
                            external_ref
                            if external_ref is not None
                            else existing_lead["external_ref"]
                        )
                        updated_lang = (
                            preferred_lang
                            if preferred_lang is not None
                            else existing_lead["preferred_language"]
                        )

                        # Merge extra attributes JSON
                        existing_extras = {}
                        if existing_lead["extra_attributes"]:
                            try:
                                existing_extras = json.loads(existing_lead["extra_attributes"])
                            except Exception:
                                existing_extras = {}
                        merged_extras = {**existing_extras, **incoming_extras}

                        # Apply schema default if still null
                        if updated_lang is None and "preferred_language" in field_defaults:
                            updated_lang = field_defaults["preferred_language"]

                        conn.execute(
                            """
                            UPDATE leads
                            SET name_encrypted = ?,
                                external_ref = ?,
                                preferred_language = ?,
                                extra_attributes = ?,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE lead_id = ?
                            """,
                            (
                                updated_name_enc,
                                updated_ref,
                                updated_lang,
                                json.dumps(merged_extras) if merged_extras else None,
                                lead_id,
                            ),
                        )

                    else:
                        # New lead: first occurrence sets permanent batch_id
                        stats.leads_created += 1

                        # Encrypt PII
                        enc_phone = encrypt_string(canonical_phone, key=active_enc_key)
                        enc_name = encrypt_string(customer_name, key=active_enc_key)
                        p_last4 = canonical_phone[-4:]

                        # Apply schema defaults to fields that are null
                        final_lang = preferred_lang
                        if final_lang is None and "preferred_language" in field_defaults:
                            final_lang = field_defaults["preferred_language"]

                        conn.execute(
                            """
                            INSERT INTO leads (
                                workflow_id, phone_hash, phone_encrypted, phone_last4,
                                name_encrypted, external_ref, batch_id, preferred_language,
                                extra_attributes
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                wf_id,
                                p_hash,
                                enc_phone,
                                p_last4,
                                enc_name,
                                external_ref,
                                batch_id,
                                final_lang,
                                json.dumps(incoming_extras) if incoming_extras else None,
                            ),
                        )

        # Record batch ingestion in tracking table
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
                "lead_csv",
                stats.lead_rows_total,
                stats.lead_rows_rejected,
                stats.duplicate_lead_rows_merged,
                stats.field_warnings,
            ),
        )

        stats.batches_processed += 1

    return stats


def discover_lead_csv_files(drop_or_leads_dir: Path) -> List[Tuple[Path, str]]:
    """
    Discovers all lead CSV files inside a drop or leads directory.
    Supports structures:
    - <drop_dir>/leads/<workflow_id>/<batch_id>.csv
    - <drop_dir>/<workflow_id>/<batch_id>.csv
    - <leads_dir>/<workflow_id>/<batch_id>.csv

    Returns a sorted list of (csv_path, workflow_id) tuples in filename order.
    """
    schemas = load_all_schemas()
    search_root = drop_or_leads_dir
    leads_sub = drop_or_leads_dir / "leads"
    if leads_sub.is_dir():
        search_root = leads_sub

    discovered: List[Tuple[Path, str]] = []

    for path in search_root.rglob("*.csv"):
        try:
            wf_id = identify_workflow(path, schemas)
            discovered.append((path, wf_id))
        except Exception:
            continue

    # Sort strictly in filename order as per assignment spec
    discovered.sort(key=lambda item: item[0].name)
    return discovered


def ingest_lead_directory(
    dir_path: Path,
    drop_name: str = "drop_1",
    db_path: Optional[str] = None,
    enc_key: Optional[bytes] = None,
    hash_key: Optional[bytes] = None,
) -> LeadIngestionStats:
    """
    Discovers and ingests all lead CSV files from a directory in filename order.
    Aggregates and returns overall ingestion statistics.
    """
    files = discover_lead_csv_files(dir_path)
    overall_stats = LeadIngestionStats()

    for csv_file, wf_id in files:
        file_stats = ingest_lead_csv(
            csv_path=csv_file,
            workflow_id=wf_id,
            drop_name=drop_name,
            db_path=db_path,
            enc_key=enc_key,
            hash_key=hash_key,
        )
        overall_stats.merge(file_stats)

    return overall_stats
