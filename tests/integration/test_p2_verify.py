"""P2 view verification on a synthetic PostgreSQL database only."""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import text

from assistant.db.uow import transaction
from assistant.privacy.content_columns import CONTENT_COLUMNS
from tests.integration.business_env import Env, business_message, deliver
from tests.integration.p2_factories import seed_account, seed_action, seed_conversation
from tests.integration.test_data_deletion import OTHER_OWNER, env, hits, seed

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/acceptance/p2_verify.py"
CANARY = "p2qacontentcanary"


async def run(url: str, *args: str, flags: str = "") -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        env={"DATABASE_URL": url, "FEATURE_FLAGS": flags, "UNRELATED_SETTING": CANARY},
        capture_output=True,
        text=True,
        check=False,
    )


async def test_release_prints_flag_names_and_no_other_setting(env: Env, migrated_url: str) -> None:
    async with transaction(env.factory) as uow:
        account = await seed_account(uow.session)
        chat = await seed_conversation(uow.session, account_id=account.id)
        await seed_action(uow.session, account_id=account.id, conversation_id=chat.id)
    result = await run(
        migrated_url, "release", flags=" tool.get_activity ,actions.send, tool.get_activity "
    )
    assert result.returncode == 0, result.stderr
    assert "0014" in result.stdout
    assert "actions.send" in result.stdout
    assert "tool.get_activity" in result.stdout
    assert CANARY not in result.stdout + result.stderr
    assert "state=proposed count=1" in result.stdout


async def test_every_mode_is_content_free(env: Env, migrated_url: str) -> None:
    seeded = await seed(env)
    async with transaction(env.factory) as uow:
        for table, column in sorted(CONTENT_COLUMNS):
            if column == "search_tsv":
                continue  # generated from search_key
            json_column = column in {"media", "args", "payload", "summary", "reasons", "spec"}
            value = (
                "jsonb_build_array(CAST(:canary AS text))"
                if column == "reasons"
                else "jsonb_build_object('canary', CAST(:canary AS text))"
                if json_column
                else ":canary"
            )
            await uow.session.execute(
                text(f'UPDATE app."{table}" SET "{column}" = {value}'),  # noqa: S608 - registry identifiers
                {"canary": CANARY},
            )
        action_id = await uow.session.scalar(
            text("SELECT min(id) FROM app.actions WHERE account_id = :a"), {"a": seeded.account_id}
        )
    assert await hits(env, CANARY) == {f"{t}.{c}" for t, c in CONTENT_COLUMNS}
    for args in (
        ("release",),
        ("actions",),
        ("action", str(action_id)),
        ("script", "P2-4", "--expect", "1-3"),
        ("notifications",),
        ("tasks",),
        ("digests",),
    ):
        result = await run(migrated_url, *args)
        assert result.returncode == 0, result.stderr
        assert CANARY not in result.stdout + result.stderr


async def test_script_mode_reports_found_missing_and_unexpected_numbers_only(
    env: Env, migrated_url: str
) -> None:
    from tests.integration.business_env import connect

    await connect(env)
    for number in (401, 403, 409):
        await deliver(
            env, business_message(message_id=number, message_text=f"P2-{number} {CANARY}")
        )
    result = await run(
        migrated_url, "script", "P2-4", "--expect", "401-403", "--direction", "contact"
    )
    assert result.returncode == 0, result.stderr
    assert "found=[401, 403, 409]" in result.stdout
    assert "missing=[402]" in result.stdout
    assert "unexpected=[409]" in result.stdout
    assert CANARY not in result.stdout
    empty = await run(migrated_url, "script", "P2-5", "--expect", "501")
    assert empty.returncode == 0
    assert "found=[] missing=[501] unexpected=[]" in empty.stdout


async def test_modes_run_read_only(migrated_url: str) -> None:
    verifier = importlib.import_module("tools.acceptance.p2_verify")

    def attempt_write() -> None:
        with verifier.open_database(migrated_url) as conn:
            assert conn.execute("SHOW transaction_read_only").fetchone()[0] == "on"
            assert conn.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute("CREATE TABLE app.qa_should_never_exist (id bigint)")

    await asyncio.to_thread(attempt_write)


@pytest.mark.parametrize(
    "args",
    [
        ("action", "0"),
        ("action", "1;SELECT"),
        ("actions", "--since", "2026-10-27"),
        ("script", "P2-abc", "--expect", "1"),
        ("script", "P2-4", "--expect", "3-1"),
        ("script", "P2-4", "--expect", "1-99999999"),
        ("script", "P2-4", "--expect", "1,,2"),
    ],
)
def test_bad_arguments_exit_2_and_never_reach_sql(
    monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...]
) -> None:
    verifier = importlib.import_module("tools.acceptance.p2_verify")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("bad arguments reached the database")

    monkeypatch.setattr(verifier.psycopg, "connect", forbidden)
    with pytest.raises(SystemExit) as error:
        verifier.main(list(args))
    assert error.value.code == 2


__all__ = ["OTHER_OWNER", "env"]
