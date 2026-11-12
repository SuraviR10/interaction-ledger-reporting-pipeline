"""
Unit tests for data normalisation functions in solution.normalizers.
Covers:
- Header normalisation and schema alias matching
- Phone number normalisation for all standard and defective cases
- Multi-format date parsing (day-first, leap year/impossible date rejection)
- Currency and decimal parsing (₹, Rs., Rs, INR, commas, negative rejection)
- Enum case-insensitive validation
"""

import unittest

from solution.normalizers import (
    match_header_to_field,
    normalize_date,
    normalize_decimal,
    normalize_enum,
    normalize_header_name,
    normalize_phone,
)


class NormalizersTestCase(unittest.TestCase):
    # 1. Header Normalisation & Alias Matching Tests
    def test_normalize_header_name(self):
        self.assertEqual(normalize_header_name("Customer_Name"), "customer name")
        self.assertEqual(normalize_header_name(" CUSTOMER   NAME "), "customer name")
        self.assertEqual(normalize_header_name("mobile_no"), "mobile no")
        self.assertEqual(normalize_header_name("___policy___number___"), "policy number")

    def test_match_header_to_field_canonical_and_aliases(self):
        fields = [
            {"name": "customer_name", "aliases": ["name", "customer name", "insured name"]},
            {"name": "phone", "aliases": ["mobile", "mobile no", "phone number", "contact"]},
            {"name": "policy_number", "aliases": ["policy no", "policy"]},
            {"name": "premium_amount", "aliases": ["premium"]},
        ]
        self.assertEqual(match_header_to_field("Customer_Name", fields), "customer_name")
        self.assertEqual(match_header_to_field("Insured Name", fields), "customer_name")
        self.assertEqual(match_header_to_field("Mobile No", fields), "phone")
        self.assertEqual(match_header_to_field("CONTACT", fields), "phone")
        self.assertEqual(match_header_to_field("Policy No", fields), "policy_number")
        self.assertEqual(match_header_to_field("PREMIUM", fields), "premium_amount")
        # Unknown column should return None
        self.assertIsNone(match_header_to_field("Unknown_Column", fields))
        self.assertIsNone(match_header_to_field("Agent_Notes", fields))

    # 2. Phone Number Normalisation Tests
    def test_phone_valid_cases(self):
        # 10 digits starting with 6, 7, 8, 9
        res, valid = normalize_phone("9876543210")
        self.assertTrue(valid)
        self.assertEqual(res, "+919876543210")

        # 12 digits starting with 91
        res, valid = normalize_phone("919876543210")
        self.assertTrue(valid)
        self.assertEqual(res, "+919876543210")

        # 11 digits starting with 0
        res, valid = normalize_phone("09876543210")
        self.assertTrue(valid)
        self.assertEqual(res, "+919876543210")

        # Mixed formats with spaces, hyphens, and brackets
        res, valid = normalize_phone("+91 98765 43210")
        self.assertTrue(valid)
        self.assertEqual(res, "+919876543210")

        res, valid = normalize_phone("91-98765-43210")
        self.assertTrue(valid)
        self.assertEqual(res, "+919876543210")

        res, valid = normalize_phone("(+91) 9876543210")
        self.assertTrue(valid)
        self.assertEqual(res, "+919876543210")

        # Starting digits 6, 7, 8
        self.assertEqual(normalize_phone("6123456789")[0], "+916123456789")
        self.assertEqual(normalize_phone("7123456789")[0], "+917123456789")
        self.assertEqual(normalize_phone("8123456789")[0], "+918123456789")

    def test_phone_invalid_cases(self):
        # Starts with invalid prefix (e.g. 5)
        res, valid = normalize_phone("5123456789")
        self.assertFalse(valid)
        self.assertIsNone(res)

        # 9 digits (too short)
        res, valid = normalize_phone("987654321")
        self.assertFalse(valid)
        self.assertIsNone(res)

        # Landline with area code
        res, valid = normalize_phone("022-2500-1234")
        self.assertFalse(valid)
        self.assertIsNone(res)

        # Non-Indian / international number
        res, valid = normalize_phone("+1 415 555 0134")
        self.assertFalse(valid)
        self.assertIsNone(res)

        # Text strings
        res, valid = normalize_phone("N/A")
        self.assertFalse(valid)
        self.assertIsNone(res)

    def test_phone_missing_cases(self):
        # Empty string and None are treated as missing (valid=True, res=None)
        self.assertEqual(normalize_phone(""), (None, True))
        self.assertEqual(normalize_phone("   "), (None, True))
        self.assertEqual(normalize_phone(None), (None, True))

    # 3. Date Normalisation Tests
    def test_date_valid_formats(self):
        # YYYY-MM-DD
        self.assertEqual(normalize_date("2026-10-15"), ("2026-10-15", True))
        # DD/MM/YYYY (day-first)
        self.assertEqual(normalize_date("15/10/2026"), ("2026-10-15", True))
        # DD-MM-YYYY
        self.assertEqual(normalize_date("15-10-2026"), ("2026-10-15", True))
        # DD-Mon-YYYY (case-insensitive month name)
        self.assertEqual(normalize_date("15-Oct-2026"), ("2026-10-15", True))
        self.assertEqual(normalize_date("15-OCT-2026"), ("2026-10-15", True))
        self.assertEqual(normalize_date("01-dec-2026"), ("2026-12-01", True))

    def test_date_invalid_and_impossible(self):
        # Impossible date: Feb 31
        self.assertEqual(normalize_date("31/02/2026"), (None, False))
        # Invalid month: month 13
        self.assertEqual(normalize_date("2026-13-01"), (None, False))
        # Text string
        self.assertEqual(normalize_date("soon"), (None, False))
        self.assertEqual(normalize_date("TBD"), (None, False))

    # 4. Decimal & Currency Normalisation Tests
    def test_decimal_valid_cases(self):
        self.assertEqual(normalize_decimal("15000"), (15000.0, True))
        self.assertEqual(normalize_decimal("15000.5"), (15000.5, True))
        self.assertEqual(normalize_decimal("15000.556"), (15000.56, True))
        # Rupee symbol
        self.assertEqual(normalize_decimal("₹12,500"), (12500.0, True))
        self.assertEqual(normalize_decimal("₹ 12,500.50"), (12500.5, True))
        # Rs and Rs. prefixes
        self.assertEqual(normalize_decimal("Rs. 4000"), (4000.0, True))
        self.assertEqual(normalize_decimal("rs 2500"), (2500.0, True))
        # INR prefix
        self.assertEqual(normalize_decimal("INR 38000.75"), (38000.75, True))

    def test_decimal_invalid_cases(self):
        # Negative value
        self.assertEqual(normalize_decimal("-500"), (None, False))
        self.assertEqual(normalize_decimal("₹ -100"), (None, False))
        # Non-numeric text
        self.assertEqual(normalize_decimal("twelve thousand"), (None, False))
        self.assertEqual(normalize_decimal("N/A"), (None, False))

    # 5. Enum Normalisation Tests
    def test_enum_normalisation(self):
        allowed = ["en", "hi", "ta", "kn", "te", "mr"]
        self.assertEqual(normalize_enum("EN", allowed), ("en", True))
        self.assertEqual(normalize_enum("  Hi  ", allowed), ("hi", True))
        self.assertEqual(normalize_enum("Ta", allowed), ("ta", True))
        # Invalid values
        self.assertEqual(normalize_enum("Hindi", allowed), (None, False))
        self.assertEqual(normalize_enum("english", allowed), (None, False))
        self.assertEqual(normalize_enum("french", allowed), (None, False))


if __name__ == "__main__":
    unittest.main()

