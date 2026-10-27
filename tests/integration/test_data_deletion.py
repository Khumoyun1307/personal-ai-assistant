"""S03.5 data deletion (FR-PRV-04; ARCHITECTURE §10.1, §10.4).

The required S03.5 tests (IMPLEMENTATION_PLAN §5), each findable by name:

- after each deletion scope, a scan of every content column registered in
  `privacy/content_columns.py` finds no text from that scope —
  `test_deleting_a_conversation_leaves_no_text_from_it_in_any_content_column`,
  `test_deleting_a_contact_leaves_no_text_about_them_in_any_content_column`,
  `test_deleting_everything_leaves_no_content_of_the_account`;
- other conversations and accounts are untouched — the same three tests
  compare every other marker's hits before and after, column by column;
- the audit row is content-free —
  `test_each_deletion_writes_exactly_one_content_free_audit_row`;
- an in-flight summary never stores deleted text —
  `test_a_deletion_committed_during_a_summary_call_leaves_no_summary`.

The scan is driven by the registry (`CONTENT_COLUMNS`), so a content column
added later is covered without touching this file; the registry itself is
checked against the live schema by
`test_managed_data_deletion.py::test_every_text_or_jsonb_column_is_classified`.

Markers (synthetic): CHATK = Kamola's chat, NOTEK = the owner's notes about
Kamola, CHATB = Bobur's chat, NOTEB = notes about Bobur, OWNSYN = the owner's
own words, ACCTB = another account.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select, text

from assistant.accounts.models import AuditLogEntry
from assistant.accounts.service import record_control_turn
from assistant.actions.models import Action
from assistant.agent.models import ToolConfirmation
from assistant.agent.summarize import SummaryStatus
from assistant.automation.models import Automation
from assistant.inbox.models import ConversationTriage, Notification, Task
from assistant.memory import items
from assistant.privacy import deletion
from assistant.privacy.content_columns import CONTENT_COLUMNS
from tests.fakes.llm import FakeLLM
from tests.integration.agent_env import summarizer
from tests.integration.business_env import (
    CONTACT,
    OTHER_CONTACT,
    OWNER,
    T_BASE,
    Env,
    business_message,
    callback,
    connect,
    connection_update,
    deleted_messages,
    deliver,
    environment,
    onboard,
    private_message,
)

pytestmark = pytest.mark.integration

OTHER_OWNER = 7000000002
OTHER_CHAT = 7000000077
MARKERS = ("CHATK", "NOTEK", "CHATB", "NOTEB", "OWNSYN", "ACCTB")


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url, owner_ids=frozenset({OWNER, OTHER_OWNER})) as built:
        yield built


@dataclass(frozen=True)
class Seeded:
    account_id: int
    kamola_chat: int
    kamola: int
    bobur_chat: int
    bobur: int
    other_account_id: int


async def scalar(env: Env, sql: str, **params: Any) -> Any:
    async with env.factory() as session:
        return (await session.execute(text(sql), params)).scalar_one()


async def test_a_pending_action_in_a_deleted_conversation_is_cancelled_with_reason(
    env: Env,
) -> None:
    from tests.integration.test_action_lifecycle_bulk import seed_actions

    account_id, conversation_id, ids = await seed_actions(env)
    async with env.factory() as session, session.begin():
        report = await deletion.delete_conversation_data(
            session,
            account_id=account_id,
            conversation_id=conversation_id,
            actor="owner",
            clock=env.clock,
        )
    async with env.factory() as session:
        action = await session.get(Action, ids["awaiting_approval"])
        assert action is not None
        assert (action.state, action.state_reason, action.text, action.approval_token) == (
            "cancelled",
            "data_deleted",
            None,
            None,
        )
    assert report.actions == len(ids)


async def test_an_executing_action_keeps_its_state_when_its_chat_is_deleted(env: Env) -> None:
    from tests.integration.test_action_lifecycle_bulk import seed_actions

    account_id, conversation_id, ids = await seed_actions(env)
    async with env.factory() as session, session.begin():
        await deletion.delete_conversation_data(
            session,
            account_id=account_id,
            conversation_id=conversation_id,
            actor="owner",
            clock=env.clock,
        )
    async with env.factory() as session:
        action = await session.get(Action, ids["executing"])
        assert action is not None
        assert action.state == "executing"
        assert action.text is None


async def hits(env: Env, needle: str) -> set[str]:
    """Every registered content column that holds `needle` (any row)."""
    found: set[str] = set()
    async with env.factory() as session:
        for table, column in sorted(CONTENT_COLUMNS):
            row = await session.execute(
                text(
                    f'SELECT 1 FROM app."{table}" '  # noqa: S608 - registry identifiers
                    f'WHERE "{column}"::text ILIKE :needle LIMIT 1'
                ),
                {"needle": f"%{needle}%"},
            )
            if row.first() is not None:
                found.add(f"{table}.{column}")
    return found


async def all_hits(env: Env) -> dict[str, set[str]]:
    return {marker: await hits(env, marker) for marker in MARKERS}


def kept(before: dict[str, set[str]], marker: str) -> set[str]:
    """What must survive a per-chat or per-contact deletion: everything, except
    that the owner's raw control-chat updates are always cleared (they exist
    only for recovery and may quote any chat; safety review item 2)."""
    if marker == "OWNSYN":
        return before[marker] - {"inbound_updates.payload"}
    return before[marker]


async def seed(env: Env) -> Seeded:
    """Two chats of the owner, the owner's own words, and another account —
    each marker in every content column it can reach."""
    await connect(env)
    await deliver(
        env,
        business_message(message_id=1, message_text="CHATK one", first_name="CHATK Kamola"),
        business_message(
            message_id=1, message_text="CHATK one edited", edit=True, first_name="CHATK Kamola"
        ),
        business_message(message_id=2, message_text="CHATK two", username="chatk_user"),
        business_message(message_id=3, message_text="CHATK three", first_name="CHATK Kamola"),
    )
    await deliver(env, deleted_messages(message_ids=[3]))
    await deliver(
        env,
        business_message(
            message_id=1,
            from_id=OTHER_CONTACT,
            chat_id=OTHER_CONTACT,
            message_text="CHATB one",
            first_name="CHATB Bobur",
        ),
    )
    await deliver(env, private_message(OWNER, "OWNSYN question"))
    # The owner's raw control-chat updates that carry the chat's text (safety
    # review item 2): a processed button press whose message is the rendered
    # panel, and a message the owner forwarded from the chat.
    press = callback("settings:zzzzzzzzzz:noop")
    press["callback_query"]["message"]["text"] = "CHATK Kamola panel: summary"
    await deliver(env, press)
    forward = private_message(OWNER, "CHATK forwarded by the owner")
    forward["message"]["forward_from"] = {"id": CONTACT, "is_bot": False, "first_name": "K"}
    forward["message"]["forward_date"] = T_BASE
    await deliver(env, forward)
    # Another account with its own content.
    await onboard(env, OTHER_OWNER)
    await deliver(env, connection_update(user_id=OTHER_OWNER, connection_id="synthetic-conn-b"))
    await deliver(
        env,
        business_message(
            "synthetic-conn-b",
            from_id=OTHER_CHAT,
            chat_id=OTHER_CHAT,
            message_text="ACCTB keep",
            first_name="ACCTB Name",
        ),
    )
    account_id = await scalar(
        env, "SELECT id FROM app.accounts WHERE telegram_user_id = :u", u=OWNER
    )
    other_id = await scalar(
        env, "SELECT id FROM app.accounts WHERE telegram_user_id = :u", u=OTHER_OWNER
    )
    chats = {}
    async with env.factory() as session:
        for chat_id, conv, contact in (
            await session.execute(
                text("SELECT business_chat_id, id, contact_id FROM app.conversations")
            )
        ).all():
            chats[int(chat_id)] = (int(conv), int(contact))
    (kamola_chat, kamola), (bobur_chat, bobur) = chats[CONTACT], chats[OTHER_CONTACT]
    other_chat, other_contact = chats[OTHER_CHAT]
    async with env.factory() as session, session.begin():
        for conv, marker in ((kamola_chat, "CHATK"), (bobur_chat, "CHATB"), (other_chat, "ACCTB")):
            owner = other_id if marker == "ACCTB" else account_id
            for kind, start in (("rolling", None), ("period", env.clock.now())):
                await session.execute(
                    text(
                        "INSERT INTO app.conversation_summaries "
                        "(account_id, conversation_id, kind, period_start, summary) "
                        "VALUES (:a, :c, :k, :s, CAST(:j AS jsonb))"
                    ),
                    {
                        "a": owner,
                        "c": conv,
                        "k": kind,
                        "s": start,
                        "j": json.dumps({"overview": f"{marker} summary"}),
                    },
                )
        for owner, content, target in (
            (account_id, "NOTEK about Kamola", {"contact_id": kamola}),
            (account_id, "CHATK chat note", {"conversation_id": kamola_chat}),
            (account_id, "NOTEB about Bobur", {"contact_id": bobur}),
            (account_id, "CHATB chat note", {"conversation_id": bobur_chat}),
            (account_id, "OWNSYN own fact", {}),
            (other_id, "ACCTB note", {"contact_id": other_contact}),
        ):
            await items.save(
                session,
                account_id=owner,
                content=content,
                kind="fact",
                actor="owner",
                now=env.clock.now(),
                **target,  # type: ignore[arg-type]
            )
        # Owner-scope items saved by runs that read a chat (S03.3 provenance
        # refs): they may hold that chat's text, so they go with it.
        for content, refs in (
            ("CHATK owner fact from the chat", {"conversation_ids": [kamola_chat]}),
            ("CHATK owner fact about her", {"contact_ids": [kamola]}),
            ("CHATB owner fact from the chat", {"conversation_ids": [bobur_chat]}),
        ):
            await items.save(
                session,
                account_id=account_id,
                content=content,
                kind="fact",
                tainted=True,
                refs=refs,
                actor="owner",
                now=env.clock.now(),
            )
        for content, quoted, refs in (
            ("CHATK answer", None, {"conversation_ids": [kamola_chat], "contact_ids": []}),
            ("CHATK by contact", None, {"conversation_ids": [], "contact_ids": [kamola]}),
            ("OWNSYN asks", "CHATK quoted", {"conversation_ids": [kamola_chat], "contact_ids": []}),
            ("CHATB answer", None, {"conversation_ids": [bobur_chat], "contact_ids": [bobur]}),
        ):
            await record_control_turn(
                session,
                account_id=account_id,
                role="assistant" if quoted is None else "owner",
                text=content,
                quoted_text=quoted,
                contains_managed=quoted is not None,
                refs=refs,
            )
        for call, values in (
            ("k1", {"conversation_id": kamola_chat, "note": "CHATK held"}),
            ("k2", {"contact_id": kamola, "note": "NOTEK held"}),
            ("b1", {"conversation_id": bobur_chat, "note": "CHATB held"}),
            ("o1", {"note": "OWNSYN held"}),
        ):
            session.add(
                ToolConfirmation(
                    account_id=account_id,
                    tool_call_id=call,
                    tool_name="synthetic_configure",
                    args=values,
                )
            )
        for tenant, chat, person, marker in (
            (account_id, kamola_chat, kamola, "CHATK"),
            (account_id, bobur_chat, bobur, "CHATB"),
            (other_id, other_chat, other_contact, "ACCTB"),
        ):
            session.add(
                Action(
                    account_id=tenant,
                    conversation_id=chat,
                    kind="send_message",
                    origin="agent_proposal",
                    state="awaiting_approval",
                    text=f"{marker} draft",
                    approval_token=f"synthetic-{marker}",
                    card_state="awaiting_approval",
                )
            )
            session.add(
                Task(
                    account_id=tenant,
                    conversation_id=chat,
                    kind="open_question",
                    title=f"{marker} task",
                    details=f"{marker} details",
                    created_by="agent",
                )
            )
            session.add(
                ConversationTriage(
                    account_id=tenant,
                    conversation_id=chat,
                    covers_through_message_id=1,
                    needs_reply=True,
                    contains_question=True,
                    requests_decision=False,
                    urgency="normal",
                    escalation_category="none",
                    reasons=[f"{marker} reason"],
                    prompt_version="synthetic.v1",
                    model="synthetic-model",
                )
            )
            session.add(
                Automation(
                    account_id=tenant,
                    kind="monitor",
                    spec={"schema_version": 1, "text": f"{marker} condition"},
                    refs={"conversation_ids": [chat], "contact_ids": [person]},
                    timezone="UTC",
                    tz_policy="fixed",
                )
            )
            session.add(
                Notification(
                    account_id=tenant,
                    kind="monitor_match",
                    priority="normal",
                    dedupe_key=f"synthetic:{marker}",
                    refs={"conversation_ids": [chat]},
                    conversation_id=chat,
                    contact_id=person,
                )
            )
    return Seeded(account_id, kamola_chat, kamola, bobur_chat, bobur, other_id)


async def assert_seeded(env: Env) -> dict[str, set[str]]:
    before = await all_hits(env)
    # The seed reaches the columns each scope must clear.
    assert {
        "messages.text",
        "messages.search_key",
        "messages.search_tsv",
        "contacts.display_name",
        "inbound_updates.payload",
        "conversation_summaries.summary",
        "memory_items.content",
        "control_turns.text",
        "control_turns.quoted_text",
        "tool_confirmations.args",
        "actions.text",
        "tasks.title",
        "tasks.details",
        "conversation_triage.reasons",
        "automations.spec",
    } <= before["CHATK"]
    assert {"memory_items.content", "tool_confirmations.args"} <= before["NOTEK"]
    assert {"control_turns.text", "inbound_updates.payload", "memory_items.content"} <= before[
        "OWNSYN"
    ]
    assert {"messages.text", "contacts.display_name", "memory_items.content"} <= before["ACCTB"]
    return before


async def test_deleting_a_conversation_leaves_no_text_from_it_in_any_content_column(
    env: Env,
) -> None:
    seeded = await seed(env)
    before = await assert_seeded(env)

    async with env.factory() as session, session.begin():
        report = await deletion.delete_conversation_data(
            session,
            account_id=seeded.account_id,
            conversation_id=seeded.kamola_chat,
            actor="owner",
            clock=env.clock,
        )

    after = await all_hits(env)
    assert after["CHATK"] == set()
    # The owner's notes about Kamola (contact scope) stay; so does everything else.
    for marker in ("NOTEK", "CHATB", "NOTEB", "OWNSYN", "ACCTB"):
        assert after[marker] == kept(before, marker), marker
    assert report.messages == 3
    assert report.summaries == 2
    assert report.memory_items == 3  # the chat note and two owner items saved from it
    assert report.control_turns_cleared == 4  # three refs, and the forward from the chat
    # The chat and contact rows stay (mode, ids), content-free.
    assert await scalar(
        env, "SELECT mode FROM app.conversations WHERE id = :c", c=seeded.kamola_chat
    )
    assert (
        await scalar(
            env,
            "SELECT count(*) FROM app.messages WHERE conversation_id = :c",
            c=seeded.kamola_chat,
        )
        == 0
    )


async def test_deleting_a_contact_leaves_no_text_about_them_in_any_content_column(
    env: Env,
) -> None:
    seeded = await seed(env)
    before = await assert_seeded(env)

    async with env.factory() as session, session.begin():
        report = await deletion.delete_contact_data(
            session,
            account_id=seeded.account_id,
            contact_id=seeded.kamola,
            actor="owner",
            clock=env.clock,
        )

    after = await all_hits(env)
    assert after["CHATK"] == set()
    assert after["NOTEK"] == set()
    for marker in ("CHATB", "NOTEB", "OWNSYN", "ACCTB"):
        assert after[marker] == kept(before, marker), marker
    assert report.memory_items == 4  # notes about her and her chat, items saved from them
    assert (
        await scalar(env, "SELECT count(*) FROM app.contacts WHERE id = :c", c=seeded.kamola) == 1
    )  # the row stays, content-free (ids, bot flag)


async def test_deleting_everything_leaves_no_content_of_the_account(env: Env) -> None:
    seeded = await seed(env)
    before = await assert_seeded(env)

    async with env.factory() as session, session.begin():
        await deletion.delete_everything(
            session, account_id=seeded.account_id, actor="owner", clock=env.clock
        )

    after = await all_hits(env)
    for marker in ("CHATK", "NOTEK", "CHATB", "NOTEB", "OWNSYN"):
        assert after[marker] == set(), marker
    assert after["ACCTB"] == before["ACCTB"]
    # The account stays usable: consent, connection, settings are kept.
    assert (
        await scalar(
            env,
            "SELECT count(*) FROM app.consents WHERE account_id = :a AND revoked_at IS NULL",
            a=seeded.account_id,
        )
        == 1
    )
    assert (
        await scalar(
            env,
            "SELECT count(*) FROM app.business_connections WHERE account_id = :a",
            a=seeded.account_id,
        )
        == 1
    )
    assert (
        await scalar(env, "SELECT status FROM app.accounts WHERE id = :a", a=seeded.account_id)
        == "active"
    )
    # Held calls cannot be confirmed any more.
    async with env.factory() as session:
        statuses = {
            row.status
            for row in (
                await session.execute(
                    select(ToolConfirmation).where(ToolConfirmation.account_id == seeded.account_id)
                )
            ).scalars()
        }
    assert statuses == {"expired"}


async def test_deleting_everything_keeps_only_the_digest_automation(env: Env) -> None:
    """R11: owner-authored monitors are content and go; the digest schedule
    is a setting and stays."""
    seeded = await seed(env)
    async with env.factory() as session, session.begin():
        session.add(
            Automation(
                account_id=seeded.account_id,
                kind="digest",
                spec={"schema_version": 1},
                timezone="UTC",
                tz_policy="follow_owner",
            )
        )

    async with env.factory() as session, session.begin():
        await deletion.delete_everything(
            session, account_id=seeded.account_id, actor="owner", clock=env.clock
        )

    kinds = await scalar(
        env,
        "SELECT array_agg(kind ORDER BY kind) FROM app.automations WHERE account_id = :a",
        a=seeded.account_id,
    )
    assert kinds == ["digest"]
    other = await scalar(
        env,
        "SELECT count(*) FROM app.automations WHERE account_id = :a",
        a=seeded.other_account_id,
    )
    assert other == 1


@pytest.mark.parametrize("scope", ["conversation", "contact"])
async def test_rows_that_name_a_chat_or_contact_only_in_refs_go_with_it(
    env: Env, scope: str
) -> None:
    """Notifications and automations hold no content, so the canary scans
    cannot see them: assert the rows themselves. Each names the deleted chat
    or contact in one place only (Jack's F0 read, non-blocking 1)."""
    seeded = await seed(env)
    named = {
        "chat-refs": {"refs": {"conversation_ids": [seeded.kamola_chat]}},
        "contact-refs": {"refs": {"contact_ids": [seeded.kamola]}},
        "contact-column": {"contact_id": seeded.kamola},
        "other-chat": {
            "refs": {"conversation_ids": [seeded.bobur_chat]},
            "contact_id": seeded.bobur,
        },
    }
    async with env.factory() as session, session.begin():
        for key, fields in named.items():
            session.add(
                Notification(
                    account_id=seeded.account_id,
                    kind="monitor_match",
                    priority="normal",
                    dedupe_key=f"synthetic:{key}",
                    **fields,
                )
            )
        for key, person in (("contact-only", seeded.kamola), ("other-contact", seeded.bobur)):
            session.add(
                Automation(
                    account_id=seeded.account_id,
                    kind="monitor",
                    spec={"schema_version": 1, "label": key},
                    refs={"contact_ids": [person]},
                    timezone="UTC",
                    tz_policy="fixed",
                )
            )

    async with env.factory() as session, session.begin():
        if scope == "conversation":
            await deletion.delete_conversation_data(
                session,
                account_id=seeded.account_id,
                conversation_id=seeded.kamola_chat,
                actor="owner",
                clock=env.clock,
            )
        else:
            await deletion.delete_contact_data(
                session,
                account_id=seeded.account_id,
                contact_id=seeded.kamola,
                actor="owner",
                clock=env.clock,
            )

    left = await scalar(
        env,
        "SELECT array_agg(dedupe_key ORDER BY dedupe_key) FROM app.notifications "
        "WHERE account_id = :a AND dedupe_key IN ('synthetic:chat-refs', "
        "'synthetic:contact-refs', 'synthetic:contact-column', 'synthetic:other-chat')",
        a=seeded.account_id,
    )
    assert left == ["synthetic:other-chat"]
    labels = await scalar(
        env,
        "SELECT array_agg(spec->>'label') FROM app.automations "
        "WHERE account_id = :a AND spec->>'label' IS NOT NULL",
        a=seeded.account_id,
    )
    assert labels == ["other-contact"]
    # The other account's rows are untouched.
    assert (
        await scalar(
            env,
            "SELECT count(*) FROM app.notifications WHERE account_id = :a",
            a=seeded.other_account_id,
        )
        == 1
    )


async def test_a_new_message_after_deleting_everything_is_stored_again(env: Env) -> None:
    """Deleting is not stopping: the connection and the chat's mode stay, so
    a later message is observed as before (M03 D78)."""
    seeded = await seed(env)
    async with env.factory() as session, session.begin():
        await deletion.delete_everything(
            session, account_id=seeded.account_id, actor="owner", clock=env.clock
        )
    await deliver(env, business_message(message_id=9, message_text="CHATK later"))
    assert (
        await scalar(
            env, "SELECT text FROM app.messages WHERE conversation_id = :c", c=seeded.kamola_chat
        )
        == "CHATK later"
    )


async def test_another_accounts_ids_are_not_found_by_deletion(env: Env) -> None:
    seeded = await seed(env)
    before = await all_hits(env)
    for call in (
        lambda s: deletion.delete_conversation_data(
            s,
            account_id=seeded.other_account_id,
            conversation_id=seeded.kamola_chat,
            actor="owner",
            clock=env.clock,
        ),
        lambda s: deletion.delete_contact_data(
            s,
            account_id=seeded.other_account_id,
            contact_id=seeded.kamola,
            actor="owner",
            clock=env.clock,
        ),
    ):
        async with env.factory() as session, session.begin():
            with pytest.raises(LookupError):
                await call(session)
    assert await all_hits(env) == before


DELETION_EVENT = "data_deleted"


async def test_each_deletion_writes_exactly_one_content_free_audit_row(env: Env) -> None:
    seeded = await seed(env)
    calls: list[tuple[str, Callable[[Any], Awaitable[Any]]]] = [
        (
            "conversation",
            lambda s: deletion.delete_conversation_data(
                s,
                account_id=seeded.account_id,
                conversation_id=seeded.kamola_chat,
                actor="owner",
                clock=env.clock,
            ),
        ),
        (
            "contact",
            lambda s: deletion.delete_contact_data(
                s,
                account_id=seeded.account_id,
                contact_id=seeded.bobur,
                actor="agent",
                clock=env.clock,
            ),
        ),
        (
            "everything",
            lambda s: deletion.delete_everything(
                s, account_id=seeded.account_id, actor="owner", clock=env.clock
            ),
        ),
    ]
    for scope, call in calls:
        async with env.factory() as session:
            count_before = len((await session.execute(select(AuditLogEntry.id))).scalars().all())
        async with env.factory() as session, session.begin():
            await call(session)
        async with env.factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(AuditLogEntry).order_by(AuditLogEntry.id).offset(count_before)
                    )
                ).scalars()
            )
        # P2 cancellation transitions add their own content-free audit rows;
        # the owner's deletion itself still writes exactly one receipt row.
        assert all(r.event in {DELETION_EVENT, "action_cancelled"} for r in rows), scope
        [row] = [entry for entry in rows if entry.event == DELETION_EVENT]
        assert row.details["scope"] == scope
        dumped = json.dumps(row.details)
        for marker in MARKERS:
            assert marker not in dumped
            assert all(marker not in json.dumps(entry.details) for entry in rows)
        # Ids, counts and enums only.
        for key, value in row.details.items():
            assert isinstance(value, int | bool) or key == "scope", (key, value)


