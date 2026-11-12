# Agentic Universe: Junior SDE Take-Home (Data & Workflow)

Thanks for your interest in Agentic Universe. We build AI voice agents for insurers, lenders and
consumer brands across India and APAC. Our platform has three layers:

- **Data:** leads, call events, dispositions and the interaction ledger that every client report
  is built on.
- **Workflow:** campaigns, scheduling, dialling, retries and callbacks, all orchestrated durably.
- **Application:** dashboards, consoles and APIs.

This repository contains the complete implementation for **Assignment A: Interaction Ledger & Reporting Pipeline**.

---

## Assignment A: Interaction Ledger & Reporting Pipeline

### Implementation Status: Fully Complete
All requirements of Assignment A have been implemented, tested, and verified:
1. **Lead Ingestion & Normalisation Engine:** Schema alias matching, phone (+91 canonical), date, decimal, enum normalisation, rejection audits (no PII), warnings, duplicate merging (last-write-wins), and idempotency.
2. **PII Security & Cryptography:** Fernet authenticated encryption (AES-128-CBC + HMAC-SHA256) for names and phones; HMAC-SHA256 deterministic hashing for phone matching at rest.
3. **Append-Only Interaction Ledger:** SQLite schema with WAL journal mode, safe schema migrations, deduplication with narrow IntegrityError handling, and append-only preservation of original event history.
4. **Event-to-Lead Linking & Orphan Handling:** Phone-based linking, `call_id`-based linking for `disposition.override`, tri-state orphan modeling (`NULL`, `0`, `1`), and automatic retry resolution when delayed leads arrive in later drops.
5. **Timestamp & Disposition Normalisation:** Standardizes UTC (`Z`), offset (`+05:30`), and naive timestamps (interpreted as IST); validates against `dispositions.csv`; calculates effective call disposition (latest override > latest set).
6. **Call Outcomes & Lead Bucketing:** Strict evaluation of lead buckets: `yet_to_reach_out` -> `completed` -> `in_progress_telephony_failed` -> `in_progress_dialed`.
7. **All Five Output Reports:** `batch_summary.csv`, `daily_overview.csv`, `lead_report.csv`, `rejections.csv`, and `data_quality.json`.
8. **Command-Line Interface:** Standard library CLI supporting `ingest`, `report`, `run`, and `init-db`.

---

## Setup & Running Instructions

### 1. Environment Setup
```bash
# Clone the repository and enter the directory
cd sde-assignment-junior-2026-main

# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\Activate.ps1

# Install dependencies (cryptography and pytest)
pip install -r requirements.txt

# Configure environment secrets
cp .env.example .env
# Set PII_ENCRYPTION_KEY and PHONE_HASH_KEY (or use existing .env)
```

### 2. Generate Synthetic Test Data
```bash
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
```

### 3. Run Pipeline
```bash
# Ingest Drop 1
python -m solution ingest data/drop_1

# Ingest Drop 2 (automatically links delayed events from Drop 1)
python -m solution ingest data/drop_2

# Generate all 5 reports
python -m solution report --out reports/

# Or run complete pipeline sequentially
python -m solution run data/drop_1 data/drop_2 --out reports/
```

### 4. Run Automated Test Suite
```bash
python -m pytest -v
```
All **73 unit and integration tests** pass.

---

## Reports Generated in `reports/`
- `batch_summary.csv`: Aggregated lead bucket counts per workflow and batch.
- `daily_overview.csv`: Daily call volumes, connected calls, 4-decimal connectivity rate, and success leads.
- `lead_report.csv`: Per-lead status, masked phones (`+91******XXXX`), last call IST timestamp, current disposition, and success flag.
- `rejections.csv`: Detailed audit log of rejected CSV rows with reasons (strictly no PII).
- `data_quality.json`: Consolidated data pipeline health metrics (total rows, rejections, duplicates, orphans, unknown dispositions).
