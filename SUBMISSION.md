# Submission

## Candidate
- **Name:** Suravi Ray
- **Assignment chosen:** A (Data – Interaction Ledger & Reporting Pipeline)
- **Time spent (honest estimate):** ~7 hours

## How to run
```bash
# 1. Environment Setup
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. Key Configuration (generate keys or copy .env.example)
cp .env.example .env

# 3. Generate Sample Data
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 4. Ingest Drops & Generate Reports
python -m solution ingest data/drop_1
python -m solution ingest data/drop_2
python -m solution report --out reports/

# 5. Run Full Test Suite (73 passing tests)
python -m pytest -v
```

## Design

### Architecture Overview
The pipeline is designed around a decoupled, layered architecture:
1. **Security & Cryptography (`solution.security`):** Fernet authenticated symmetric encryption (AES-128-CBC + HMAC-SHA256) protects PII (`customer_name`, `phone`) at rest. Deterministic HMAC-SHA256 hashes enable exact equality joins on phone numbers without exposing plaintext numbers.
2. **Ingestion Engine (`solution.lead_ingestion`, `solution.event_ingestion`):** Implements streaming reading, field normalization, schema validation order, duplicate merging (*last-write-wins*), and narrow unique constraint deduplication.
3. **Append-Only Interaction Ledger (`solution.db`):** Telephony events are strictly append-only. Overrides arrive as new rows, preserving complete historical event truth. Linkage state uses tri-state semantics (`NULL` for pending, `0` for linked, `1` for confirmed orphan).
4. **Analytics & Outcomes (`solution.outcomes`):** Evaluates call metrics (connected, telephony failed), resolves effective dispositions (latest override > latest set), and determines lead buckets in strict priority order.
5. **Reporting (`solution.reports`):** Formats and exports the 5 required contract files.

### Database Schema
```text
┌────────────────────┐       ┌────────────────────┐
│ ingestion_batches  │       │       leads        │
├────────────────────┤       ├────────────────────┤
│ batch_key (PK)     │       │ lead_id (PK)       │◄────────┐
│ drop_name          │       │ workflow_id        │         │
│ file_type          │       │ phone_hash         │         │
│ total_rows         │       │ phone_encrypted    │         │
│ duplicate_rows     │       │ phone_last4        │         │
│ rejected_rows      │       │ name_encrypted     │         │
│ warning_count      │       │ external_ref       │         │
└────────────────────┘       │ batch_id           │         │
                             │ extra_attributes   │         │
                             └────────────────────┘         │
                                                            │
┌────────────────────┐       ┌──────────────────────────┐   │
│  lead_rejections   │       │    interaction_ledger    │   │
├────────────────────┤       ├──────────────────────────┤   │
│ id (PK)            │       │ ledger_id (PK)           │   │
│ workflow_id        │       │ event_id (UNIQUE)        │   │
│ batch_id           │       │ event_type               │   │
│ row_number         │       │ workflow_id              │   │
│ reason             │       │ call_id                  │   │
└────────────────────┘       │ occurred_at_raw          │   │
                             │ occurred_at_utc          │   │
                             │ occurred_at_ist          │   │
                             │ lead_id (FK) ────────────┼───┘
                             │ phone_hash               │
                             │ phone_encrypted          │
                             │ payload                  │
                             │ is_orphan (NULL/0/1)     │
                             └──────────────────────────┘
```

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| **HMAC-SHA256 for Phone Matching** | Prevents rainbow table and brute-force attacks against 10-digit phone numbers while supporting $O(1)$ indexed equality joins. | Plain SHA-256 (vulnerable to precomputed tables) or searching encrypted strings (impossible with randomized IVs). |
| **Tri-State `is_orphan` (`NULL`, `0`, `1`)** | Distinguishes newly ingested events awaiting linkage (`NULL`) from successfully linked events (`0`) and confirmed orphans (`1`). | Binary boolean flag (`0`/`1`), which prematurely labels newly ingested events as orphans before batch processing completes. |
| **Deterministic Tie-Breaking on Timestamps** | When multiple overrides or sets share identical timestamps, `ledger_id DESC` picks the latest record appended to the ledger. | Random choice or raising an error. |
| **Orphan Retry on Lead Arrival** | Telephony events frequently arrive in Drop 1 before leads are uploaded in Drop 2. Automatically matching pending events upon lead arrival guarantees self-healing data. | Batch re-processing of raw event files (violates idempotency and wastes compute). |
| **Separate `lead_rejections` Table** | Keeps corrupted/invalid CSV rows out of the clean `leads` table while providing a dedicated audit log with zero PII. | Adding a `status = 'rejected'` column on `leads` (pollutes primary lead queries). |
| **WAL Mode for SQLite** | Enables concurrent readers without blocking writers, boosting throughput during large stream ingestions. | Default rollback journal mode. |