# ------------------------------------------------------------ the summary race
async def _summary_seed(env: Env) -> tuple[int, int, int]:
    from tests.integration.test_summaries import seed as summary_seed

    a, _ = await summary_seed(env)
    account_id = await scalar(
        env, "SELECT id FROM app.accounts WHERE telegram_user_id = :u", u=OWNER
    )
    contact_id = await scalar(env, "SELECT contact_id FROM app.conversations WHERE id = :c", c=a)
    return account_id, a, contact_id


@pytest.mark.parametrize("scope", ["conversation", "contact", "everything"])
async def test_a_deletion_committed_during_a_summary_call_leaves_no_summary(
    migrated_url: str, scope: str
) -> None:
    """The deletion commits while the model is answering (FakeLLM hook); the
    store re-checks its source under the §9.4 locks (S03.2) and stores
    nothing, so no deleted text lands in a summary afterwards."""
    from tests.integration.test_summaries import TEXT_A, TEXT_A2

    async with environment(migrated_url, summaries=True) as env:
        account_id, conversation, contact = await _summary_seed(env)

        async def delete_now() -> None:
            async with env.factory() as session, session.begin():
                if scope == "conversation":
                    await deletion.delete_conversation_data(
                        session,
                        account_id=account_id,
                        conversation_id=conversation,
                        actor="owner",
                        clock=env.clock,
                    )
                elif scope == "contact":
                    await deletion.delete_contact_data(
                        session,
                        account_id=account_id,
                        contact_id=contact,
                        actor="owner",
                        clock=env.clock,
                    )
                else:
                    await deletion.delete_everything(
                        session, account_id=account_id, actor="owner", clock=env.clock
                    )

        async def answer(request: Any) -> dict[str, Any]:
            await delete_now()
            return {
                "overview": f"{TEXT_A} {TEXT_A2}",
                "open_questions_to_owner": [],
                "commitments": [],
                "decisions_requested": [],
                "key_facts": [],
                "needs_reply": False,
                "needs_reply_reason": None,
                "language": "uz",
            }

        fake = FakeLLM()
        fake.parsed = [answer]
        outcome = await summarizer(env, fake).refresh(conversation)

        assert outcome.status is SummaryStatus.DISCARDED
        async with env.factory() as session:
            stored = (
                (
                    await session.execute(
                        text("SELECT summary::text FROM app.conversation_summaries")
                    )
                )
                .scalars()
                .all()
            )
        assert all("SYNTH-A" not in row for row in stored)
        assert (
            await scalar(
                env,
                "SELECT count(*) FROM app.conversation_summaries WHERE conversation_id = :c",
                c=conversation,
            )
            == 0
        )
