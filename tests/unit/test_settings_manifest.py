"""Every app setting needs a reviewed production deployment classification."""

import re
from pathlib import Path

from assistant.config import Settings

ROOT = Path(__file__).resolve().parents[2]

# These settings deliberately bypass the optional tuning inventory. Keep the
# reasons explicit: a new Settings field must never disappear into a wildcard.
EXCLUDED_SETTINGS = {
    "APP_ENV": "forced to prod",
    "LOG_FORMAT": "forced to json",
    "DATABASE_URL": "generated from the persistent database password",
    "TEST_DATABASE_URL": "dev/test only",
    "BOT_TOKEN": "secret, existing required input",
    "OWNER_TELEGRAM_IDS": "required allowlist with CLI override",
    "OPENAI_API_KEY": "secret, existing required input",
    "HEARTBEAT_URL": "secret ping URL, existing required input",
    "BACKUP_STATUS_PATH": "fixed by the production Compose mount",
    "BACKUP_REMOTE": "generated from the B2 bucket",
}


def test_production_settings_inventory_covers_every_app_field() -> None:
    keys = [
        line
        for raw in (ROOT / "deploy/settings-manifest.txt").read_text("utf-8").splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    ]
    assert keys
    assert all(re.fullmatch(r"[A-Z][A-Z0-9_]*", key) for key in keys)
    assert len(keys) == len(set(keys))
    assert not set(keys) & EXCLUDED_SETTINGS.keys()
    fields = {name.upper() for name in Settings.model_fields}
    # BACKUP_HOUR_UTC belongs to the production backup sidecar, not Settings.
    assert set(keys) - fields == {"BACKUP_HOUR_UTC"}
    assert fields == (set(keys) - {"BACKUP_HOUR_UTC"}) | EXCLUDED_SETTINGS.keys()
