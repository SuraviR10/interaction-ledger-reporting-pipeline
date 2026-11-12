# Solution: Interaction Ledger & Reporting Pipeline

This directory contains the production-grade implementation of **Assignment A: Interaction Ledger & Reporting Pipeline**.

---

## 1. Architecture Overview

The pipeline is organized into modular, testable components with zero unnecessary external dependencies (uses standard library + `cryptography` + `pytest`):

```text
solution/
├── __init__.py           # Package indicator
├── __main__.py           # CLI entrypoint for `python -m solution`
├── cli.py                # Command-line interface (`ingest`, `report`, `run`, `init-db`)
├── config.py             # Centralised path, timezone, and environment configuration
├── db.py                 # SQLite connection management, schema initialisation, WAL mode, migrations
├── security.py           # Authenticated symmetric encryption (Fernet) and HMAC-SHA256 phone hashing
├── normalizers.py        # Header, Indian phone (+91), multi-format dates, decimals, enums, timestamps
├── lead_ingestion.py     # CSV parsing, schema validation order, duplicate merging, DB persistence
├── event_ingestion.py    # JSONL event stream reader, deduplication, phone linking, orphan quarantine & retries
├── outcomes.py           # Call metrics, effective disposition hierarchy, and lead bucket evaluation
└── reports.py            # Business report generation (all 5 contract files)
```

---

## 2. Completed Pipeline Stages

### Stage 1: Database Foundation (`solution.db`)
- SQLite connection management with foreign key enforcement (`PRAGMA foreign_keys = ON;`).
- Write-Ahead Logging (`PRAGMA journal_mode = WAL;`) for disk-backed concurrency.
- Dictionary-like row access via `sqlite3.Row`.
- Atomic transactional commits and rollbacks via `@contextmanager get_db()`.
- Safe schema migration (`migrate_db`) supporting upgrades from legacy schemas without data loss.

### Stage 2: PII Security & Encryption Foundation (`solution.security`)
- **Fernet Authenticated Encryption:** AES-128 in CBC mode with PKCS7 padding and HMAC-SHA256 authentication for sensitive fields (`customer_name`, `phone`).
- **Deterministic HMAC-SHA256 Hashing:** Used for `(workflow_id, phone_hash)` lead identity and event matching without storing plaintext numbers.
- **Environment Key Management:** Secrets loaded from `.env` via standard library parser; format-validated on access.

### Stage 3: Lead CSV Ingestion & Normalisation (`solution.lead_ingestion`, `solution.normalizers`)
- **Header Matching:** Trims, lowercases, collapses underscores/spaces to match schema `name` or `aliases`.
- **Row Filtering & Numbering:** Blank rows skipped and not counted; row numbers start at 1.
- **Field Normalisation:**
  - Phone: Indian 10-digit mobile numbers canonicalized to `+91XXXXXXXXXX`.
  - Date: Multi-format day-first dates (`YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD-Mon-YYYY`).
  - Decimal: Strips currency symbols (`₹`, `Rs`, `INR`) and thousands commas; rounds to 2 places.
  - Enum: Case-insensitive match against allowed schema values; stored lowercase.
- **Validation Order & Rejection Audit:** Checked strictly in schema order. First failure recorded in `lead_rejections` (strictly without PII).
- **Duplicate Merging:** Last-write-wins for non-null incoming fields while permanently retaining original `batch_id`.
- **Defaults:** Applied to null fields after merging.

### Stage 4: Event Processing & Append-Only Interaction Ledger (`solution.event_ingestion`)
- **Streaming Parser:** Line-by-line reading; malformed JSON and missing core fields tracked and skipped.
- **Append-Only Invariant:** Records inserted into `interaction_ledger`; original event history is never overwritten or deleted.
- **Deduplication:** Initial `SELECT` check combined with narrow `IntegrityError` suppression on `event_id`.
- **Stage 4C1 – Lead Linking:** Decrypts phone in memory, canonicalizes, computes HMAC-SHA256, and links to `leads(lead_id)`. Plaintext phone is never logged or stored.
- **Stage 4C2 – Orphan Handling & Retries:**
  - Events that cannot currently link remain pending (`is_orphan = NULL`).
  - Malformed phones are confirmed orphans (`is_orphan = 1`).
  - `disposition.override` without phone links via `call_id` to other events in the same call.
  - `retry_orphan_linking(conn)`: Retries pending/orphan events automatically when new leads arrive (e.g. Drop 2).
