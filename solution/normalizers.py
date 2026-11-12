"""
Data normalisation module for Assignment A: CSV Lead Ingestion.

Provides pure functions for:
- Header normalisation and schema alias matching
- Indian phone number cleaning, canonicalization (+91XXXXXXXXXX), and validation
- Multi-format date parsing (YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY)
- Currency and decimal parsing (handling ₹, Rs., Rs, INR, commas)
- Enum case-insensitive validation and lowercase storage
"""

from datetime import datetime
import re
from typing import Any, Dict, List, Optional, Tuple


def normalize_header_name(header: str) -> str:
    """
    Normalises a CSV header string per assignment rules:
    1. Lowercase it.
    2. Strip leading and trailing whitespace.
    3. Replace any sequence of underscores and whitespace with a single space.
    """
    if not header:
        return ""
    return re.sub(r"[\s_]+", " ", header.lower()).strip()


def match_header_to_field(header: str, schema_fields: List[Dict[str, Any]]) -> Optional[str]:
    """
    Matches a normalized CSV header to a schema field name or one of its aliases.
    Returns the canonical field name if matched, or None if unknown.
    """
    normalized_hdr = normalize_header_name(header)
    if not normalized_hdr:
        return None

    for field in schema_fields:
        # Check canonical field name
        canonical_name = field["name"]
        if normalized_hdr == normalize_header_name(canonical_name):
            return canonical_name

        # Check all aliases defined for this field
        aliases = field.get("aliases", [])
        for alias in aliases:
            if normalized_hdr == normalize_header_name(alias):
                return canonical_name

    return None


def normalize_phone(raw_value: Optional[str]) -> Tuple[Optional[str], bool]:
    """
    Normalises an Indian phone number:
    1. Remove all non-digit characters.
    2. If 12 digits starting with '91', drop the '91'.
    3. If 11 digits starting with '0', drop the '0'.
    4. Valid if and only if exactly 10 digits and starts with 6, 7, 8, or 9.
    5. Returns canonical format '+91XXXXXXXXXX' and a boolean flag is_valid.

    Returns:
    - (None, True) if raw_value is missing/empty (missing field, not invalid).
    - ('+91XXXXXXXXXX', True) if valid.
    - (None, False) if a phone number was supplied but fails validation rules.
    """
    if raw_value is None:
        return None, True

    trimmed = raw_value.strip()
    if not trimmed:
        return None, True

    digits = re.sub(r"\D", "", trimmed)

    # Strip country code or trunk prefix
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    # Must be exactly 10 digits starting with 6, 7, 8, or 9
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}", True

    return None, False


def normalize_date(raw_value: Optional[str]) -> Tuple[Optional[str], bool]:
    """
    Parses a date string supporting multiple formats:
    - YYYY-MM-DD
    - DD/MM/YYYY (day-first)
    - DD-MM-YYYY (day-first)
    - DD-Mon-YYYY (e.g. 01-Dec-2026, case-insensitive)

    Returns:
    - (None, True) if raw_value is None or empty.
    - ('YYYY-MM-DD', True) if successfully parsed into a valid date.
    - (None, False) if present but invalid (e.g. impossible date 31/02/2026).
    """
    if raw_value is None:
        return None, True

    trimmed = raw_value.strip()
    if not trimmed:
        return None, True

    supported_formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d-%b-%Y",
    ]

    for fmt in supported_formats:
        try:
            parsed_dt = datetime.strptime(trimmed, fmt)
            return parsed_dt.strftime("%Y-%m-%d"), True
        except ValueError:
            continue

    return None, False


def normalize_decimal(raw_value: Optional[str]) -> Tuple[Optional[float], bool]:
    """
    Normalises currency and decimal amounts:
    - Strips optional prefixes: ₹, Rs., Rs, INR (case-insensitive, optional space).
    - Removes thousands separator commas.
    - Rejects negative values.
    - Rounds to 2 decimal places.

    Returns:
    - (None, True) if raw_value is None or empty.
    - (rounded_float, True) if successfully parsed and >= 0.
    - (None, False) if invalid string or negative.
    """
    if raw_value is None:
        return None, True

    trimmed = raw_value.strip()
    if not trimmed:
        return None, True

    # Strip currency prefixes (e.g. '₹', 'Rs.', 'Rs', 'INR') with optional whitespace
    cleaned = re.sub(r"^(?:₹|rs\.?|inr)\s*", "", trimmed, flags=re.IGNORECASE)
    # Remove commas
    cleaned = cleaned.replace(",", "")

    try:
        val = float(cleaned)
        if val < 0:
            return None, False
        return round(val, 2), True
    except ValueError:
        return None, False


def normalize_enum(raw_value: Optional[str], allowed_values: List[str]) -> Tuple[Optional[str], bool]:
    """
    Validates an enum string against allowed values case-insensitively.
    Returns:
    - (None, True) if raw_value is None or empty.
    - (lowercase_value, True) if matched.
    - (None, False) if not in allowed values.
    """
    if raw_value is None:
        return None, True

    trimmed = raw_value.strip()
    if not trimmed:
        return None, True

    lowered = trimmed.lower()
    allowed_lowered = [v.lower() for v in allowed_values]

    if lowered in allowed_lowered:
        return lowered, True

    return None, False
