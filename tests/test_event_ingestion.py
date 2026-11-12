"""
Unit tests for Checkpoint 4A: JSONL Event Reading, Parsing, and Validation.
"""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from solution.db import get_db, init_db
from solution.event_ingestion import (
    EventIngestionStats,
    ParsedEvent,
    discover_event_files,
    ingest_event_directory,
    ingest_event_jsonl,
    parse_event_line,
    read_event_jsonl,
)
from solution.security import decrypt_string, generate_encryption_key


class EventIngestionCheckpoint4ATests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "test_ledger.db"
        init_db(self.db_path)
        self.enc_key = generate_encryption_key().encode("utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_jsonl(self, filename: str, lines: list[str]) -> Path:
        file_path = self.temp_path / filename
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return file_path

    def test_parse_valid_event_with_phone(self):
        line = (
            '{"event_id": "evt_001", "event_type": "call.initiated", '
            '"workflow_id": "motor_renewal", "call_id": "call_100", '
            '"occurred_at": "2026-10-01T09:00:00Z", "phone": "+91 98765 43210", '
            '"payload": {"provider": "mock"}}'
        )
        status, event = parse_event_line(line)
        self.assertEqual(status, "valid")
        self.assertIsInstance(event, ParsedEvent)
        self.assertEqual(event.event_id, "evt_001")
        self.assertEqual(event.event_type, "call.initiated")
        self.assertEqual(event.workflow_id, "motor_renewal")
        self.assertEqual(event.call_id, "call_100")
        self.assertEqual(event.occurred_at_raw, "2026-10-01T09:00:00Z")
        self.assertEqual(event.phone_raw, "+91 98765 43210")
        self.assertEqual(event.payload, {"provider": "mock"})

    def test_parse_disposition_override_without_phone(self):
        # Override events intentionally lack a phone field
        line = (
            '{"event_id": "evt_002", "event_type": "disposition.override", '
            '"workflow_id": "motor_renewal", "call_id": "call_100", '
            '"occurred_at": "2026-10-02T10:00:00+05:30", '
            '"payload": {"disposition_code": "renewed", "overridden_by": "sup_1"}}'
        )
        status, event = parse_event_line(line)
        self.assertEqual(status, "valid")
        self.assertIsNone(event.phone_raw)
        self.assertEqual(event.event_type, "disposition.override")
        self.assertEqual(event.payload.get("overridden_by"), "sup_1")

    def test_blank_lines_are_skipped(self):
        status, event = parse_event_line("   \n")
        self.assertEqual(status, "empty")
        self.assertIsNone(event)

    def test_truncated_json_detected_as_malformed(self):
        # Truncated syntax injected by synthetic generator
        truncated = '{"event_id": "evt_003", "event_type": "call.'
        status, event = parse_event_line(truncated)
        self.assertEqual(status, "malformed")
        self.assertIsNone(event)

    def test_non_dictionary_json_detected_as_malformed(self):
        # JSON arrays, numbers, booleans, and null are not valid event records
        for non_dict in ["[1, 2, 3]", "12345", "true", "null", '"just a string"']:
            status, event = parse_event_line(non_dict)
            self.assertEqual(status, "malformed", f"Failed to mark non-dict as malformed: {non_dict}")
            self.assertIsNone(event)

    def test_missing_required_core_fields(self):
        # Each of these core fields must be present and non-empty
        core_fields = ["event_id", "event_type", "workflow_id", "call_id", "occurred_at"]
        base = {
            "event_id": "evt_004",
            "event_type": "call.connected",
            "workflow_id": "motor_renewal",
            "call_id": "call_101",
            "occurred_at": "2026-10-01 09:05:00",
        }

        import json
        for missing in core_fields:
            data = dict(base)
            del data[missing]
            status, event = parse_event_line(json.dumps(data))
            self.assertEqual(status, "malformed", f"Failed when missing: {missing}")

            # Also test when field is empty string
            data[missing] = "   "
            status, event = parse_event_line(json.dumps(data))
            self.assertEqual(status, "malformed", f"Failed when empty: {missing}")

    def test_read_event_jsonl_stream_and_stats(self):
        lines = [
            '{"event_id": "e1", "event_type": "call.initiated", "workflow_id": "w1", "call_id": "c1", "occurred_at": "2026-10-01T09:00:00Z", "phone": "9876543210"}',
            "",  # empty line (not counted)
            '   ',  # whitespace line (not counted)
            '{"event_id": "e2", "event_type": "call.',  # malformed JSON (counted in events_total and malformed_events)
            '{"event_type": "call.connected", "workflow_id": "w1", "call_id": "c1", "occurred_at": "2026-10-01T09:01:00Z"}',  # missing event_id (malformed)
            '{"event_id": "e3", "event_type": "disposition.override", "workflow_id": "w1", "call_id": "c1", "occurred_at": "2026-10-01T10:00:00Z"}',
        ]
        file_path = self._write_jsonl("events_test.jsonl", lines)

        stats = EventIngestionStats()
        events = list(read_event_jsonl(file_path, stats=stats))

        # 4 non-empty lines total (e1, malformed JSON, missing event_id, e3)
        self.assertEqual(stats.events_total, 4)
        # 2 malformed lines
        self.assertEqual(stats.malformed_events, 2)
        # 2 valid events parsed (e1 and e3)
        self.assertEqual(len(events), 2)
        self.assertEqual([e.event_id for e in events], ["e1", "e3"])

    def test_stats_merge(self):
        s1 = EventIngestionStats(events_total=10, malformed_events=2)
        s2 = EventIngestionStats(events_total=5, malformed_events=1)
        s1.merge(s2)
        self.assertEqual(s1.events_total, 15)
        self.assertEqual(s1.malformed_events, 3)

    # ================= Checkpoint 4B Tests =================

    def test_ingest_event_jsonl_stores_records_and_encrypts_phone(self):
        lines = [
            '{"event_id": "evt_101", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T09:00:00Z", "phone": "+91 98765 43210", "payload": {"foo": "bar"}}',
            '{"event_id": "evt_102", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-02T10:00:00+05:30", "payload": {"disposition_code": "renewed"}}',
        ]
        file_path = self._write_jsonl("events_001.jsonl", lines)

        stats = ingest_event_jsonl(
            file_path=file_path,
            drop_name="drop_1",
            db_path=self.db_path,
            enc_key=self.enc_key,
        )

        self.assertEqual(stats.events_total, 2)
        self.assertEqual(stats.events_recorded, 2)
        self.assertEqual(stats.malformed_events, 0)
        self.assertEqual(stats.duplicate_events_dropped, 0)

        with get_db(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM interaction_ledger ORDER BY ledger_id").fetchall()
            self.assertEqual(len(rows), 2)

            r1, r2 = rows[0], rows[1]
            self.assertEqual(r1["event_id"], "evt_101")
            self.assertEqual(r1["event_type"], "call.initiated")
            self.assertEqual(r1["occurred_at_raw"], "2026-10-01T09:00:00Z")
            # Phone must be encrypted ciphertext, never plaintext
            self.assertNotEqual(r1["phone_encrypted"], "+91 98765 43210")
            self.assertEqual(decrypt_string(r1["phone_encrypted"], key=self.enc_key), "+91 98765 43210")
            # Linkage state is pending (NULL, not confirmed orphan)
            self.assertIsNone(r1["is_orphan"])
            self.assertEqual(json.loads(r1["payload"]), {"foo": "bar"})

            # Override event has NULL phone
            self.assertEqual(r2["event_id"], "evt_102")
            self.assertEqual(r2["event_type"], "disposition.override")
            self.assertIsNone(r2["phone_encrypted"])
            self.assertIsNone(r2["is_orphan"])

    def test_duplicate_events_in_same_file_detected(self):
        lines = [
            '{"event_id": "dup_1", "event_type": "call.initiated", "workflow_id": "w1", "call_id": "c1", "occurred_at": "2026-10-01T09:00:00Z"}',
            '{"event_id": "dup_1", "event_type": "call.connected", "workflow_id": "w1", "call_id": "c1", "occurred_at": "2026-10-01T09:00:10Z"}',
            '{"event_id": "unique_1", "event_type": "call.ended", "workflow_id": "w1", "call_id": "c1", "occurred_at": "2026-10-01T09:01:00Z"}',
        ]
        file_path = self._write_jsonl("events_dups.jsonl", lines)

        stats = ingest_event_jsonl(file_path, "drop_1", self.db_path, self.enc_key)

        self.assertEqual(stats.events_total, 3)
        self.assertEqual(stats.events_recorded, 2)
        self.assertEqual(stats.duplicate_events_dropped, 1)

        with get_db(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM interaction_ledger").fetchone()[0]
            self.assertEqual(count, 2)

    def test_duplicate_events_across_files_detected(self):
        file1 = self._write_jsonl("file1.jsonl", [
            '{"event_id": "shared_1", "event_type": "call.initiated", "workflow_id": "w1", "call_id": "c1", "occurred_at": "2026-10-01T09:00:00Z"}',
        ])
        file2 = self._write_jsonl("file2.jsonl", [
            '{"event_id": "shared_1", "event_type": "call.initiated", "workflow_id": "w1", "call_id": "c1", "occurred_at": "2026-10-01T09:00:00Z"}',
            '{"event_id": "fresh_2", "event_type": "call.connected", "workflow_id": "w1", "call_id": "c1", "occurred_at": "2026-10-01T09:00:15Z"}',
        ])

        s1 = ingest_event_jsonl(file1, "drop_1", self.db_path, self.enc_key)
        self.assertEqual(s1.events_recorded, 1)
        self.assertEqual(s1.duplicate_events_dropped, 0)

        s2 = ingest_event_jsonl(file2, "drop_1", self.db_path, self.enc_key)
        self.assertEqual(s2.events_total, 2)
        self.assertEqual(s2.events_recorded, 1)  # only fresh_2 inserted
        self.assertEqual(s2.duplicate_events_dropped, 1)  # shared_1 dropped

    def test_idempotent_reingestion_skips_processed_event_file(self):
        file1 = self._write_jsonl("batch_idem.jsonl", [
            '{"event_id": "idem_1", "event_type": "call.initiated", "workflow_id": "w1", "call_id": "c1", "occurred_at": "2026-10-01T09:00:00Z"}',
        ])

        s1 = ingest_event_jsonl(file1, "drop_1", self.db_path, self.enc_key)
        self.assertEqual(s1.events_recorded, 1)

        # Re-ingesting the exact same batch returns zero stats
        s2 = ingest_event_jsonl(file1, "drop_1", self.db_path, self.enc_key)
        self.assertEqual(s2.events_total, 0)
        self.assertEqual(s2.events_recorded, 0)
        self.assertEqual(s2.duplicate_events_dropped, 0)

    def test_transaction_rollback_on_failure(self):
        file1 = self._write_jsonl("crash.jsonl", [
            '{"event_id": "ok_1", "event_type": "call.initiated", "workflow_id": "w1", "call_id": "c1", "occurred_at": "2026-10-01T09:00:00Z"}',
            '{"event_id": "ok_2", "event_type": "call.connected", "workflow_id": "w1", "call_id": "c1", "occurred_at": "2026-10-01T09:01:00Z"}',
        ])

        # Simulate a crash mid-file during iteration
        def crashing_read(file_path, stats=None):
            yield parse_event_line('{"event_id": "ok_1", "event_type": "call.initiated", "workflow_id": "w1", "call_id": "c1", "occurred_at": "2026-10-01T09:00:00Z"}')[1]
            raise sqlite3.OperationalError("Simulated mid-ingestion disk/stream crash")

        with unittest.mock.patch("solution.event_ingestion.read_event_jsonl", side_effect=crashing_read):
            with self.assertRaises(sqlite3.OperationalError):
                ingest_event_jsonl(file1, "drop_1", self.db_path, self.enc_key)

        # After rollback, zero rows should be in interaction_ledger or ingestion_batches
        with get_db(self.db_path) as conn:
            ledger_count = conn.execute("SELECT COUNT(*) FROM interaction_ledger").fetchone()[0]
            batch_count = conn.execute("SELECT COUNT(*) FROM ingestion_batches WHERE batch_key LIKE '%crash.jsonl'").fetchone()[0]
            self.assertEqual(ledger_count, 0, "All events must be rolled back on error")
            self.assertEqual(batch_count, 0, "Batch tracking must not be marked completed on error")

    def test_append_only_preserves_original_when_override_arrives(self):
        # A call receives disposition.set, then a supervisor overrides it
        lines = [
            '{"event_id": "set_1", "event_type": "disposition.set", "workflow_id": "w1", "call_id": "c99", "occurred_at": "2026-10-01T10:00:00Z", "payload": {"disposition_code": "renewed"}}',
            '{"event_id": "ovr_1", "event_type": "disposition.override", "workflow_id": "w1", "call_id": "c99", "occurred_at": "2026-10-02T10:00:00Z", "payload": {"disposition_code": "payment_link_sent", "overridden_by": "sup_1"}}',
        ]
        file_path = self._write_jsonl("append_only.jsonl", lines)
        ingest_event_jsonl(file_path, "drop_1", self.db_path, self.enc_key)

        with get_db(self.db_path) as conn:
            rows = conn.execute("SELECT event_id, event_type, payload FROM interaction_ledger WHERE call_id = 'c99' ORDER BY ledger_id").fetchall()
            self.assertEqual(len(rows), 2)
            # The original disposition.set is untouched and retained
            self.assertEqual(rows[0]["event_id"], "set_1")
            self.assertEqual(rows[0]["event_type"], "disposition.set")
            self.assertIn("renewed", rows[0]["payload"])
            # The override is appended as a distinct record
            self.assertEqual(rows[1]["event_id"], "ovr_1")
            self.assertEqual(rows[1]["event_type"], "disposition.override")
            self.assertIn("payment_link_sent", rows[1]["payload"])

    def test_narrow_integrity_error_handling(self):
        # Verify that foreign key failures or other integrity errors are NOT swallowed
        with get_db(self.db_path) as conn:
            # Trying to insert an invalid foreign key lead_id when PRAGMA foreign_keys is ON
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO interaction_ledger (
                        event_id, event_type, workflow_id, call_id,
                        occurred_at_raw, lead_id
                    ) VALUES ('test_fk', 'call.initiated', 'w1', 'c1', '2026-10-01', 999999)
                    """
                )

    def test_legacy_schema_migration_preserves_data_and_populates_occurred_at_raw(self):
        # 1. Create a legacy database file using the old Part 1 schema SQL
        legacy_db = self.temp_path / "legacy.db"
        legacy_schema_sql = """
        CREATE TABLE interaction_ledger (
            ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            workflow_id TEXT NOT NULL,
            call_id TEXT NOT NULL,
            occurred_at_utc TEXT NOT NULL,
            occurred_at_ist TEXT NOT NULL,
            lead_id INTEGER,
            phone_hash TEXT,
            payload TEXT,
            is_orphan INTEGER DEFAULT 0,
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
        conn = sqlite3.connect(str(legacy_db))
        conn.executescript(legacy_schema_sql)
        # Insert a legacy record into the old table
        conn.execute(
            """
            INSERT INTO interaction_ledger (
                event_id, event_type, workflow_id, call_id,
                occurred_at_utc, occurred_at_ist, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("legacy_evt_1", "call.initiated", "motor_renewal", "c_leg", "2026-10-01T09:00:00Z", "2026-10-01T14:30:00+05:30", '{"old": true}')
        )
        conn.commit()
        conn.close()

        # 2. Run init_db on the legacy database (which triggers migrate_db)
        init_db(legacy_db)

        # 3. Verify that migration succeeded, occurred_at_raw was populated from occurred_at_utc,
        # and ledger_id / event_id were preserved
        with get_db(legacy_db) as conn:
            row = conn.execute("SELECT * FROM interaction_ledger WHERE event_id = 'legacy_evt_1'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["ledger_id"], 1)
            self.assertEqual(row["occurred_at_raw"], "2026-10-01T09:00:00Z")
            self.assertEqual(row["occurred_at_utc"], "2026-10-01T09:00:00Z")
            self.assertEqual(row["occurred_at_ist"], "2026-10-01T14:30:00+05:30")
            self.assertIsNone(row["phone_encrypted"])

            # 4. Verify we can insert new events into the migrated table without constraint errors
            new_file = self._write_jsonl("migrated_new.jsonl", [
                '{"event_id": "new_evt_2", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c_leg", "occurred_at": "2026-10-01 10:00:00", "phone": "9876543210"}',
            ])
            s = ingest_event_jsonl(new_file, "drop_1", str(legacy_db), self.enc_key)
            self.assertEqual(s.events_recorded, 1)

            new_row = conn.execute("SELECT * FROM interaction_ledger WHERE event_id = 'new_evt_2'").fetchone()
            self.assertIsNotNone(new_row)
            self.assertEqual(new_row["occurred_at_raw"], "2026-10-01 10:00:00")
            self.assertIsNotNone(new_row["phone_encrypted"])

    def test_discover_event_files_sorted(self):
        events_dir = self.temp_path / "events"
        events_dir.mkdir()
        (events_dir / "events_0003.jsonl").write_text("", encoding="utf-8")
        (events_dir / "events_0001.jsonl").write_text("", encoding="utf-8")
        (events_dir / "events_0002.jsonl").write_text("", encoding="utf-8")

        discovered = discover_event_files(self.temp_path)
        self.assertEqual([p.name for p in discovered], ["events_0001.jsonl", "events_0002.jsonl", "events_0003.jsonl"])


if __name__ == "__main__":
    unittest.main()