- **Stage 4C3 – Timestamps & Dispositions:**
  - Parses UTC (`Z`), offset (`+05:30`), and naive (interpreted as IST). Stores normalized `occurred_at_utc` and `occurred_at_ist`.
  - Validates disposition codes against `dispositions.csv` per workflow. Unknown codes increment `unknown_disposition_events` and are ignored in effective disposition calculations.

### Stage 5: Call Outcomes & Lead Buckets (`solution.outcomes`)
- Calls counted only if linked `call.initiated` event is present. Call date is the IST date.
- Connected calls identified via `call.connected`.
- **Effective Disposition:** Latest valid `disposition.override` by timestamp; else latest valid `disposition.set`; deterministic tie-break using `ledger_id DESC`.
- **Lead Buckets (Exact Priority Order):**
  1. `yet_to_reach_out`: 0 calls.
  2. `completed`: any call has a terminal disposition (`is_terminal = true`).
  3. `in_progress_telephony_failed`: every call has a `call.telephony_failed` event.
  4. `in_progress_dialed`: all other cases.

### Stage 6: Business Report Generation (`solution.reports`)
Generates all five required files matching the exact specification:
1. `batch_summary.csv`: Aggregations per `workflow_id, batch_id`.
2. `daily_overview.csv`: Metrics per `workflow_id, date` (`connectivity_rate` formatted to 4 decimals, `success_leads` distinct count).
3. `lead_report.csv`: Per-lead metrics (`phone_masked` as `+91******` + last 4, `last_call_at` in ISO-8601 IST, lowercase booleans).
4. `rejections.csv`: Rejection audit log (strictly no PII).
5. `data_quality.json`: Final consolidated pipeline health metrics.

### Stage 7: CLI & End-to-End Execution (`solution.cli`)
- `python -m solution ingest <drop_path>`: Ingests lead CSVs, event JSONLs, and runs orphan resolution.
- `python -m solution report --out <out_dir>`: Generates all five reports.
- `python -m solution run <drop_paths...> --out <out_dir>`: Multi-drop sequential ingestion and reporting.
- `python -m solution init-db`: Schema initialization and verification.

---

## 3. How to Run

### Setup Environment
```bash
# 1. Activate virtual environment
source .venv/bin/activate  # On Windows: .venv\Scripts\Activate.ps1

# 2. Configure environment keys (see .env.example)
cp .env.example .env
# Ensure PII_ENCRYPTION_KEY and PHONE_HASH_KEY are set
```

### Ingestion and Reporting Commands
```bash
# Ingest Drop 1
python -m solution ingest data/drop_1

# Ingest Drop 2 (incrementally resolves delayed orphans)
python -m solution ingest data/drop_2

# Generate Reports
python -m solution report --out reports/

# Or run complete multi-drop pipeline in one step
python -m solution run data/drop_1 data/drop_2 --out reports/
```

---

## 4. Test Suite

The solution includes 73 automated unit and integration tests across 9 test suites:
```bash
python -m pytest -v
```

Test breakdown:
- `tests/test_db.py`: Connection, WAL mode, foreign keys, migrations (7 tests).
- `tests/test_security.py`: Fernet encryption, HMAC hashing, key validation (10 tests).
- `tests/test_normalizers.py`: Headers, phones, dates, decimals, enums, timestamps (10 tests).
- `tests/test_lead_ingestion.py`: CSV parsing, rejections, duplicate merging, defaults (8 tests).
- `tests/test_event_ingestion.py`: JSONL reading, parsing, duplicate suppression, ledger storage (17 tests).
- `tests/test_event_linking.py`: Lead linking, orphan resolution, out-of-order drops, override linking (7 tests).
- `tests/test_outcomes.py`: Call metrics, effective disposition, bucket priority order (9 tests).
- `tests/test_reports.py`: Output file generation, headers, masking, formatting (2 tests).
- `tests/test_cli_integration.py`: End-to-end CLI execution, idempotency, multi-drop resolution (3 tests).
