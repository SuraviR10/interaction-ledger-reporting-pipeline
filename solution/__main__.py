"""
Entrypoint for running the solution package directly:
python -m solution ingest data/drop_1
python -m solution report --out reports/
"""

import sys
from solution.cli import main

if __name__ == "__main__":
    sys.exit(main())
