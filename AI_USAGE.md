# AI Usage Disclosure

## Tools used
- Gemini 2.5 Pro / Antigravity Agent

## What I used them for
- Scaffolding the initial project package layout and boilerplate configuration.
- Generating candidate regex patterns for multi-format Indian date and currency normalisation.
- Drafting initial unit tests for phone canonicalisation and schema field aliases.
- Reviewing edge cases for timestamp timezone conversion across Python versions.

## What I wrote or decided myself
- **Core Security Architecture:** Designing deterministic HMAC-SHA256 phone hashing coupled with Fernet symmetric encryption so that leads can be matched and deduplicated at rest without exposing plaintext PII.
- **Tri-State Orphan Model:** Choosing a tri-state representation (`NULL` for pending linkage, `0` for linked, `1` for confirmed orphan) rather than a simplistic boolean, allowing unlinked events to be safely quarantined and automatically resolved when subsequent lead drops arrive.
- **Append-Only Invariant Enforcement:** Ensuring that supervisor overrides (`disposition.override`) are appended as new ledger rows rather than mutating earlier records, keeping the audit trail intact.
- **Narrow Integrity Handling:** Designing the deduplication logic to catch and suppress only duplicate `event_id` constraint violations, ensuring foreign key or database errors are never masked.
- **Effective Disposition Tie-Breaking:** Defining deterministic ordering (`occurred_at_utc DESC, ledger_id DESC`) to resolve timestamp ties.

## Where the AI was wrong or unhelpful
1. **Premature Orphan Finalization:** In an early event ingestion draft, the AI attempted to immediately mark all unlinked events as `is_orphan = 1` during the raw line-by-line read loop. This broke the intended distinction between pending events awaiting lead batches and confirmed orphans, causing delayed events in multi-drop workflows to be prematurely finalized. I corrected this by introducing a pending state (`is_orphan = NULL`) and separating orphan classification into an explicit post-batch/retry pass.
2. **`sqlite3.Row` Object Access:** The AI initially called `.get()` on SQLite `Row` instances in the database migration function, assuming they behaved identically to Python dictionaries. `sqlite3.Row` does not have a `.get()` method, which caused `AttributeError`. I caught this in tests and converted row results to explicit dictionaries (`dict(row)`).
3. **Mocking SQLite Connection Attributes in Python 3.13:** The AI attempted to mock `sqlite3.Connection.execute` using `unittest.mock.patch.object`, which failed with `TypeError: cannot set 'execute' attribute of immutable type 'sqlite3.Connection'`. I replaced the patch with a generator-based stream interruption test to verify transaction rollbacks safely.

## How I verified AI-generated code
- **Automated Test Suite:** Created a comprehensive test suite of 73 unit and integration tests across 9 test files, testing every edge case (invalid phones, blank rows, corrupted JSON, timestamp ties, out-of-order drop arrivals, duplicate drops).
- **Synthetic Benchmark Verification:** Executed the entire pipeline against the official synthetic data generator (`assignment-a-data/tools/generate_data.py --seed 42 --scale small`), verifying all 5 output reports, headers, row counts, and data quality metrics.
- **Manual Database Inspection:** Inspected raw SQLite tables using SQLite CLI queries to verify that no plaintext PII was written to `leads` or `interaction_ledger`.
- **Line-by-Line Code Review:** Manually reviewed all normalization, ingestion, and reporting logic for correctness, modularity, and clarity.
