"""The laptop's switch to the dev bot (D40, M02 step D).

The main account's id must reach var/prod.env before .env forgets it, the
production token must never stay in .env, and nothing may be printed but
setting names. `tools/` is outside `src/`, so it is loaded by path. Every
value here is synthetic.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SWITCH_PATH = Path(__file__).resolve().parents[2] / "tools" / "dev" / "switch_dev_bot.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("switch_dev_bot", SWITCH_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["switch_dev_bot"] = module
    spec.loader.exec_module(module)
    return module


switch = _load()

PROD_TOKEN = "1111111111:" + "P" * 35
OLD_TOKEN = "1111111111:" + "O" * 35
DEV_TOKEN = "2222222222:" + "D" * 35
MAIN_ID = "7000000001"
DEV_ID = "7000000002"
HEARTBEAT = "https://hc.example.test/ping/synthetic"


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    env = tmp_path / ".env"
    env.write_text(
        "# local settings\n"
        f"BOT_TOKEN={OLD_TOKEN}\n"
        f"OWNER_TELEGRAM_IDS={MAIN_ID}\n"
        "PROBE_ALLOWED_CHAT_IDS=7000000003\n"
        "OPENAI_API_KEY=sk-dev-synthetic-key-0000000000\n"
        f"HEARTBEAT_URL={HEARTBEAT}\n"
        "POSTGRES_PASSWORD=dev-password\n",
        encoding="utf-8",
    )
    prod = tmp_path / "var" / "prod.env"
    prod.parent.mkdir()
    prod.write_text(
        f"BOT_TOKEN={PROD_TOKEN}\n"
        "OPENAI_API_KEY=sk-prod-synthetic-key-000000000\n"
        f'HEARTBEAT_URL="{HEARTBEAT}"\n',
        encoding="utf-8",
    )
    dev = tmp_path / "var" / "dev-bot.env"
    dev.write_text(f"DEV_BOT_TOKEN={DEV_TOKEN}\nDEV_OWNER_TELEGRAM_ID={DEV_ID}\n", encoding="utf-8")
    return {
        "env": env,
        "prod": prod,
        "dev": dev,
        "backup": tmp_path / "var" / "dev-switch" / "env.before",
    }


def _run(files: dict[str, Path]) -> int:
    return int(
        switch.main(
            [
                "--env",
                str(files["env"]),
                "--prod",
                str(files["prod"]),
                "--dev",
                str(files["dev"]),
                "--backup",
                str(files["backup"]),
            ]
        )
    )


def test_the_main_account_moves_to_prod_settings_and_env_takes_the_dev_bot(
    files: dict[str, Path],
) -> None:
    assert _run(files) == 0

    prod = switch.read_env(files["prod"])
    env = switch.read_env(files["env"])
    assert prod["OWNER_TELEGRAM_IDS"] == MAIN_ID
    assert prod["BOT_TOKEN"] == PROD_TOKEN
    assert env["BOT_TOKEN"] == DEV_TOKEN
    assert env["OWNER_TELEGRAM_IDS"] == DEV_ID
    assert env["PROBE_ALLOWED_CHAT_IDS"] == f"{DEV_ID},{MAIN_ID}"
    assert env["HEARTBEAT_URL"] == ""  # the production heartbeat is never pinged from a laptop
    assert env["POSTGRES_PASSWORD"] == "dev-password"  # untouched
    assert files["env"].read_text(encoding="utf-8").startswith("# local settings\n")


def test_the_previous_settings_are_kept_once(files: dict[str, Path]) -> None:
    before = files["env"].read_bytes()
    assert _run(files) == 0
    assert files["backup"].read_bytes() == before

    files["env"].write_text(
        f"BOT_TOKEN={OLD_TOKEN}\nOWNER_TELEGRAM_IDS={MAIN_ID}\n", encoding="utf-8"
    )
    assert _run(files) == 0
    assert files["backup"].read_bytes() == before


def test_a_second_run_changes_nothing(files: dict[str, Path]) -> None:
    assert _run(files) == 0
    env_after, prod_after = files["env"].read_bytes(), files["prod"].read_bytes()

    assert _run(files) == 0

    assert files["env"].read_bytes() == env_after
    assert files["prod"].read_bytes() == prod_after


def test_only_setting_names_are_printed(
    files: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    files["env"].write_text(
        files["env"]
        .read_text(encoding="utf-8")
        .replace("sk-dev-synthetic-key-0000000000", "sk-prod-synthetic-key-000000000"),
        encoding="utf-8",
    )
    assert _run(files) == 0
    out = capsys.readouterr()
    printed = out.out + out.err
    assert "OPENAI_API_KEY" in printed  # the shared-key note names the setting
    for secret in (PROD_TOKEN, OLD_TOKEN, DEV_TOKEN, MAIN_ID, DEV_ID, HEARTBEAT, "sk-prod"):
        assert secret not in printed


@pytest.mark.parametrize(
    ("target", "content", "reason"),
    [
        ("dev", f"DEV_BOT_TOKEN={DEV_TOKEN}\n", "DEV_OWNER_TELEGRAM_ID"),
        ("dev", f"DEV_BOT_TOKEN=REPLACE\nDEV_OWNER_TELEGRAM_ID={DEV_ID}\n", "DEV_BOT_TOKEN"),
        ("dev", f"DEV_BOT_TOKEN={PROD_TOKEN}\nDEV_OWNER_TELEGRAM_ID={DEV_ID}\n", "production bot"),
        ("dev", f"DEV_BOT_TOKEN={DEV_TOKEN}\nDEV_OWNER_TELEGRAM_ID={MAIN_ID}\n", "second account"),
        ("prod", "OPENAI_API_KEY=sk-x\n", "BOT_TOKEN"),
        ("prod", f"BOT_TOKEN={PROD_TOKEN}\nOWNER_TELEGRAM_IDS=7000000009\n", "differs"),
        ("env", f"BOT_TOKEN={OLD_TOKEN}\n", "main account"),
    ],
)
def test_a_failed_precondition_writes_nothing(
    files: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
    target: str,
    content: str,
    reason: str,
) -> None:
    files[target].write_text(content, encoding="utf-8")
    before = {name: path.read_bytes() for name, path in files.items() if path.exists()}

    assert _run(files) == 1

    assert reason in capsys.readouterr().err
    assert {name: path.read_bytes() for name, path in files.items() if path.exists()} == before


def test_switched_settings_without_the_main_id_in_prod_stop(files: dict[str, Path]) -> None:
    files["env"].write_text(
        f"BOT_TOKEN={DEV_TOKEN}\nOWNER_TELEGRAM_IDS={DEV_ID}\n", encoding="utf-8"
    )
    assert _run(files) == 1
    assert "OWNER_TELEGRAM_IDS" not in switch.read_env(files["prod"])
