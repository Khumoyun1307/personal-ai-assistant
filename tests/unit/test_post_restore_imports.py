"""The scratch restore entrypoints must not load any Telegram adapter."""

import subprocess
import sys


def test_post_restore_and_p2_verify_never_import_telegram() -> None:
    code = """
import importlib
import sys
import assistant.__main__
import assistant.privacy.post_restore
import tools.acceptance.p2_verify
import tools.acceptance.restore_verify
assert not any(name == 'assistant.telegram' or name.startswith('assistant.telegram.')
               for name in sys.modules)
"""
    subprocess.run(  # noqa: S603 — fixed import probe, no external input
        [sys.executable, "-c", code], check=True, capture_output=True
    )
