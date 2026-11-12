"""
Unit tests for Stage 6: Report Generation (All Five Reports).
"""

import csv
import json
from pathlib import Path
import tempfile
import unittest

from solution.db import get_db, init_db
from solution.event_ingestion import ingest_event_jsonl
from solution.lead_ingestion import ingest_lead_csv
from solution.reports import generate_all_reports
from solution.security import generate_encryption_key, generate_hash_key


class ReportsGenerationStage6Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "stage6_test.db"
        init_db(self.db_path)
        self.reports_dir = self.temp_path / "reports"
        self.enc_key = generate_encryption_key().encode("utf-8")
        self.hash_key = generate_hash_key().encode("utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_file(self, filename: str, content: str) -> Path:
        p = self.temp_path / filename
        p.write_text(content, encoding="utf-8")
        return p

    def test_empty_database_generates_all_reports_with_correct_headers(self):
        with get_db(self.db_path) as conn:
            paths = generate_all_reports(conn, self.reports_dir)

        # All 5 files must exist
        for name, p in paths.items():
            self.assertTrue(p.is_file(), f"Report file {name} was not generated")

        # 1. batch_summary.csv header
        with open(paths["batch_summary"], "r", encoding="utf-8") as f:
            header = f.readline().strip().split(",")
            self.assertEqual(
                header,
                ["workflow_id", "batch_id", "total_leads", "yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"]
            )
            self.assertEqual(f.read(), "")  # No data rows

        # 2. daily_overview.csv header
        with open(paths["daily_overview"], "r", encoding="utf-8") as f:
            header = f.readline().strip().split(",")
            self.assertEqual(header, ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"])
            self.assertEqual(f.read(), "")

        # 3. lead_report.csv header
        with open(paths["lead_report"], "r", encoding="utf-8") as f:
            header = f.readline().strip().split(",")
            self.assertEqual(
                header,
                ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls", "connected_calls", "last_call_at", "current_disposition", "ever_success"]
            )
            self.assertEqual(f.read(), "")

        # 4. rejections.csv header
        with open(paths["rejections"], "r", encoding="utf-8") as f:
            header = f.readline().strip().split(",")
            self.assertEqual(header, ["workflow_id", "batch_id", "row_number", "reason"])
            self.assertEqual(f.read(), "")

        # 5. data_quality.json
        with open(paths["data_quality"], "r", encoding="utf-8") as f:
            dq = json.load(f)
            self.assertEqual(dq["lead_rows_total"], 0)
            self.assertEqual(dq["events_total"], 0)
            self.assertEqual(dq["orphan_events"], 0)

    def test_populated_database_report_contents_and_formatting(self):
        # 1. Ingest lead CSV with 1 valid row and 1 invalid phone row
        lead_csv = (
            "Policy_Number,Customer_Name,Phone,Expiry_Date,Premium_Amount\n"
            "POL-001,Priya Sharma,9876543210,2026-12-01,15000\n"
            "POL-002,Rahul Verma,12345,2026-12-01,12000\n"  # Invalid phone
        )
        csv_path = self._write_file("batch_001.csv", lead_csv)
        ingest_lead_csv(csv_path, "motor_renewal", "drop_1", db_path=self.db_path, enc_key=self.enc_key, hash_key=self.hash_key)

        # 2. Ingest event stream: Call 1 (connected, renewed), Call 2 (orphan event)
        events_jsonl = (
            '{"event_id": "evt_1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_1", "occurred_at": "2026-10-01T09:00:00Z", "phone": "9876543210"}\n'
            '{"event_id": "evt_2", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "call_1", "occurred_at": "2026-10-01T09:00:15Z", "phone": "9876543210"}\n'
            '{"event_id": "evt_3", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "call_1", "occurred_at": "2026-10-01T09:02:00Z", "phone": "9876543210", "payload": {"disposition_code": "renewed"}}\n'
            '{"event_id": "evt_orphan", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_orph", "occurred_at": "2026-10-01T10:00:00Z", "phone": "9999999999"}\n'
        )
        jsonl_path = self._write_file("events_001.jsonl", events_jsonl)
        ingest_event_jsonl(jsonl_path, "drop_1", self.db_path, self.enc_key, self.hash_key)

        # 3. Generate all reports
        with get_db(self.db_path) as conn:
            paths = generate_all_reports(conn, self.reports_dir)

        # Verify batch_summary.csv
        with open(paths["batch_summary"], "r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            self.assertEqual(len(reader), 1)
            row = reader[0]
            self.assertEqual(row["workflow_id"], "motor_renewal")
            self.assertEqual(row["batch_id"], "batch_001")
            self.assertEqual(row["total_leads"], "1")
            self.assertEqual(row["completed"], "1")

        # Verify daily_overview.csv
        with open(paths["daily_overview"], "r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            self.assertEqual(len(reader), 1)
            row = reader[0]
            self.assertEqual(row["workflow_id"], "motor_renewal")
            self.assertEqual(row["date"], "2026-10-01")
            self.assertEqual(row["total_calls"], "1")
            self.assertEqual(row["connected_calls"], "1")
            self.assertEqual(row["connectivity_rate"], "1.0000")
            self.assertEqual(row["success_leads"], "1")

        # Verify lead_report.csv
        with open(paths["lead_report"], "r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            self.assertEqual(len(reader), 1)
            row = reader[0]
            self.assertEqual(row["workflow_id"], "motor_renewal")
            self.assertEqual(row["external_ref"], "POL-001")
            self.assertEqual(row["phone_masked"], "+91******3210")
            self.assertEqual(row["bucket"], "completed")
            self.assertEqual(row["total_calls"], "1")
            self.assertEqual(row["connected_calls"], "1")
            self.assertEqual(row["last_call_at"], "2026-10-01T14:30:00+05:30")
            self.assertEqual(row["current_disposition"], "renewed")
            self.assertEqual(row["ever_success"], "true")

        # Verify rejections.csv
        with open(paths["rejections"], "r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            self.assertEqual(len(reader), 1)
            row = reader[0]
            self.assertEqual(row["workflow_id"], "motor_renewal")
            self.assertEqual(row["batch_id"], "batch_001")
            self.assertEqual(row["row_number"], "2")
            self.assertEqual(row["reason"], "invalid_phone")

        # Verify data_quality.json
        with open(paths["data_quality"], "r", encoding="utf-8") as f:
            dq = json.load(f)
            self.assertEqual(dq["lead_rows_total"], 2)
            self.assertEqual(dq["lead_rows_rejected"], 1)
            self.assertEqual(dq["rejections_by_reason"], {"invalid_phone": 1})
            self.assertEqual(dq["events_total"], 4)
            self.assertEqual(dq["orphan_events"], 1)


if __name__ == "__main__":
    unittest.main()
