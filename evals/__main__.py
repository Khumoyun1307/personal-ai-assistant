"""Entry point: `python -m evals` (see `evals/cli.py`)."""

from __future__ import annotations

import sys

from evals.cli import main

if __name__ == "__main__":
    sys.exit(main())
