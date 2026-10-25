"""The evaluation tooling's own errors.

`evals/cli.py` turns exactly these into a clean message and a non-zero exit;
anything else is a bug and keeps its traceback.
"""

from __future__ import annotations


class EvalsError(Exception):
    """An expected, explainable failure of an evaluation command."""


class RefusedError(EvalsError):
    """The run was refused before it started. The message says why."""


class ConfigurationError(EvalsError):
    """Something the run needs is missing or invalid. Never carries a secret."""


class LedgerError(EvalsError):
    """The owner's review ledger cannot be read."""
