"""
Integration tests for Stage 7: CLI and End-to-End Pipeline Execution.
"""

import json
from pathlib import Path
import tempfile
import unittest

from solution.cli import main
from solution.db import get_db


class CliIntegrationStage7Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "cli_test.db"
        self.reports_dir = self.temp_path / "reports"

        # Prepare synthetic drop structures
        # Drop 1: Lead 1 uploaded, Event 1 arrives (linked), Event 2 arrives (orphan for now)
        self.drop_1 = self.temp_path / "data" / "drop_1"
        leads_d1 = self.drop_1 / "leads" / "motor_renewal"
        leads_d1.mkdir(parents=True, exist_ok=True)
        events_d1 = self.drop_1 / "events"
        events_d1.mkdir(parents=True, exist_ok=True)

        lead1_csv = (
            "Policy_Number,Customer_Name,Phone,Expiry_Date,Premium_Amount\n"
            "POL-101,Aarav Sharma,9876543210,2026-12-01,15000\n"
        )
        (leads_d1 / "batch_001.csv").write_text(lead1_csv, encoding="utf-8")

        events1_jsonl = (
            '{"event_id": "e1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T09:00:00Z", "phone": "9876543210"}\n'
            '{"event_id": "e2", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T09:00:15Z", "phone": "9876543210"}\n'
            '{"event_id": "e3", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01T09:02:00Z", "phone": "9876543210", "payload": {"disposition_code": "renewed"}}\n'
            # Event for Phone 9123456789 (which arrives later in Drop 2!)
            '{"event_id": "e_delayed", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c2", "occurred_at": "2026-10-01T10:00:00Z", "phone": "9123456789"}\n'
        )
        (events_d1 / "events_0001.jsonl").write_text(events1_jsonl, encoding="utf-8")

        # Drop 2: Lead 2 uploaded (resolves e_delayed), and an override for Call 1 arrives
        self.drop_2 = self.temp_path / "data" / "drop_2"
        leads_d2 = self.drop_2 / "leads" / "motor_renewal"
        leads_d2.mkdir(parents=True, exist_ok=True)
        events_d2 = self.drop_2 / "events"
        events_d2.mkdir(parents=True, exist_ok=True)

        lead2_csv = (
            "Policy_Number,Customer_Name,Phone,Expiry_Date,Premium_Amount\n"
            "POL-102,Diya Patel,9123456789,2026-12-05,20000\n"
        )
        (leads_d2 / "batch_002.csv").write_text(lead2_csv, encoding="utf-8")

        events2_jsonl = (
            '{"event_id": "e_ovr", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-02T10:00:00Z", "payload": {"disposition_code": "payment_link_sent", "overridden_by": "supervisor_1"}}\n'
        )
        (events_d2 / "events_0002.jsonl").write_text(events2_jsonl, encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cli_init_db(self):
        code = main(["--db", str(self.db_path), "init-db"])
        self.assertEqual(code, 0)
        self.assertTrue(self.db_path.is_file())

    def test_cli_incremental_pipeline_and_reports(self):
        # 1. Ingest Drop 1
        ret = main(["--db", str(self.db_path), "ingest", str(self.drop_1)])
        self.assertEqual(ret, 0)

        with get_db(self.db_path) as conn:
            leads_cnt = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            self.assertEqual(leads_cnt, 1)

            # e_delayed should initially be an orphan after drop 1
            e_del = conn.execute("SELECT * FROM interaction_ledger WHERE event_id = 'e_delayed'").fetchone()
            self.assertEqual(e_del["is_orphan"], 1)

        # 2. Re-ingesting Drop 1 is completely idempotent
        ret_idem = main(["--db", str(self.db_path), "ingest", str(self.drop_1)])
        self.assertEqual(ret_idem, 0)

        with get_db(self.db_path) as conn:
            leads_cnt2 = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            self.assertEqual(leads_cnt2, 1)
            batches_cnt = conn.execute("SELECT COUNT(*) FROM ingestion_batches").fetchone()[0]
            self.assertEqual(batches_cnt, 2)  # 1 lead batch + 1 event batch

        # 3. Ingest Drop 2 (Lead 2 arrives, resolving e_delayed, and override arrives)
        ret2 = main(["--db", str(self.db_path), "ingest", str(self.drop_2)])
        self.assertEqual(ret2, 0)

        with get_db(self.db_path) as conn:
            leads_cnt_total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            self.assertEqual(leads_cnt_total, 2)

            # e_delayed must now be linked and no longer an orphan!
            e_del_resolved = conn.execute("SELECT * FROM interaction_ledger WHERE event_id = 'e_delayed'").fetchone()
            self.assertIsNotNone(e_del_resolved["lead_id"])
            self.assertEqual(e_del_resolved["is_orphan"], 0)

            # e_ovr must also be linked to Call 1's lead
            e_ovr = conn.execute("SELECT * FROM interaction_ledger WHERE event_id = 'e_ovr'").fetchone()
            self.assertIsNotNone(e_ovr["lead_id"])
            self.assertEqual(e_ovr["is_orphan"], 0)

        # 4. Generate Reports
        ret_rep = main(["--db", str(self.db_path), "report", "--out", str(self.reports_dir)])
        self.assertEqual(ret_rep, 0)

        # Verify all 5 reports are produced
        for r_name in ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv", "data_quality.json"]:
            p = self.reports_dir / r_name
            self.assertTrue(p.is_file(), f"Expected report {r_name} missing")

        # In data_quality.json, orphan_events should now be 0 since e_delayed was resolved!
        with open(self.reports_dir / "data_quality.json", "r", encoding="utf-8") as f:
            dq = json.load(f)
            self.assertEqual(dq["orphan_events"], 0)
            self.assertEqual(dq["lead_rows_total"], 2)
            self.assertEqual(dq["events_total"], 5)

    def test_cli_run_command_multi_drop(self):
        # Test the all-in-one 'run' command
        out_dir = self.temp_path / "run_reports"
        ret = main([
            "--db", str(self.db_path),
            "run",
            str(self.drop_1),
            str(self.drop_2),
            "--out", str(out_dir),
        ])
        self.assertEqual(ret, 0)

        self.assertTrue((out_dir / "lead_report.csv").is_file())
        self.assertTrue((out_dir / "data_quality.json").is_file())


if __name__ == "__main__":
    unittest.main()
