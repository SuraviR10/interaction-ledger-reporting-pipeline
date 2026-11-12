"""
Configuration module for Assignment A: Interaction Ledger & Reporting Pipeline.

Centralizes file paths, environment variable names, and application settings.
"""

from pathlib import Path
import os
from typing import Optional

# Project root: the directory containing the 'solution' folder
ROOT_DIR = Path(__file__).resolve().parent.parent

# Schemas directory containing motor_renewal.json, policy_feedback.json, dispositions.csv
SCHEMAS_DIR = ROOT_DIR / "assignment-a-data" / "schemas"

# Default reports output directory
DEFAULT_REPORTS_DIR = ROOT_DIR / "reports"

# Default SQLite database path (can be overridden via environment variable)
DEFAULT_DB_PATH = Path(os.getenv("LEDGER_DB_PATH", str(ROOT_DIR / "ledger.db")))

# Environment variable names for security keys
PII_ENCRYPTION_KEY_ENV = "PII_ENCRYPTION_KEY"
PHONE_HASH_KEY_ENV = "PHONE_HASH_KEY"

# IST Timezone offset (+05:30)
IST_TIMEZONE_STR = "+05:30"


def load_env_file(env_file_path: Optional[Path] = None) -> None:
    """
    Loads key=value pairs from a .env file into os.environ if not already set.
    Uses standard library only, with no external dependencies.
    """
    path = env_file_path or (ROOT_DIR / ".env")
    if not path.is_file():
        return

    with open(path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            # Skip comments and empty lines
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            # Strip potential quotes around values
            val = val.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = val


# Attempt to load .env on module import if present
load_env_file()
