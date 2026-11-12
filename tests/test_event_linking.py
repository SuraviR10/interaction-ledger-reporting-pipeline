"""
Tests for Stage 4C: Event-to-Lead Linking, Orphan Handling, Timestamps, and Dispositions.
"""

from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from solution.db import get_db, init_db
from solution.event_ingestion import (
    classify_unlinked_as_orphans,
    ingest_event_jsonl,
    retry_orphan_linking,
)
from solution.lead_ingestion import ingest_lead_csv
from solution.normalizers import (
    load_disposition_catalogue,
    parse_event_timestamp,
)
from solution.security import (
    generate_encryption_key,
    generate_hash_key,
    hash_phone,
)


class EventProcessingStage4CTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "stage4c_test.db"
        init_db(self.db_path)
        self.enc_key = generate_encryption_key().encode("utf-8")
        self.hash_key = generate_hash_key().encode("utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_file(self, filename: str, content: str) -> Path:
        path = self.temp_path / filename
        path.write_text(content, encoding="utf-8")
        return path

    def _create_sample_lead(self, workflow_id="motor_renewal", phone="9876543210", ref="POL-100", batch_id="batch_001"):
        csv_content = (
            "Policy_Number,Customer_Name,Phone,Expiry_Date,Premium_Amount\n"
            f"{ref},Aditya Sharma,{phone},2026-12-01,15000\n"
        )
        csv_file = self._write_file(f"{batch_id}.csv", csv_content)
        ingest_lead_csv(
            csv_path=csv_file,
            workflow_id=workflow_id,
            drop_name="drop_1",
            db_path=self.db_path,
            enc_key=self.enc_key,
            hash_key=self.hash_key,
        )

    # ---------------- 4C1: Event to Lead Linking ----------------

    def test_event_links_to_existing_lead_by_workflow_and_phone(self):
        self._create_sample_lead(workflow_id="motor_renewal", phone="9876543210", ref="POL-101")

        event_line = json.dumps({
            "event_id": "evt_link_1",
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "c1",
            "occurred_at": "2026-10-01T09:00:00Z",
            "phone": "+91 98765 43210",
        })
        event_file = self._write_file("events_link.jsonl", event_line + "\n")

        stats = ingest_event_jsonl(event_file, "drop_1", self.db_path, self.enc_key, self.hash_key)
        self.assertEqual(stats.events_recorded, 1)

        with get_db(self.db_path) as conn:
            row = conn.execute("SELECT * FROM interaction_ledger WHERE event_id = 'evt_link_1'").fetchone()
            self.assertIsNotNone(row)
            self.assertIsNotNone(row["lead_id"])
            self.assertEqual(row["is_orphan"], 0)
            self.assertIsNotNone(row["phone_hash"])
            # UTC and IST normalized timestamps must be populated
            self.assertEqual(row["occurred_at_utc"], "2026-10-01T09:00:00Z")
            self.assertEqual(row["occurred_at_ist"], "2026-10-01T14:30:00+05:30")

    def test_same_phone_different_workflow_does_not_cross_link(self):
        # Lead exists in policy_feedback only
        self._create_sample_lead(workflow_id="policy_feedback", phone="9876543210", ref="FB-101")

        # Event arrives for motor_renewal with the same phone
        event_line = json.dumps({
            "event_id": "evt_cross_wf",
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "c_cross",
            "occurred_at": "2026-10-01T09:00:00Z",
            "phone": "9876543210",
        })
        event_file = self._write_file("cross_wf.jsonl", event_line + "\n")
        ingest_event_jsonl(event_file, "drop_1", self.db_path, self.enc_key, self.hash_key)

        with get_db(self.db_path) as conn:
            row = conn.execute("SELECT * FROM interaction_ledger WHERE event_id = 'evt_cross_wf'").fetchone()
            # Must NOT link to the policy_feedback lead!
            self.assertIsNone(row["lead_id"])

    def test_invalid_phone_event_is_orphan(self):
        event_line = json.dumps({
            "event_id": "evt_bad_phone",
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "c_bad",
            "occurred_at": "2026-10-01T09:00:00Z",
            "phone": "12345",  # invalid Indian phone
        })
        event_file = self._write_file("bad_phone.jsonl", event_line + "\n")
        ingest_event_jsonl(event_file, "drop_1", self.db_path, self.enc_key, self.hash_key)

        with get_db(self.db_path) as conn:
            row = conn.execute("SELECT * FROM interaction_ledger WHERE event_id = 'evt_bad_phone'").fetchone()
            self.assertIsNone(row["lead_id"])
            self.assertEqual(row["is_orphan"], 1)

    # ---------------- 4C2: Pending Events, Orphans & Retries ----------------

    def test_events_arriving_before_lead_resolved_when_lead_arrives(self):
        # 1. Event arrives in Drop 1 before lead is uploaded
        event_line = json.dumps({
            "event_id": "evt_early",
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "c_early",
            "occurred_at": "2026-10-01T09:00:00Z",
            "phone": "9123456789",
        })
        event_file = self._write_file("early_event.jsonl", event_line + "\n")
        ingest_event_jsonl(event_file, "drop_1", self.db_path, self.enc_key, self.hash_key)

        with get_db(self.db_path) as conn:
            row = conn.execute("SELECT * FROM interaction_ledger WHERE event_id = 'evt_early'").fetchone()
            self.assertIsNone(row["lead_id"])

        # 2. Lead arrives later in Drop 2
        self._create_sample_lead(workflow_id="motor_renewal", phone="9123456789", ref="POL-EARLY", batch_id="batch_002")

        # 3. Verify event has now been automatically linked
        with get_db(self.db_path) as conn:
            row = conn.execute("SELECT * FROM interaction_ledger WHERE event_id = 'evt_early'").fetchone()
            self.assertIsNotNone(row["lead_id"], "Event must be linked after lead arrives")
            self.assertEqual(row["is_orphan"], 0)

    def test_disposition_override_linked_through_call_id(self):
        self._create_sample_lead(workflow_id="motor_renewal", phone="9988776655", ref="POL-CALL")

        lines = [
            json.dumps({
                "event_id": "evt_call_init",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "call_ovr_1",
                "occurred_at": "2026-10-01T10:00:00Z",
                "phone": "9988776655",
            }),
            json.dumps({
                "event_id": "evt_call_conn",
                "event_type": "call.connected",
                "workflow_id": "motor_renewal",
                "call_id": "call_ovr_1",
                "occurred_at": "2026-10-01T10:00:15Z",
                "phone": "9988776655",
            }),
            json.dumps({
                "event_id": "evt_call_set",
                "event_type": "disposition.set",
                "workflow_id": "motor_renewal",
                "call_id": "call_ovr_1",
                "occurred_at": "2026-10-01T10:02:00Z",
                "phone": "9988776655",
                "payload": {"disposition_code": "payment_link_sent"},
            }),
            json.dumps({
                "event_id": "evt_call_ovr",
                "event_type": "disposition.override",
                "workflow_id": "motor_renewal",
                "call_id": "call_ovr_1",
                "occurred_at": "2026-10-02T10:00:00Z",
                # Note: No phone field in override event!
                "payload": {"disposition_code": "renewed", "overridden_by": "sup_1"},
            }),
        ]
        event_file = self._write_file("call_stream.jsonl", "\n".join(lines) + "\n")
        ingest_event_jsonl(event_file, "drop_1", self.db_path, self.enc_key, self.hash_key)

        with get_db(self.db_path) as conn:
            rows = conn.execute(
                "SELECT event_id, event_type, lead_id, is_orphan FROM interaction_ledger WHERE call_id = 'call_ovr_1' ORDER BY ledger_id"
            ).fetchall()
            self.assertEqual(len(rows), 4)
            lead_id_target = rows[0]["lead_id"]
            self.assertIsNotNone(lead_id_target)

            # All 4 events including the override must be linked to the same lead_id
            for r in rows:
                self.assertEqual(r["lead_id"], lead_id_target)
                self.assertEqual(r["is_orphan"], 0)

    # ---------------- 4C3: Timestamps & Dispositions ----------------

    def test_timestamp_parsing_variants(self):
        # 1. UTC ending in Z
        utc, ist, ok = parse_event_timestamp("2026-10-05T13:20:21Z")
        self.assertTrue(ok)
        self.assertEqual(utc, "2026-10-05T13:20:21Z")
        self.assertEqual(ist, "2026-10-05T18:50:21+05:30")

        # 2. Offset (+05:30)
        utc, ist, ok = parse_event_timestamp("2026-10-02T10:00:00+05:30")
        self.assertTrue(ok)
        self.assertEqual(utc, "2026-10-02T04:30:00Z")
        self.assertEqual(ist, "2026-10-02T10:00:00+05:30")

        # 3. Naive timestamp (Spec: Naive means IST)
        utc, ist, ok = parse_event_timestamp("2026-10-03 10:01:00")
        self.assertTrue(ok)
        self.assertEqual(ist, "2026-10-03T10:01:00+05:30")
        self.assertEqual(utc, "2026-10-03T04:31:00Z")

        # 4. Invalid timestamp
        utc, ist, ok = parse_event_timestamp("invalid-date-string")
        self.assertFalse(ok)
        self.assertIsNone(utc)
        self.assertIsNone(ist)

    def test_unknown_disposition_counted_and_tracked(self):
        lines = [
            json.dumps({
                "event_id": "evt_disp_unknown",
                "event_type": "disposition.set",
                "workflow_id": "motor_renewal",
                "call_id": "c_unk",
                "occurred_at": "2026-10-01T10:00:00Z",
                "phone": "9876543210",
                "payload": {"disposition_code": "RENEWD"},  # Invalid / typo code
            }),
            json.dumps({
                "event_id": "evt_disp_valid",
                "event_type": "disposition.set",
                "workflow_id": "motor_renewal",
                "call_id": "c_unk",
                "occurred_at": "2026-10-01T10:05:00Z",
                "phone": "9876543210",
                "payload": {"disposition_code": "renewed"},  # Valid catalogue code
            }),
        ]
        file_path = self._write_file("unknown_disp.jsonl", "\n".join(lines) + "\n")
        stats = ingest_event_jsonl(file_path, "drop_1", self.db_path, self.enc_key, self.hash_key)

        self.assertEqual(stats.unknown_disposition_events, 1)


if __name__ == "__main__":
    unittest.main()