## Failure modes considered
1. **Duplicate Ingestion & Drop Re-runs:** Tracked in `ingestion_batches`. If a drop or file is processed again, it returns zero processed rows without duplicating records or re-merging duplicate counters.
2. **Crash Mid-File:** Every file runs inside an atomic SQLite transaction (`@contextmanager get_db()`). If an exception occurs, all database insertions roll back completely.
3. **Malformed Input & Corrupt JSON:** Validated line-by-line. Malformed lines, non-dictionary payloads, or missing core fields are counted in `malformed_events` and skipped without crashing the pipeline.
4. **Timestamp Parsing Failures:** Handled gracefully. If an unparseable timestamp arrives, raw content is preserved in `occurred_at_raw` and processed without halting ingestion.
5. **Narrow Integrity Handling:** Only unique constraint violations on `event_id` are suppressed as duplicates. Foreign key failures or disk errors are immediately re-raised.

## Testing
- **73 Automated Tests** covering all components:
  - `test_db.py`: Database connection, WAL mode, foreign keys, table creation, legacy schema migration.
  - `test_security.py`: Authenticated encryption, HMAC hashing, tamper resistance, key format checks.
  - `test_normalizers.py`: Headers, Indian phone normalization, multi-format dates, decimals, enums, timestamps.
  - `test_lead_ingestion.py`: CSV parsing, alias matching, schema-ordered rejections, field warnings, duplicate merging.
  - `test_event_ingestion.py`: Streaming reading, malformed JSON detection, duplicate event suppression, ledger storage.
  - `test_event_linking.py`: Phone-based lead linking, orphan quarantine, out-of-order drop resolution, override call-based linking.
  - `test_outcomes.py`: Call connection, telephony failure, effective disposition hierarchy, lead bucket priority order.
  - `test_reports.py`: Header matching, row formatting, connectivity rate 4-decimal precision, phone masking, empty database handling.
  - `test_cli_integration.py`: End-to-end CLI commands (`init-db`, `ingest`, `report`, `run`), idempotency, and multi-drop resolution.

## Results
Execution against synthetic benchmark data (`--seed 42 --scale small`):
- **Drop 1 Ingestion:** 1515 lead rows processed (1415 created, 29 merged, 71 rejected). 4247 event rows processed (4160 recorded, 83 duplicates dropped). 1123 events quarantined as orphans.
- **Drop 2 Ingestion:** 542 lead rows processed (477 created, 28 merged, 37 rejected). 4498 event rows processed (4340 recorded, 154 duplicates dropped).
- **Out-of-Order Resolution:** Orphan count dropped from **1123 down to 462** as delayed Drop 1 events were linked to Drop 2 leads.
- **Report Generation:** All 5 reports generated in under 1 second.
- **Data Quality:** Exact match with expected anomaly metrics (108 total rejections, 57 merged duplicates, 122 warnings, 8 malformed events, 237 duplicate events dropped, 19 unknown dispositions).

## If I had another day
1. **Chunked Streaming Aggregation:** For the `--scale large` dataset (~140k leads, ~600k events), stream report aggregation via SQL window functions (`ROW_NUMBER() OVER (...)`) to eliminate in-memory Python structures.
2. **PostgreSQL Adapter:** Implement an optional Postgres connection mode using the same interfaces via `psycopg3`.
3. **Parquet Export:** Add optional Parquet output alongside CSV for analytical data lake ingestion.
