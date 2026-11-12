"""
Unit and integration tests for lead CSV ingestion, validation, duplicate merging,
rejection audit, and PII protection in solution.lead_ingestion.
"""

import json
from pathlib import Path
import tempfile
import unittest

from solution.db import get_db, init_db
from solution.lead_ingestion import (
    discover_lead_csv_files,
    evaluate_lead_row,
    ingest_lead_csv,
    load_schema,
)
from solution.security import (
    decrypt_string,
    generate_encryption_key,
    generate_hash_key,
    hash_phone,
)


class LeadIngestionTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "test_ingestion.db"
        init_db(self.db_path)

        self.enc_key = generate_encryption_key().encode("utf-8")
        self.hash_key = generate_hash_key().encode("utf-8")
        self.motor_schema = load_schema("motor_renewal")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_csv(self, filename: str, content: str, subdir: str = "motor_renewal") -> Path:
        target_dir = self.temp_path / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / filename
        with open(file_path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return file_path

    # 1. Blank row skipping and row numbering
    def test_blank_rows_skipped_and_row_numbers_accurate(self):
        csv_content = (
            "Customer_Name,Phone,Policy_No,Expiry,Premium,Language\n"
            "Aarav Sharma,9876543210,MOT/2026/001,2026-12-01,15000,en\n"
            ",,,,,\n"  # blank row 1
            "   ,   ,   ,   ,   ,   \n"  # blank row 2
            "Priya Patel,5555555555,MOT/2026/002,2026-12-01,16000,en\n"  # bad phone
        )
        csv_file = self._create_csv("batch_001.csv", csv_content)
        stats = ingest_lead_csv(
            csv_path=csv_file,
            workflow_id="motor_renewal",
            drop_name="drop_1",
            db_path=self.db_path,
            enc_key=self.enc_key,
            hash_key=self.hash_key,
        )

        # 2 nonblank rows evaluated
        self.assertEqual(stats.lead_rows_total, 2)
        # Row 1 is valid, Row 2 has bad phone
        self.assertEqual(stats.leads_created, 1)
        self.assertEqual(stats.lead_rows_rejected, 1)

        # Check rejection row number: nonblank row 2 must be recorded with row_number = 2
        with get_db(self.db_path) as conn:
            rej = conn.execute("SELECT * FROM lead_rejections").fetchone()
            self.assertEqual(rej["row_number"], 2)
            self.assertEqual(rej["reason"], "invalid_phone")
            self.assertEqual(rej["batch_id"], "batch_001")

    # 2. Schema order rejection rules
    def test_rejection_order_prioritizes_first_schema_failure(self):
        # In motor_renewal schema: customer_name is before phone, phone is before policy_number
        csv_content = (
            "Customer_Name,Phone,Policy_No\n"
            ",5555555555,MOT/001\n"  # Missing name AND bad phone -> missing_required:customer_name
            "Rohan Gupta,5555555555,\n"  # Bad phone AND missing policy_number -> invalid_phone
            "Saanvi Rao,,MOT/003\n"  # Missing phone -> missing_required:phone
        )
        csv_file = self._create_csv("batch_order.csv", csv_content)
        stats = ingest_lead_csv(
            csv_path=csv_file,
            workflow_id="motor_renewal",
            drop_name="drop_1",
            db_path=self.db_path,
            enc_key=self.enc_key,
            hash_key=self.hash_key,
        )

        self.assertEqual(stats.lead_rows_rejected, 3)
        self.assertEqual(stats.rejections_by_reason.get("missing_required:customer_name"), 1)
        self.assertEqual(stats.rejections_by_reason.get("invalid_phone"), 1)
        self.assertEqual(stats.rejections_by_reason.get("missing_required:phone"), 1)

    # 3. Optional invalid fields produce warnings and null values on valid rows
    def test_optional_invalid_fields_produce_null_and_warnings(self):
        csv_content = (
            "Customer_Name,Phone,Policy_No,Expiry,Premium,Language\n"
            "Diya Joshi,9876543210,MOT/2026/010,31/02/2026,twelve thousand,french\n"
        )
        csv_file = self._create_csv("batch_warnings.csv", csv_content)
        stats = ingest_lead_csv(
            csv_path=csv_file,
            workflow_id="motor_renewal",
            drop_name="drop_1",
            db_path=self.db_path,
            enc_key=self.enc_key,
            hash_key=self.hash_key,
        )

        self.assertEqual(stats.leads_created, 1)
        self.assertEqual(stats.lead_rows_rejected, 0)
        # 3 optional invalid fields: date, decimal, enum -> 3 warnings
        self.assertEqual(stats.field_warnings, 3)

        # Check that invalid optional fields became null in database, but default language applied
        with get_db(self.db_path) as conn:
            row = conn.execute("SELECT * FROM leads WHERE external_ref = 'MOT/2026/010'").fetchone()
            self.assertEqual(row["preferred_language"], "en", "Schema default 'en' must apply")
            extras = json.loads(row["extra_attributes"]) if row["extra_attributes"] else {}
            self.assertIsNone(extras.get("policy_expiry"))
            self.assertIsNone(extras.get("premium_amount"))

    # 4. Lead creation, PII encryption, and phone masking
    def test_lead_pii_encryption_and_masked_phone(self):
        csv_content = (
            "Insured Name,Mobile No,Policy No,Premium\n"
            "Meera Nair,+91 98765 43210,MOT/POL-999,₹ 25,000.50\n"
        )
        csv_file = self._create_csv("batch_pii.csv", csv_content)
        ingest_lead_csv(
            csv_path=csv_file,
            workflow_id="motor_renewal",
            drop_name="drop_1",
            db_path=self.db_path,
            enc_key=self.enc_key,
            hash_key=self.hash_key,
        )

        with get_db(self.db_path) as conn:
            row = conn.execute("SELECT * FROM leads WHERE external_ref = 'MOT/POL-999'").fetchone()
            # PII must not be plaintext
            self.assertNotEqual(row["name_encrypted"], "Meera Nair")
            self.assertNotEqual(row["phone_encrypted"], "+919876543210")
            # Decryption must recover original values
            self.assertEqual(decrypt_string(row["name_encrypted"], key=self.enc_key), "Meera Nair")
            self.assertEqual(decrypt_string(row["phone_encrypted"], key=self.enc_key), "+919876543210")
            # Masked phone last4
            self.assertEqual(row["phone_last4"], "3210")
            # Hash must match deterministic HMAC
            expected_hash = hash_phone("+919876543210", key=self.hash_key)
            self.assertEqual(row["phone_hash"], expected_hash)

    # 5. Duplicate lead merging (last write wins, preserves batch_id)
    def test_duplicate_lead_merging_last_write_wins_preserves_batch_id(self):
        # Batch 1 introduces the lead
        b1_content = (
            "Customer_Name,Phone,Policy_No,Premium,Language\n"
            "Farhan Khan,9876543210,MOT/001,10000,en\n"
        )
        b1_file = self._create_csv("batch_001.csv", b1_content)
        s1 = ingest_lead_csv(b1_file, "motor_renewal", "drop_1", self.db_path, self.enc_key, self.hash_key)
        self.assertEqual(s1.leads_created, 1)
        self.assertEqual(s1.duplicate_lead_rows_merged, 0)

        # Batch 2 has the same phone number with updated premium and language
        b2_content = (
            "Customer_Name,Phone,Policy_No,Premium,Language\n"
            "Farhan Khan,9876543210,MOT/001-UPDATED,12500,hi\n"
        )
        b2_file = self._create_csv("batch_002.csv", b2_content)
        s2 = ingest_lead_csv(b2_file, "motor_renewal", "drop_1", self.db_path, self.enc_key, self.hash_key)
        self.assertEqual(s2.leads_created, 0)
        self.assertEqual(s2.duplicate_lead_rows_merged, 1)

        # Verify database record
        p_hash = hash_phone("+919876543210", key=self.hash_key)
        with get_db(self.db_path) as conn:
            row = conn.execute("SELECT * FROM leads WHERE phone_hash = ?", (p_hash,)).fetchone()
            # Batch ID must remain batch_001 (permanent per spec)
            self.assertEqual(row["batch_id"], "batch_001")
            # Existing name must NOT be overwritten by empty/null
            self.assertEqual(decrypt_string(row["name_encrypted"], key=self.enc_key), "Farhan Khan")
            # Non-null values must be updated
            self.assertEqual(row["external_ref"], "MOT/001-UPDATED")
            self.assertEqual(row["preferred_language"], "hi")
            extras = json.loads(row["extra_attributes"])
            self.assertEqual(extras["premium_amount"], 12500.0)

    # 6. Idempotency on repeated file ingestion
    def test_idempotent_reingestion_skips_processed_batch(self):
        content = (
            "Customer_Name,Phone,Policy_No\n"
            "Arjun Reddy,9876543210,MOT/001\n"
        )
        csv_file = self._create_csv("batch_idem.csv", content)
        s1 = ingest_lead_csv(csv_file, "motor_renewal", "drop_1", self.db_path, self.enc_key, self.hash_key)
        self.assertEqual(s1.leads_created, 1)

        # Running again on the same drop/batch
        s2 = ingest_lead_csv(csv_file, "motor_renewal", "drop_1", self.db_path, self.enc_key, self.hash_key)
        self.assertEqual(s2.lead_rows_total, 0)
        self.assertEqual(s2.leads_created, 0)
        self.assertEqual(s2.duplicate_lead_rows_merged, 0)

        # Only one lead in database
        with get_db(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            self.assertEqual(count, 1)

    # 7. Invalid duplicate row is rejected and not merged
    def test_invalid_duplicate_row_is_rejected_and_not_merged(self):
        b1_content = "Customer_Name,Phone,Policy_No\nKavya Reddy,9876543210,MOT/100\n"
        b1_file = self._create_csv("b1.csv", b1_content)
        ingest_lead_csv(b1_file, "motor_renewal", "drop_1", self.db_path, self.enc_key, self.hash_key)

        # Second row for same lead has bad phone number
        b2_content = "Customer_Name,Phone,Policy_No\nKavya Reddy,12345,MOT/100\n"
        b2_file = self._create_csv("b2.csv", b2_content)
        s2 = ingest_lead_csv(b2_file, "motor_renewal", "drop_1", self.db_path, self.enc_key, self.hash_key)

        self.assertEqual(s2.lead_rows_rejected, 1)
        self.assertEqual(s2.duplicate_lead_rows_merged, 0)

    # 8. File discovery in filename order
    def test_discover_lead_csv_files_sorted(self):
        self._create_csv("batch_003.csv", "name\n", subdir="motor_renewal")
        self._create_csv("batch_001.csv", "name\n", subdir="motor_renewal")
        self._create_csv("batch_002.csv", "name\n", subdir="motor_renewal")

        discovered = discover_lead_csv_files(self.temp_path)
        filenames = [f[0].name for f in discovered]
        self.assertEqual(filenames, ["batch_001.csv", "batch_002.csv", "batch_003.csv"])


if __name__ == "__main__":
    unittest.main()
