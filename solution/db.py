"""
Database module for Assignment A: Interaction Ledger & Reporting Pipeline.

Provides SQLite connection management, schema definitions, and idempotent
initialization logic.
"""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Generator, Optional, Union

from solution.config import DEFAULT_DB_PATH

# SQL statements defining the core schema
SCHEMA_SQL = """
-- 1. Ingestion tracking table: ensures idempotent drop processing
CREATE TABLE IF NOT EXISTS ingestion_batches (
    batch_key TEXT PRIMARY KEY,
    drop_name TEXT NOT NULL,
    file_type TEXT NOT NULL,
    total_rows INTEGER DEFAULT 0,
    rejected_rows INTEGER DEFAULT 0,
    duplicate_rows INTEGER DEFAULT 0,
    warning_count INTEGER DEFAULT 0,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Leads table: stores cleaned, validated, and merged leads
CREATE TABLE IF NOT EXISTS leads (
    lead_id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    phone_hash TEXT NOT NULL,
    phone_encrypted TEXT NOT NULL,
    phone_last4 TEXT NOT NULL,
    name_encrypted TEXT,
    external_ref TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    preferred_language TEXT DEFAULT 'en',
    extra_attributes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(workflow_id, phone_hash)
);

-- 3. Interaction Ledger: append-only log of telephony and disposition events
CREATE TABLE IF NOT EXISTS interaction_ledger (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL,
    occurred_at_ist TEXT NOT NULL,
    lead_id INTEGER REFERENCES leads(lead_id),
    phone_hash TEXT,
    payload TEXT,
    is_orphan INTEGER DEFAULT 0,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 4. Rejections table: stores audit log of rejected CSV rows (strictly no PII)
CREATE TABLE IF NOT EXISTS lead_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    reason TEXT NOT NULL,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(workflow_id, batch_id, row_number)
);

-- Performance and lookup indexes
CREATE INDEX IF NOT EXISTS idx_leads_wf_phone ON leads(workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS idx_ledger_call_id ON interaction_ledger(call_id);
CREATE INDEX IF NOT EXISTS idx_ledger_lead_id ON interaction_ledger(lead_id);
CREATE INDEX IF NOT EXISTS idx_ledger_orphan ON interaction_ledger(workflow_id, phone_hash, is_orphan);
"""


def get_connection(db_path: Optional[Union[str, Path]] = None) -> sqlite3.Connection:
    """
    Creates and configures a SQLite connection.

    - Enables foreign key constraint enforcement (disabled by default in SQLite).
    - Sets row_factory to sqlite3.Row for convenient name-based column access.
    - Enables WAL (Write-Ahead Logging) journal mode for file-based databases to
      improve write throughput and concurrent reads.
    """
    target_path = str(db_path if db_path is not None else DEFAULT_DB_PATH)
    is_uri = target_path.startswith("file:")
    conn = sqlite3.connect(target_path, uri=is_uri)
    conn.row_factory = sqlite3.Row

    # SQLite does not enforce foreign keys by default; enable explicitly
    conn.execute("PRAGMA foreign_keys = ON;")

    # Use WAL mode for persistent files (skip for in-memory databases)
    if not (target_path == ":memory:" or "mode=memory" in target_path):
        conn.execute("PRAGMA journal_mode = WAL;")

    return conn


@contextmanager
def get_db(db_path: Optional[Union[str, Path]] = None) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager that yields a connection, automatically commits transactions
    on normal exit, rolls back on exceptions, and ensures the connection is closed.
    """
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Optional[Union[str, Path]] = None) -> None:
    """
    Initializes the database schema if tables do not exist.
    This operation is safe to run multiple times (idempotent).
    """
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
