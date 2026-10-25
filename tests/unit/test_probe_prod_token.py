"""`probe --prod-token` reaches production only through non-polling calls (D40).

A laptop that polls the production bot steals its updates and advances the
server's offset; a send acts on real chats. So the production token is
accepted for `me` and `getfile` only, and read from var/prod.env without
being printed. `tools/` is outside `src/`, so it is loaded by path.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROBE_PATH = Path(__file__).resolve().parents[2] / "tools" / "probe" / "probe.py"
PROD_TOKEN = "1111111111:" + "P" * 35


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("probe_prod", PROBE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_prod"] = module
    spec.loader.exec_module(module)
    return module


probe = _load()


@pytest.fixture
def prod_env(tmp_path: Path) -> Path:
    path = tmp_path / "prod.env"
    path.write_text(
        f'# production\nBOT_TOKEN="{PROD_TOKEN}"\nOWNER_TELEGRAM_IDS=7000000001\n', encoding="utf-8"
    )
    return path


@pytest.mark.parametrize("command", sorted(probe.PROD_TOKEN_COMMANDS))
def test_non_polling_commands_get_the_production_token(
    prod_env: Path,
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "local-token")
    probe.use_prod_token(command, prod_env)
    assert os.environ["BOT_TOKEN"] == PROD_TOKEN
    out = capsys.readouterr()
    assert PROD_TOKEN not in out.out + out.err


@pytest.mark.parametrize(
    "command",
    ["listen", "send", "dm", "edit", "typing", "delete", "sendphoto", "burst", "connection"],
)
def test_polling_and_sending_commands_refuse_the_production_token(
    prod_env: Path, command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "local-token")
    with pytest.raises(SystemExit) as exc:
        probe.use_prod_token(command, prod_env)
    assert exc.value.code == 2
    assert os.environ["BOT_TOKEN"] == "local-token"


def test_every_probe_command_is_classified() -> None:
    assert set(probe.COMMANDS) >= probe.PROD_TOKEN_COMMANDS
    assert "listen" not in probe.PROD_TOKEN_COMMANDS


def test_a_missing_production_file_or_token_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "local-token")
    with pytest.raises(SystemExit):
        probe.use_prod_token("getfile", tmp_path / "absent.env")
    empty = tmp_path / "empty.env"
    empty.write_text("OWNER_TELEGRAM_IDS=1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        probe.use_prod_token("getfile", empty)
    assert os.environ["BOT_TOKEN"] == "local-token"


def test_the_flag_is_parsed_before_the_command() -> None:
    args = probe.build_parser().parse_args(
        ["--prod-token", "getfile", "--latest", "photo", "--download"]
    )
    assert args.prod_token is True
    assert args.command == "getfile"


DEV_TOKEN = "2222222222:" + "D" * 35


def _dump(tmp_path: Path, records: list[dict[str, object]]) -> None:
    import json

    (tmp_path / "updates-20261025.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )


def _photo(file_id: str, **extra: object) -> dict[str, object]:
    message = {"photo": [{"file_id": file_id + "-small"}, {"file_id": file_id}]}
    return {"update_type": "business_message", "payload": {"business_message": message}, **extra}


def test_latest_file_id_only_returns_this_bots_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # File ids work only for the bot that received them (D40: two bots, one dump).
    monkeypatch.setattr(probe, "DUMP_DIR", tmp_path)
    _dump(
        tmp_path,
        [
            _photo("legacy-alviro"),
            _photo("alviro", bot_id="1111111111"),
            _photo("dev", bot_id="2222222222"),
        ],
    )
    monkeypatch.setenv("BOT_TOKEN", PROD_TOKEN)
    assert probe.latest_file_id("photo", accept_legacy=True) == "alviro"
    monkeypatch.setenv("BOT_TOKEN", DEV_TOKEN)
    assert probe.latest_file_id("photo", accept_legacy=False) == "dev"


def test_records_from_before_the_switch_count_only_for_the_production_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "DUMP_DIR", tmp_path)
    _dump(tmp_path, [_photo("legacy-alviro")])
    monkeypatch.setenv("BOT_TOKEN", DEV_TOKEN)
    assert probe.latest_file_id("photo", accept_legacy=False) is None
    monkeypatch.setenv("BOT_TOKEN", PROD_TOKEN)
    assert probe.latest_file_id("photo", accept_legacy=True) == "legacy-alviro"


def test_every_record_names_its_bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    monkeypatch.setattr(probe, "DUMP_DIR", tmp_path)
    monkeypatch.setenv("BOT_TOKEN", DEV_TOKEN)
    probe.write_record({"kind": "api_result"})
    (line,) = next(tmp_path.glob("updates-*.jsonl")).read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["bot_id"] == "2222222222"


def test_a_failed_download_never_prints_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    import aiohttp
    from yarl import URL

    monkeypatch.setattr(probe, "DUMP_DIR", tmp_path)
    monkeypatch.setenv("BOT_TOKEN", PROD_TOKEN)
    url = URL(f"https://api.telegram.org/file/bot{PROD_TOKEN}/photos/file_1.jpg")

    async def failing_download(bot: object) -> None:
        info = aiohttp.RequestInfo(url, "GET", headers=None, real_url=url)  # type: ignore[arg-type]
        raise aiohttp.ClientResponseError(info, (), status=404, message="Not Found")

    asyncio.run(probe.with_bot("getFile(photo)", failing_download))

    out = capsys.readouterr()
    assert "ClientResponseError" in out.out
    assert PROD_TOKEN not in out.out + out.err
    assert PROD_TOKEN not in "".join(
        p.read_text(encoding="utf-8") for p in tmp_path.glob("*.jsonl")
    )
