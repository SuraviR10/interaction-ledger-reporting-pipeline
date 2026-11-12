"""
Unit tests for database connection handling, schema initialization, and constraints.
Compatible with both standard library `unittest` and `pytest`.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from solution.db import get_connection, get_db, init_db


class DatabaseInitAndConnectionTests(unittest.TestCase):
    def setUp(self):
        """Create a fresh temporary database file for each test."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_ledger.db"
        init_db(self.db_path)

    def tearDown(self):
        """Clean up the temporary directory."""
        self.temp_dir.cleanup()

    def test_connection_settings(self):
        """Verify that foreign keys are enabled and rows can be accessed by column name."""
        with get_db(self.db_path) as conn:
            cursor = conn.execute("PRAGMA foreign_keys;")
            fk_enabled = cursor.fetchone()[0]
            self.assertEqual(fk_enabled, 1, "Foreign keys must be enabled (PRAGMA foreign_keys = 1)")

            # Test row_factory allows column name access
            cursor = conn.execute("SELECT 1 AS num, 'test' AS label;")
            row = cursor.fetchone()
            self.assertEqual(row["num"], 1)
            self.assertEqual(row["label"], "test")

    def test_all_expected_tables_exist(self):
        """Verify that all core tables are created upon initialization."""
        expected_tables = {
            "ingestion_batches",
            "leads",
            "interaction_ledger",
            "lead_rejections",
        }
        with get_db(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
            )
            created_tables = {row["name"] for row in cursor.fetchall()}
            self.assertTrue(
                expected_tables.issubset(created_tables),
                f"Missing tables: {expected_tables - created_tables}",
            )

    def test_init_db_is_idempotent(self):
        """Calling init_db multiple times must succeed without error."""
        try:
            init_db(self.db_path)
            init_db(self.db_path)
        except Exception as exc:
            self.fail(f"init_db raised an unexpected exception on repeated calls: {exc}")

    def test_leads_unique_constraint(self):
        """A lead is unique on (workflow_id, phone_hash). Duplicates must fail."""
        with get_db(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO leads (
                    workflow_id, phone_hash, phone_encrypted, phone_last4,
                    name_encrypted, external_ref, batch_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                ("motor_renewal", "hash_abc123", "enc_phone", "0036", "enc_name", "POL-1", "batch_001"),
            )

        # Attempt inserting duplicate (workflow_id, phone_hash)
        with self.assertRaises(sqlite3.IntegrityError):
            with get_db(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO leads (
                        workflow_id, phone_hash, phone_encrypted, phone_last4,
                        name_encrypted, external_ref, batch_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    ("motor_renewal", "hash_abc123", "enc_phone2", "0036", "enc_name2", "POL-2", "batch_002"),
                )

    def test_interaction_ledger_unique_event_id(self):
        """Each event_id must be unique across the entire interaction ledger."""
        with get_db(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO interaction_ledger (
                    event_id, event_type, workflow_id, call_id,
                    occurred_at_utc, occurred_at_ist, phone_hash, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    "evt_00000001",
                    "call.initiated",
                    "motor_renewal",
                    "call_00000001",
                    "2026-10-01T09:00:00Z",
                    "2026-10-01T14:30:00+05:30",
                    "hash_abc123",
                    "{}",
                ),
            )

        # Inserting duplicate event_id must raise IntegrityError
        with self.assertRaises(sqlite3.IntegrityError):
            with get_db(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO interaction_ledger (
                        event_id, event_type, workflow_id, call_id,
                        occurred_at_utc, occurred_at_ist, phone_hash, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        "evt_00000001",
                        "call.connected",
                        "motor_renewal",
                        "call_00000001",
                        "2026-10-01T09:00:15Z",
                        "2026-10-01T14:30:15+05:30",
                        "hash_abc123",
                        "{}",
                    ),
                )

    def test_lead_rejections_unique_constraint(self):
        """Rejections table must enforce UNIQUE(workflow_id, batch_id, row_number)."""
        with get_db(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO lead_rejections (workflow_id, batch_id, row_number, reason)
                VALUES (?, ?, ?, ?);
                """,
                ("motor_renewal", "batch_001", 10, "invalid_phone"),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            with get_db(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO lead_rejections (workflow_id, batch_id, row_number, reason)
                    VALUES (?, ?, ?, ?);
                    """,
                    ("motor_renewal", "batch_001", 10, "missing_required:customer_name"),
                )

    def test_transaction_rollback_on_error(self):
        """Ensure get_db rolls back transactions if an exception occurs inside the block."""
        try:
            with get_db(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO ingestion_batches (batch_key, drop_name, file_type)
                    VALUES (?, ?, ?);
                    """,
                    ("drop_1:batch_001.csv", "drop_1", "lead_csv"),
                )
                raise ValueError("Simulated pipeline crash during ingestion")
        except ValueError:
            pass

        # Verify that the row was rolled back and is not in the database
        with get_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM ingestion_batches WHERE batch_key = ?",
                ("drop_1:batch_001.csv",),
            ).fetchone()
            self.assertIsNone(row, "Transaction must roll back on error")


if __name__ == "__main__":
    unittest.main()
