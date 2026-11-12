# Solution: Interaction Ledger & Reporting Pipeline

This directory contains the application code for **Assignment A**.

---

## 1. Architecture Overview

The solution is structured as a modular Python package:
```text
solution/
├── __init__.py      # Package indicator
├── config.py        # Centralized path and environment configuration
├── db.py            # SQLite connection management and schema initialisation
└── security.py      # PII authenticated encryption and deterministic phone hashing
```

---

## 2. Database Foundation (Part 1)

Database operations use standard library `sqlite3` with strict integrity constraints:
- **Foreign keys:** Explicitly enabled via `PRAGMA foreign_keys = ON;`.
- **Concurrency:** Uses Write-Ahead Logging (`PRAGMA journal_mode = WAL;`) for disk-backed databases.
- **Row access:** Uses `sqlite3.Row` for dictionary-like name-based column access.
- **Transactions:** Handled via the `@contextmanager get_db(db_path)` helper, ensuring atomic commits and automatic rollbacks on error.

### Database Tables:
1. `ingestion_batches`: Tracks ingested files for drop-level idempotency.
2. `leads`: Stores cleaned leads with encrypted PII, masked phone suffix, and HMAC lookup hash.
3. `interaction_ledger`: Append-only event log with orphan tracking and IST/UTC timestamps.
4. `lead_rejections`: Audit table for invalid CSV rows (strictly no PII).

---

## 3. PII Security & Encryption Foundation (Part 2)

Per the assignment specification, sensitive customer data (`"pii": true` fields such as `customer_name` and `phone`) must not be stored in plaintext.

### Security Mechanisms:

1. **Fernet Authenticated Symmetric Encryption (`solution.security.encrypt_string` / `decrypt_string`)**:
   - Used for `customer_name` and canonical `phone`.
   - Built on AES-128 in CBC mode with PKCS7 padding and authenticated via HMAC-SHA256.
   - Any ciphertext tampering or invalid key raises an `InvalidToken` error.

2. **Deterministic Phone Hashing (`solution.security.hash_phone`)**:
   - Used for lead identity `(workflow_id, phone_hash)`, deduplication, and event-to-lead linking.
   - Built using **HMAC-SHA256** with a secret key.
   - *Why not ordinary SHA-256?* Indian phone numbers have a relatively small space (~4 billion valid 10-digit mobile numbers). An unsalted or unkeyed SHA-256 hash can be reversed using precomputed rainbow tables or brute-force dictionaries. Using HMAC with a secret key ensures that an attacker cannot compute or look up phone hashes without possessing the secret key.

---

## 4. Key Management & Environment Variables

Keys are never hardcoded and must be loaded from environment variables (or a local `.env` file that is excluded from Git):

| Variable | Purpose | Format |
|---|---|---|
| `PII_ENCRYPTION_KEY` | Symmetric key for Fernet encryption/decryption | 32-byte url-safe base64 string |
| `PHONE_HASH_KEY` | Secret key for deterministic HMAC-SHA256 hashing | Secure hex or random string |
| `LEDGER_DB_PATH` | (Optional) Path to SQLite database file | Path (defaults to `./ledger.db`) |

### How to Generate Keys:
```bash
# Generate Fernet Encryption Key:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Generate HMAC Phone Hashing Key:
python -c "import secrets; print(secrets.token_hex(32))"
```

A template file [`.env.example`](../.env.example) is provided in the repository root.

---

## 5. Running Tests

Run the test suite using Python's standard `unittest`:
```powershell
python -m unittest discover tests
```
Or via `pytest`:
```powershell
pytest
```

---

## 6. Current Implementation Status & Limitations

- **Completed:**
  - Part 1: SQLite schema initialisation, connection management, transactions.
  - Part 2: PII authenticated encryption, deterministic HMAC phone hashing, key validation, environment loading.
- **Current Limitations / Upcoming in Next Parts:**
  - CSV lead ingestion & header mapping not yet integrated.
  - JSONL event ingestion & orphan resolution not yet integrated.
  - Reporting and metrics not yet implemented.
