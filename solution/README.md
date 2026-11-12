# Solution: Interaction Ledger & Reporting Pipeline

This directory contains the application code for **Assignment A: Interaction Ledger & Reporting Pipeline**.

---

## 1. Architecture Overview

The solution is organised into focused, testable modules:
```text
solution/
├── __init__.py           # Package indicator
├── config.py             # Centralised path, timezone, and environment configuration
├── db.py                 # SQLite connection management and schema initialisation
├── normalizers.py        # Header, phone, date, decimal, and enum normalisation
├── lead_ingestion.py     # CSV parsing, schema validation, duplicate merging, and DB persistence
└── security.py           # Authenticated symmetric encryption (Fernet) and HMAC phone hashing
```

---

## 2. Database Foundation (Part 1)

Database operations use standard library `sqlite3` with strict integrity constraints:
- **Foreign keys:** Explicitly enabled via `PRAGMA foreign_keys = ON;`.
- **Concurrency:** Uses Write-Ahead Logging (`PRAGMA journal_mode = WAL;`) for disk-backed databases.
- **Row access:** Uses `sqlite3.Row` for dictionary-like name-based column access.
- **Transactions:** Handled via the `@contextmanager get_db(db_path)` helper, ensuring atomic commits and automatic rollbacks on error.

### Database Tables:
1. `ingestion_batches`: Tracks ingested files for drop-level idempotency and records row counts.
2. `leads`: Stores cleaned leads with encrypted PII, masked phone suffix, and HMAC lookup hash.
3. `interaction_ledger`: Append-only event log with orphan tracking and IST/UTC timestamps.
4. `lead_rejections`: Audit table for invalid CSV rows (strictly no PII).

---

## 3. PII Security & Encryption Foundation (Part 2)

Per the assignment specification, sensitive customer data (`"pii": true` fields such as `customer_name` and `phone`) are never stored in plaintext.

### Security Mechanisms:
1. **Fernet Authenticated Symmetric Encryption (`solution.security.encrypt_string` / `decrypt_string`)**:
   - Used for `customer_name` and canonical `phone`.
   - Built on AES-128 in CBC mode with PKCS7 padding and authenticated via HMAC-SHA256.
   - Any ciphertext tampering or invalid key raises an `InvalidToken` error.

2. **Deterministic Phone Hashing (`solution.security.hash_phone`)**:
   - Used for lead identity `(workflow_id, phone_hash)`, deduplication, and event-to-lead linking.
   - Built using **HMAC-SHA256** with a secret key.
   - Prevents rainbow table and dictionary attacks against the small search space of Indian mobile numbers.

---

## 4. Lead CSV Ingestion & Normalisation (Part 3)

The lead ingestion engine processes dirty customer CSV uploads against per-workflow JSON schemas.

### Ingestion Workflow & Rules:

1. **File Discovery & Workflow Mapping (`discover_lead_csv_files`, `identify_workflow`)**:
   - Reads CSV files in sorted filename order (`batch_001.csv`, `batch_002.csv`, ...).
   - Identifies workflow by directory convention (`leads/<workflow_id>/<batch_id>.csv`) or header inspection fallback.
   - Uses the file stem (e.g. `batch_001`) as the permanent `batch_id`.

2. **Header Normalisation (`solution.normalizers.normalize_header_name`, `match_header_to_field`)**:
   - Converts headers to lowercase, strips whitespace, and collapses runs of underscores/whitespace into single spaces.
   - Matches against schema `name` or `aliases`.
   - Unknown headers are ignored.

3. **Row Filtering & Numbering**:
   - Completely blank rows (all cells empty or whitespace) are skipped and not counted.
   - Data row numbers count non-blank rows starting at 1. The header row is not counted.
   - Values are trimmed; empty strings are treated as missing (`None`).

4. **Data Normalisation (`solution.normalizers`)**:
   - **Phone:** Strips non-digits, strips leading `91` (if 12 digits) or `0` (if 11 digits), validates 10 digits starting with 6/7/8/9. Canonical format: `+91XXXXXXXXXX`.
   - **Date:** Accepts `YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD-Mon-YYYY` (day-first). Rejects impossible dates (e.g. 31/02/2026).
   - **Decimal:** Strips `₹`, `Rs`, `Rs.`, `INR`, commas. Must be $\ge 0$. Rounds to 2 decimal places.
   - **Enum:** Case-insensitive match against allowed schema values; stored in lowercase.

5. **Validation, Rejections & Warnings**:
   - Validated strictly in schema field order.
   - Missing required field $\rightarrow$ `missing_required:<field_name>`.
   - Present phone number invalid $\rightarrow$ `invalid_phone`.
   - First failure becomes the row's single rejection reason. Rejected rows are recorded in `lead_rejections` (strictly without PII) and excluded from `leads`.
   - Optional invalid fields become `None` and increment the `field_warnings` counter. Warnings are only counted for valid (non-rejected) rows.

6. **Duplicate Merging (Last Write Wins)**:
   - Unique lead identity is `(workflow_id, canonical phone)` identified by `(workflow_id, phone_hash)`.
   - The first valid occurrence creates the lead record and permanently assigns its `batch_id`.
   - Later valid occurrences increment `duplicate_lead_rows_merged` and overwrite only fields where the incoming value is non-null (*last write wins*).
   - Schema defaults (e.g. `preferred_language = "en"`) apply after merging to fields that remain null.

---

## 5. Running Tests

Run all 35 unit tests using Python's standard `unittest` or `pytest`:
```powershell
python -m unittest discover tests
# or
python -m pytest -v
```

---

## 6. Current Implementation Status & Limitations

- **Completed:**
  - Part 1: SQLite schema initialisation, connection management, transactions.
  - Part 2: PII authenticated encryption, deterministic HMAC phone hashing, key validation, environment loading.
  - Part 3: Lead CSV ingestion, header mapping, phone/date/decimal/enum normalisation, rejections audit, warnings, duplicate merging, idempotency.
- **Current Limitations / Upcoming in Next Parts:**
  - JSONL event ingestion & orphan resolution (Part 4).
  - Business metrics & 5 output reports (Part 5).
  - CLI runner (Part 6).
