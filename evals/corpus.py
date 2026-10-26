"""The synthetic E1 corpus in an isolated database, for the retrieval target.

The retrieval set measures the product's real search service
(`assistant.memory.retrieval`), so the corpus lives in a real, migrated
PostgreSQL with `pg_trgm` and the generated `search_tsv`, loaded the way
ingestion stores messages (`normalize` -> `search_key`, `detect_script`,
`guess_lang`).

Isolation (AGENTS §9: never dev or production data):

- `isolated_corpus` creates a **throwaway database** named
  `assistant_eval_<random>` on the server of `EVAL_DATABASE_URL` (default
  `TEST_DATABASE_URL`, never `DATABASE_URL`), migrates it to head, loads the
  corpus under one synthetic account and drops the database afterwards. It
  touches no existing database.
- `load_corpus` / `unload_corpus` load under a fresh account in a database you
  already have (the integration tests use the shared test database and delete
  the account, which cascades).

What is loaded is exactly what the product would hold: each message's
*current* text (edits applied), tombstones for deleted messages (no text, no
key: the product purges them), media rows without text. The reviewer-only
fields `original_text` and `purged_text` are never loaded.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account
from assistant.conversations.models import Contact, Message
from assistant.conversations.models import Conversation as ConversationRow
from assistant.db.engine import build_engine, build_session_factory
from assistant.memory.search import detect_script, guess_lang, normalize
from evals.errors import ConfigurationError
from evals.schema import Conversation
from evals.schema import Message as DatasetMessage

ROOT = Path(__file__).resolve().parents[1]
#: The owner's timezone in the dataset (README: relative dates resolve here).
OWNER_TIMEZONE = "Asia/Tashkent"
#: Queries run "a day after the last message": a fixed instant, so the
#: recency bonus (`retrieval.RECENCY_*`) is the same on every run.
NOW_AFTER_CORPUS = timedelta(days=1)
_SYNTHETIC_OWNER_ID = 7_000_000_099


@dataclass(frozen=True, slots=True)
class LoadedCorpus:
    session_factory: async_sessionmaker[AsyncSession]
    account_id: int
    #: The instant queries are evaluated at (see `NOW_AFTER_CORPUS`).
    now: datetime
    timezone: str
    #: Database message id -> dataset message id (`c09.m05`).
    refs: Mapping[int, str]
    #: Conversation id -> the contact's display name, for reference checks.
    contact_names: Mapping[str, str]


# ------------------------------------------------------------------- loading
def _row_values(message: DatasetMessage, index: int) -> dict[str, Any]:
    direction = "contact" if message.direction == "contact" else "owner_manual"
    values: dict[str, Any] = {
        "telegram_message_id": index,
        "direction": direction,
        "sent_at": message.sent_at,
        "edited_at": message.edited_at,
        "content_type": "text",
    }
    if message.deleted:
        # A tombstone, as deletion leaves it: no content, no search key.
        values["deleted_at"] = message.deleted_at
        values["content_type"] = "unknown"
        return values
    text = message.text
    if message.media is not None:
        values["content_type"] = message.media
        values["media"] = {"kind": message.media}
    if text:
        values["text"] = text
        values["search_key"] = normalize(text)
        values["script"] = detect_script(text)
        values["lang_guess"] = guess_lang(text)
    return values


async def load_corpus(
    session_factory: async_sessionmaker[AsyncSession], conversations: Sequence[Conversation]
) -> LoadedCorpus:
    """Load `conversations` under a new synthetic account."""
    refs: dict[int, str] = {}
    async with session_factory() as session, session.begin():
        account = Account(
            telegram_user_id=_SYNTHETIC_OWNER_ID + secrets.randbelow(900_000_000),
            status="active",
            timezone=OWNER_TIMEZONE,
        )
        session.add(account)
        await session.flush()
        last = datetime.min.replace(tzinfo=UTC)
        for number, conversation in enumerate(conversations, start=1):
            contact = Contact(
                account_id=account.id,
                telegram_user_id=1_000_000 + number,
                display_name=conversation.contact.display_name,
            )
            session.add(contact)
            await session.flush()
            row = ConversationRow(
                account_id=account.id,
                business_chat_id=1_000_000 + number,
                contact_id=contact.id,
                coverage_started_at=conversation.coverage_started_at,
            )
            session.add(row)
            await session.flush()
            for index, message in enumerate(conversation.messages, start=1):
                values = _row_values(message, index)
                stored = Message(account_id=account.id, conversation_id=row.id, **values)
                session.add(stored)
                await session.flush()
                refs[stored.id] = message.id
                last = max(last, message.sent_at)
        account_id = account.id
    return LoadedCorpus(
        session_factory=session_factory,
        account_id=account_id,
        now=last + NOW_AFTER_CORPUS,
        timezone=OWNER_TIMEZONE,
        refs=refs,
        contact_names={c.id: c.contact.display_name for c in conversations},
    )


async def unload_corpus(session_factory: async_sessionmaker[AsyncSession], account_id: int) -> None:
    """Delete the synthetic account; every tenant row cascades."""
    async with session_factory() as session, session.begin():
        await session.execute(delete(Account).where(Account.id == account_id))


# ---------------------------------------------------------- scratch database
def eval_database_url() -> str:
    """The server to create the throwaway database on. Never `DATABASE_URL`."""
    url = os.environ.get("EVAL_DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")
    if not url:
        raise ConfigurationError(
            "set EVAL_DATABASE_URL (or TEST_DATABASE_URL): the server to create a "
            "throwaway evaluation database on; the dev database is never used"
        )
    return url


def _admin_url(base_url: str) -> str:
    return (
        sa.engine.make_url(base_url)
        .set(drivername="postgresql", database="postgres")
        .render_as_string(hide_password=False)
    )


def _create_database(base_url: str, name: str) -> str:
    with psycopg.connect(_admin_url(base_url), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    return sa.engine.make_url(base_url).set(database=name).render_as_string(hide_password=False)


#: The throwaway databases are `assistant_eval_` plus 8 hex digits, and nothing else
#: is ever listed or dropped by `leftover_databases` / `drop_leftover_databases`.
EVAL_DATABASE_PREFIX = "assistant_eval_"
_EVAL_DATABASE_NAME = re.compile(rf"^{EVAL_DATABASE_PREFIX}[0-9a-f]{{8}}$")


def leftover_databases(base_url: str) -> list[str]:
    """Throwaway databases a killed run left on the server of `base_url` (a normal
    run, a failure and Ctrl+C all drop theirs; a killed process cannot)."""
    with psycopg.connect(_admin_url(base_url), autocommit=True) as conn:
        rows = conn.execute("SELECT datname FROM pg_database ORDER BY datname").fetchall()
    return [name for (name,) in rows if _EVAL_DATABASE_NAME.fullmatch(name)]


def drop_leftover_databases(base_url: str, names: list[str]) -> list[str]:
    """Drop the named leftovers; a name that is not one of ours is ignored."""
    dropped = [name for name in names if _EVAL_DATABASE_NAME.fullmatch(name)]
    for name in dropped:
        _drop_database(base_url, name)
    return dropped


def _drop_database(base_url: str, name: str) -> None:
    with psycopg.connect(_admin_url(base_url), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _migrate(url: str) -> None:
    """Alembic to head. Sync on purpose: `migrations/env.py` owns its loop."""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    previous = os.environ.get("ALEMBIC_DATABASE_URL")
    os.environ["ALEMBIC_DATABASE_URL"] = url
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
        else:
            os.environ["ALEMBIC_DATABASE_URL"] = previous


@asynccontextmanager
async def scratch_database(base_url: str) -> AsyncIterator[str]:
    """A migrated throwaway database, dropped on exit. Yields its URL."""
    name = f"{EVAL_DATABASE_PREFIX}{secrets.token_hex(4)}"
    url = await asyncio.to_thread(_create_database, base_url, name)
    try:
        await asyncio.to_thread(_migrate, url)
        yield url
    finally:
        await asyncio.to_thread(_drop_database, base_url, name)


@asynccontextmanager
async def isolated_corpus(
    base_url: str, conversations: Sequence[Conversation]
) -> AsyncIterator[LoadedCorpus]:
    """The corpus in its own throwaway database (see the module docstring)."""
    async with scratch_database(base_url) as url:
        engine = build_engine(url)
        try:
            yield await load_corpus(build_session_factory(engine), conversations)
        finally:
            await engine.dispose()
