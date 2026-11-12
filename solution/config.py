"""
Configuration module for Assignment A: Interaction Ledger & Reporting Pipeline.

Centralizes file paths and application settings so that no path or environment
variable is hardcoded across different modules.
"""

from pathlib import Path
import os

# Project root: the directory containing the 'solution' folder
ROOT_DIR = Path(__file__).resolve().parent.parent

# Schemas directory containing motor_renewal.json, policy_feedback.json, dispositions.csv
SCHEMAS_DIR = ROOT_DIR / "assignment-a-data" / "schemas"

# Default reports output directory
DEFAULT_REPORTS_DIR = ROOT_DIR / "reports"

# Default SQLite database path (can be overridden via environment variable)
DEFAULT_DB_PATH = Path(os.getenv("LEDGER_DB_PATH", str(ROOT_DIR / "ledger.db")))

# IST Timezone offset (+05:30)
IST_TIMEZONE_STR = "+05:30"
