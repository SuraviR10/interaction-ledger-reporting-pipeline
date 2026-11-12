"""
Unit tests for Stage 5: Call Outcomes, Effective Disposition, and Lead Buckets.
"""

import json
from pathlib import Path
import tempfile
import unittest

from solution.db import get_db, init_db
from solution.event_ingestion import ingest_event_jsonl
from solution.lead_ingestion import ingest_lead_csv
from solution.outcomes import (
    CallOutcome,
    calculate_effective_disposition,
    compute_call_outcomes,
    compute_lead_outcomes,
    determine_lead_bucket,
)
from solution.security import generate_encryption_key, generate_hash_key


class CallOutcomesAndBucketsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "stage5_test.db"
        init_db(self.db_path)
        self.enc_key = generate_encryption_key().encode("utf-8")
        self.hash_key = generate_hash_key().encode("utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_file(self, filename: str, content: str) -> Path:
        p = self.temp_path / filename
        p.write_text(content, encoding="utf-8")
        return p

    def _create_lead(self, phone="9876543210", ref="POL-1", batch="batch_001"):
        csv_content = f"Policy_Number,Customer_Name,Phone,Expiry_Date,Premium_Amount\n{ref},Test User,{phone},2026-12-01,1000\n"
        csv_path = self._write_file(f"{batch}.csv", csv_content)
        ingest_lead_csv(csv_path, "motor_renewal", "drop_1", db_path=self.db_path, enc_key=self.enc_key, hash_key=self.hash_key)

    # ---------------- Pure Bucket Logic Unit Tests ----------------

    def test_bucket_priority_1_yet_to_reach_out(self):
        self.assertEqual(determine_lead_bucket([]), "yet_to_reach_out")

    def test_bucket_priority_2_completed_terminal_disposition(self):
        # Even if one call failed and one connected, if one call is terminal, bucket is completed
        calls = [
            CallOutcome("motor_renewal", "c1", 1, "t1", "t1", "d1", False, True, None, False, False),
            CallOutcome("motor_renewal", "c2", 1, "t2", "t2", "d2", True, False, "renewed", True, True),
        ]
        self.assertEqual(determine_lead_bucket(calls), "completed")

    def test_bucket_priority_3_in_progress_telephony_failed(self):
        # All calls failed
        calls = [
            CallOutcome("motor_renewal", "c1", 1, "t1", "t1", "d1", False, True, None, False, False),
            CallOutcome("motor_renewal", "c2", 1, "t2", "t2", "d2", False, True, None, False, False),
        ]
        self.assertEqual(determine_lead_bucket(calls), "in_progress_telephony_failed")

    def test_bucket_priority_4_in_progress_dialed(self):
        # One call failed, one call connected with non-terminal disposition
        calls = [
            CallOutcome("motor_renewal", "c1", 1, "t1", "t1", "d1", False, True, None, False, False),
            CallOutcome("motor_renewal", "c2", 1, "t2", "t2", "d2", True, False, "callback_requested", False, False),
        ]
        self.assertEqual(determine_lead_bucket(calls), "in_progress_dialed")

    # ---------------- Effective Disposition Hierarchy Tests ----------------

    def test_effective_disposition_override_takes_precedence_over_set(self):
        cat = {
            ("motor_renewal", "renewed"): (True, True),
            ("motor_renewal", "payment_link_sent"): (False, False),
        }
        events = [
            {"event_type": "disposition.set", "payload": {"disposition_code": "payment_link_sent"}, "occurred_at_utc": "2026-10-01T10:00:00Z", "ledger_id": 1},
            {"event_type": "disposition.override", "payload": {"disposition_code": "renewed"}, "occurred_at_utc": "2026-10-01T10:30:00Z", "ledger_id": 2},
        ]
        code, terminal, success = calculate_effective_disposition(events, "motor_renewal", cat)
        self.assertEqual(code, "renewed")
        self.assertTrue(terminal)
        self.assertTrue(success)

    def test_effective_disposition_latest_override_wins(self):
        cat = {
            ("motor_renewal", "renewed"): (True, True),
            ("motor_renewal", "not_interested"): (True, False),
        }
        events = [
            {"event_type": "disposition.override", "payload": {"disposition_code": "not_interested"}, "occurred_at_utc": "2026-10-01T10:00:00Z", "ledger_id": 1},
            {"event_type": "disposition.override", "payload": {"disposition_code": "renewed"}, "occurred_at_utc": "2026-10-01T11:00:00Z", "ledger_id": 2},
        ]
        code, terminal, success = calculate_effective_disposition(events, "motor_renewal", cat)
        self.assertEqual(code, "renewed")
        self.assertTrue(success)

    def test_effective_disposition_tie_breaking_deterministic(self):
        cat = {
            ("motor_renewal", "renewed"): (True, True),
            ("motor_renewal", "not_interested"): (True, False),
        }
        # Equal timestamps: ledger_id DESC breaks the tie deterministically
        events = [
            {"event_type": "disposition.override", "payload": {"disposition_code": "not_interested"}, "occurred_at_utc": "2026-10-01T10:00:00Z", "ledger_id": 10},
            {"event_type": "disposition.override", "payload": {"disposition_code": "renewed"}, "occurred_at_utc": "2026-10-01T10:00:00Z", "ledger_id": 20},
        ]
        code, _, _ = calculate_effective_disposition(events, "motor_renewal", cat)
        self.assertEqual(code, "renewed")

    def test_unknown_disposition_ignored_in_effective_calculation(self):
        cat = {
            ("motor_renewal", "renewed"): (True, True),
        }
        events = [
            {"event_type": "disposition.set", "payload": {"disposition_code": "renewed"}, "occurred_at_utc": "2026-10-01T10:00:00Z", "ledger_id": 1},
            {"event_type": "disposition.override", "payload": {"disposition_code": "UNKNOWN_CODE"}, "occurred_at_utc": "2026-10-01T11:00:00Z", "ledger_id": 2},
        ]
        code, terminal, success = calculate_effective_disposition(events, "motor_renewal", cat)
        # UNKNOWN_CODE must be ignored; falls back to valid earlier disposition
        self.assertEqual(code, "renewed")
        self.assertTrue(terminal)

    # ---------------- End-to-End Database Outcome Tests ----------------

    def test_lead_outcomes_from_db(self):
        self._create_lead(phone="9876543210", ref="POL-ALPHA")

        events = [
            # Call 1: connected, callback_requested
            '{"event_id": "e1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T10:00:00Z", "phone": "9876543210"}',
            '{"event_id": "e2", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T10:00:10Z", "phone": "9876543210"}',
            '{"event_id": "e3", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T10:01:00Z", "phone": "9876543210", "payload": {"disposition_code": "callback_requested"}}',
            # Call 2: connected, renewed (terminal & success)
            '{"event_id": "e4", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c2", "occurred_at": "2026-10-02T12:00:00Z", "phone": "9876543210"}',
            '{"event_id": "e5", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c2", "occurred_at": "2026-10-02T12:00:10Z", "phone": "9876543210"}',
            '{"event_id": "e6", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "c2", "occurred_at": "2026-10-02T12:02:00Z", "phone": "9876543210", "payload": {"disposition_code": "renewed"}}',
        ]
        ev_file = self._write_file("events.jsonl", "\n".join(events) + "\n")
        ingest_event_jsonl(ev_file, "drop_1", self.db_path, self.enc_key, self.hash_key)

        with get_db(self.db_path) as conn:
            lead_outcomes = compute_lead_outcomes(conn)
            self.assertEqual(len(lead_outcomes), 1)
            lo = lead_outcomes[0]

            self.assertEqual(lo.workflow_id, "motor_renewal")
            self.assertEqual(lo.external_ref, "POL-ALPHA")
            self.assertEqual(lo.phone_masked, "+91******3210")
            self.assertEqual(lo.bucket, "completed")
            self.assertEqual(lo.total_calls, 2)
            self.assertEqual(lo.connected_calls, 2)
            # Latest call initiated timestamp in IST: 2026-10-02T12:00:00Z -> 2026-10-02T17:30:00+05:30
            self.assertEqual(lo.last_call_at, "2026-10-02T17:30:00+05:30")
            self.assertEqual(lo.current_disposition, "renewed")
            self.assertTrue(lo.ever_success)


if __name__ == "__main__":
    unittest.main()
