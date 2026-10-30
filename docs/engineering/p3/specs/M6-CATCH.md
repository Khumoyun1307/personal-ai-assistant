# M6-CATCH — End-of-delegation catch-up: builder, requester and renderer (junior unit)

| | |
|---|---|
| **Unit** | M6-CATCH — the deterministic `delegation_catch_up` (FR-AUT-09, J5 step 6): what came in, what was answered automatically (each reply linked to `/activity`, failed and uncertain included), what is waiting for the owner, which decisions are needed, and what may be missing; produced even with zero activity; nothing from other chats or other delegations; no model call (OD-11) |
| **Owner slot** | D1 (David's team, junior) |
| **Mentor** | J1 (Claude; the peer review, dispatched by the TL) |
| **QA** | QD1, **before merge**, for both PRs (depth: decoys excluded; uncertain never shown as sent; produced with zero activity; window boundaries) |
| **Size** | S+ (≈ 0.75 engineer-day, one fix round included), delivered as **two PRs** (A: builder and requester; B: renderer and wiring) |
| **Depends on** | F0-S (`delegations`, `delegation_conversations`, `actions.delegation_id`, `NotificationKind.DELEGATION_CATCH_UP`, `NotificationRefs.delegation_ids`, the delivery-class table, `end_delegations`, `p3_factories`); F0-R (`core/p3_ports.py::CatchUpRequester`, `P3Ports`, `_wire_surfaces`, `P3WireContext`). Part B also uses F0-S's fixed Pause button `core/autonomy_texts.py::pause_button()` (TL ruling R45, which settled M6-PAUSE's gap G1; §6.2 B3). Nothing from M6-ESC, M6-DELEG or M6-LIFE is imported: tests seed their rows directly |
| **Consumed by** | M6-DELEG (`owner_ended`, `superseded`) and M6-LIFE (`time_elapsed`, `consent_revoked`, `connection_disabled`) call `CatchUpRequester.request` through `P3Ports.catch_up_requester` after `end_delegations`; QA-S6 (J5 catch-up scenario); LIVE-P3 (J5 live, "catch-up delivered") |
| **Branch → base** | Part A: `p3/m6-catch` from `origin/int/p3` (after CP1) → PR to `int/p3`. Part B: `p3/m6-catch-render` from `origin/p3/m6-catch` → PR to `p3/m6-catch` while Part A is open; after Part A merges, `git rebase origin/int/p3` and `gh pr edit <n> --base int/p3` (WORKFLOW §4.2) |
| **Normative inputs** | [P3-interfaces §4.5](../contracts/P3-interfaces.md) (`CatchUpRequester`, `P3Ports`), §6.1 (normal ends request the catch-up; deletion and restore never do), §11.1–§11.3 (`delegation_catch_up`: held_in_quiet, priority high, dedupe `catchup:{delegation_id}`; renderers; escape once), §12.5 (Pause button on the catch-up), §13 (audit `delegation_catch_up_rendered`), §15.3 (`_wire_surfaces`); [P3-schema §2.1, §2.2, §3.1, §7.1, §7.2 step 6](../contracts/P3-schema.md); [specs/M6-ESC.md §3.3 step 5](M6-ESC.md) (batched alerts with reason `delegation_catch_up`); [specs/GW-ESC.md §3.3](GW-ESC.md) (escape-once rule); IMPLEMENTATION_PLAN S06.4 tests 1–3; PRD FR-AUT-09, J5; decisions OD-11, P3-T29, P3-T52 (R15); AGENTS §6 invariants 4, 6, 9, 11, 12; AGENTS §7 ("surface coverage gaps") |

## 1. Goal

When a delegation ends, the owner gets one control-chat message that says, truthfully:
- **what came in**: every contact message in the delegated chats during the delegation, counted per chat with its first and last time;
- **what was answered automatically**: each automatic reply with its action id ("reply #345"; `get_activity` returns the same `action_id`, and `/activity` lists the reply by time and chat name with its delegation, STAT-ACT Part A), grouped exactly as sent, delivery uncertain, not sent or still being sent. An automatic action that was never sent (cancelled or expired) is not a reply and is not counted here (`/activity` lists it as not sent);
- **what is waiting for you**: the delegation's held replies (OD-11: the autonomy-fallback `agent_proposal` rows, announced by `autonomy_not_sent` alerts) that wait for approval or are ready to copy, whether or not the owner has opened their cards yet;
- **decisions needed**: the alerts M6-ESC held back for this catch-up;
- **what may be missing**: the coverage caveats of the window (gaps, an inactive connection, deleted messages).

It is built from stored rows with no model call (OD-11), so it works with the AI down or out of budget and needs no consent. It is delivered as a `delegation_catch_up` notification, so the existing at-most-once notification claim delivers it; you write no claim code. It never shows anything from a chat the delegation did not cover, or an automatic reply or card of another delegation (RG-09).

## 2. Read first

1. [handoffs/junior-engineers.md](../handoffs/junior-engineers.md), all of it.
2. [WORKFLOW](../WORKFLOW.md) §2, §3, §4 (stacking and the PR template), §5, §6.2, §6.5 ("escape once", "wire functions", shared lists).
3. `src/assistant/inbox/notifications.py`: `enqueue` (its `dedupe_key`, `refs`, `clock`, `scheduler` arguments; the after-commit defer) and `NotificationDelivery.deliver` (the renderer runs inside the claim transaction, after the delivery decision; `None` from a renderer suppresses the row).
4. `src/assistant/inbox/alert_texts.py`: `RenderedAlert`, `Renderer`, `UNNAMED`, `render_triage` (the house style: `html.escape`, `<b>` headlines, `None` when the source is gone).
5. `src/assistant/memory/catch_up.py`: `period_coverage` and `caveats` (you call them; never edit the file: M7-TASK owns its one P3 change).
6. `src/assistant/memory/summaries.py::preferred_name`.
7. After F0-S/F0-R: `src/assistant/automation/delegation_models.py` (`Delegation`, `DelegationConversation`, the status and end-reason constants), `src/assistant/automation/delegations_repo.py::end_delegations` (what an end writes: `status`, `ended_at`, `ended_reason`, links' `ended_at`), `src/assistant/core/p3_ports.py` (`CatchUpRequester`, `P3Ports`), `src/assistant/inbox/notification_types.py` (`DELEGATION_CATCH_UP`, `delegation_ids`, `DELIVERY_CLASSES`), `tests/integration/p3_factories.py`, and the `_wire_surfaces` function in `src/assistant/bootstrap.py`.
8. [specs/M6-ESC.md](M6-ESC.md) §3.3 (which alerts are batched for the catch-up) and [specs/GW-ESC.md](GW-ESC.md) §3.3 (the escape-once rule and its test).
9. This spec, to the end, before you start.

## 3. Allowed files

**Part A (`p3/m6-catch`):**

| Path | New or existing | Exactly what may change |
|---|---|---|
| `src/assistant/memory/delegation_catch_up.py` | new | The whole module (§6.1) |
| `tests/integration/test_delegation_catch_up.py` | new | §7.1 |
| `tests/integration/test_log_hygiene_m6_catch.py` | new | §7.3 (first test), copied from F0-E's template `tests/unit/test_log_hygiene_f0e.py` |

**Part B (`p3/m6-catch-render`):**

| Path | New or existing | Exactly what may change |
|---|---|---|
| `src/assistant/inbox/catch_up_texts.py` | new | The whole module (§6.2) |
| `src/assistant/bootstrap.py` | existing | Lines inside `_wire_surfaces` only (§6.2 B4) |
| `docs/ARCHITECTURE.md` | existing | §17: one new bullet after "Owner alerts" (§6.2 B5). Nothing else in the file |
| `tests/unit/test_catch_up_texts.py` | new | §7.2 (pure rendering) |
| `tests/integration/test_catch_up_renderer.py` | new | §7.2 (renderer, delivery, wiring) |
| `tests/integration/test_log_hygiene_m6_catch.py` | yours (Part A) | §7.3 second test appended |

Nothing else, in either PR, except the shared-list edit of §5.

## 4. Forbidden

- `src/assistant/policy/`, `src/assistant/actions/` (read `Action` and `actions.states` only), `src/assistant/telegram/` (including `gateway.py`, `render.py`, `ingress/`, `control/`), `migrations/`, `src/assistant/agent/` (including `prompts/`, `prompt_registry.py`), `src/assistant/privacy/`, consent code (`src/assistant/llm/consent.py`), `src/assistant/config.py`, `src/assistant/core/` (read `core/p3_ports.py`, `core/autonomy_texts.py`, `core/clock.py`, `core/jobs.py` only), `src/assistant/jobs/`.
- `src/assistant/inbox/notification_policy.py`, `inbox/notifications.py`, `inbox/triage_store.py`, `inbox/alert_texts.py`, `inbox/notification_types.py` (call and import only).
- `src/assistant/memory/catch_up.py` (M7-TASK's one P3 change) and every other existing `memory/` file; `src/assistant/automation/*` (read only).
- `bootstrap.py` outside `_wire_surfaces`, and its module import block.
- Imports in your two modules from `assistant.telegram`, `assistant.agent`, `assistant.llm`, `assistant.jobs` (layering: `memory` and `inbox` are domain packages). No model, no Telegram call, no claim code, no job.
- `AGENTS.md`, every `docs/` file other than the one ARCHITECTURE bullet, other units' files (M6-DELEG, M6-LIFE, M6-ESC, QA-S6 scenarios), `pyproject.toml`, `uv.lock`, `.env*`, `var/`.
- **Every file not in §3.**

## 5. Shared lists you must update

- **Part A:** none.
- **Part B:** if F0-R's `tests/integration/test_p3_wiring.py::test_empty_wire_functions_change_nothing` (or another F0-R test) asserts that a process with all P3 flags off registers **no renderer** or **no `catch_up_requester`**, append the `delegation_catch_up` renderer and the requester to that test's expected set and change nothing else in it (WORKFLOW §6.5, append only), in a **separate last commit**, and name it in the PR. The catch-up is wired whatever the flags on purpose: ending a delegation is never flag-gated (P3-interfaces §4.5), so its catch-up must never be lost. If any other exact-set test fails, stop and ask (§10).

## 6. Step-by-step

All commands run in your directory with `$env:COMPOSE_PROJECT_NAME = 'd1'` set (they run inside the container). `…` stands for `docker compose -f compose.dev.yml run --rm app`. If your session cannot run Docker, write the tests, push, let CI run them, and say so in the PR.

### 6.1 Part A — the builder and the requester (`src/assistant/memory/delegation_catch_up.py`)

**A1. The exact API.**

```python
"""End-of-delegation catch-up: the deterministic builder and the requester
(M6-CATCH; FR-AUT-09, J5 step 6; P3-interfaces §4.5, §11.2; OD-11).

No model call: the catch-up is built from stored messages, actions,
notifications and the coverage facts of the delegation's window, when the
notification is delivered. Every query is account-scoped and limited to this
delegation's chats and this delegation's own actions (RG-09, RG-21). The
requester enqueues one content-free `delegation_catch_up` notification per
delegation; the existing notification claim delivers it at most once."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions.models import Action
from assistant.automation.delegation_models import Delegation, DelegationConversation
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.clock import Clock
from assistant.core.jobs import JobScheduler
from assistant.inbox.models import Notification
from assistant.inbox.notification_types import NotificationKind, NotificationRefs, Priority
from assistant.inbox.notifications import enqueue
from assistant.memory import catch_up
from assistant.memory.summaries import preferred_name
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

CATCH_UP_PRIORITY: Final = Priority.HIGH
#: `notifications.reason_code` of an alert M6-ESC held back for the catch-up.
CATCH_UP_REASON: Final = "delegation_catch_up"
#: Automatic-reply states the catch-up reports, and how (never folded together).
REPLY_GROUPS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "succeeded": "sent",
        "delivery_uncertain": "uncertain",
        "failed": "failed",
        "executing": "in_flight",
    }
)
#: Held-reply states that still need the owner (OD-11: shown or not yet shown).
CARD_STATES: Final = ("awaiting_approval", "revising", "blocked")


def catch_up_dedupe_key(delegation_id: int) -> str: ...  # f"catchup:{delegation_id}"


@dataclass(frozen=True, slots=True)
class CatchUpChat:
    conversation_id: int
    contact_id: int
    name: str | None  # preferred_name, raw: contact-chosen, the renderer escapes it once
    window_start: datetime
    window_end: datetime
    message_ids: tuple[
        int, ...
    ]  # every contact message in [window_start, window_end), oldest first
    first_message_at: datetime | None
    last_message_at: datetime | None


@dataclass(frozen=True, slots=True)
class CatchUpReply:
    action_id: int
    conversation_id: int
    group: str  # a REPLY_GROUPS value
    at: datetime  # executed_at, else attempt_started_at, else updated_at


@dataclass(frozen=True, slots=True)
class CatchUpCard:
    action_id: int
    conversation_id: int
    state: str  # a CARD_STATES value


@dataclass(frozen=True, slots=True)
class CatchUpDecision:
    notification_id: int
    conversation_id: int
    kind: str
    priority: str


@dataclass(frozen=True, slots=True)
class CatchUp:
    delegation_id: int
    kind: str  # "time_bounded" | "standing"
    status: str  # "ended" | "cancelled"
    ended_reason: str | None
    starts_at: datetime
    ended_at: datetime
    chats: tuple[CatchUpChat, ...]
    replies: tuple[CatchUpReply, ...]
    cards: tuple[CatchUpCard, ...]
    decisions: tuple[CatchUpDecision, ...]
    caveats: tuple[str, ...]

    @property
    def message_count(self) -> int: ...
    def counts(self) -> dict[str, int]: ...


async def build_catch_up(
    session: AsyncSession, *, account_id: int, delegation_id: int, timezone: str
) -> CatchUp | None: ...


class DelegationCatchUpRequester:
    """`CatchUpRequester` (core/p3_ports.py); wired in `_wire_surfaces`."""

    def __init__(self, *, clock: Clock, scheduler: JobScheduler | None) -> None: ...

    async def request(
        self, session: AsyncSession, *, account_id: int, delegation_id: int, now: datetime
    ) -> None: ...
```

These names are binding (Part B, the wiring, M6-DELEG and M6-LIFE through the port, and QA use them).

**A2. Rules for `build_catch_up`** (each tested in §7.1), all in the caller's session, no lock, no write:
1. **The delegation.** `select(Delegation).where(Delegation.id == delegation_id, Delegation.account_id == account_id)`. No row ⇒ return `None`. `status == "active"` ⇒ log `catch_up_delegation_still_active` (ids only) and return `None` (a catch-up describes an ended delegation; the requester never asks for an active one). `ended_at is None` ⇒ return `None` (impossible under `ck_delegations_end_fields`; fail closed). The `spec` is **not** read: a deletion may have scrubbed it, and the catch-up needs only the window and the links. Do not call `delegations_repo.get` or `shareable_for` either (`get` returns `None` for a scrubbed or invalid spec).
2. **The chats.** Every `DelegationConversation` row of this delegation and account, live or ended, ordered by `conversation_id`. For each: `window_start = delegation.starts_at`; `window_end = min(link.ended_at, delegation.ended_at)` when `link.ended_at` is set, else `delegation.ended_at` (a chat handed over to a newer delegation by supersede counts only until the handover). Names: one query joining `Conversation` and `Contact`, both filtered by `account_id`, for these conversation ids; `name = preferred_name(display_name, username)`. A chat whose conversation is not found is left out.
3. **Messages.** For each chat with `window_end > window_start`: `select(Message.id, Message.sent_at)` where `Message.account_id == account_id`, `Message.conversation_id == <chat>`, `Message.direction == "contact"`, `Message.sent_at >= window_start`, `Message.sent_at < window_end`, ordered by `sent_at, id`. The start is included, the end excluded. Deleted, expired and not-stored rows that kept their `direction` and `sent_at` are counted (they came in; the caveats say what is missing); a deletion tombstone with no direction or date cannot be counted and is covered by the caveats alone. Otherwise `message_ids = ()` and both times `None`.
4. **Replies.** `Action` rows with `account_id`, `delegation_id == delegation_id`, `origin == "delegation_auto"`, `state` in `REPLY_GROUPS`; `group = REPLY_GROUPS[state]`; `at = executed_at or attempt_started_at or updated_at`; ordered by `(at, action_id)`. Cancelled and expired automatic actions are not replies (never sent; `/activity` lists them as not sent), and neither is an owner-approved held reply that was sent (origin `agent_proposal`: the owner sent it). At the end of a delegation `end_delegations` has already cancelled its pending autonomous actions, so `executing` is the only non-final state left.
5. **Cards.** `Action` rows with `account_id`, `delegation_id == delegation_id`, `origin == "agent_proposal"`, `state` in `CARD_STATES`, ordered by `created_at, id`.
6. **Decisions.** `Notification` rows with `account_id`, `status == "batched"`, `reason_code == CATCH_UP_REASON`, `conversation_id` in the chats' ids; keep a row only when `created_at` is inside that chat's `[window_start, window_end)`, and, for kind `delegation_escalation`, only when `refs["delegation_ids"]` is missing or is a list containing `delegation_id` (an escalation of another delegation is never this one's). Ordered by `created_at, id`.
7. **Caveats.** When there is at least one chat and `delegation.ended_at > delegation.starts_at`: `coverage = await catch_up.period_coverage(session, account_id, start=delegation.starts_at, end=delegation.ended_at, conversation_ids=[chat ids])` and `caveats = tuple(catch_up.caveats(coverage, timezone))`; otherwise `()`.
8. Return the `CatchUp`. `message_count` = the sum of `len(chat.message_ids)`. `counts()` returns exactly `{"messages": message_count, "sent": <replies in "sent">, "failed": <in "failed">, "uncertain": <in "uncertain"> + <in "in_flight">, "pending_cards": len(cards), "decisions": len(decisions)}` (the audit `details` of P3-interfaces §13: an in-flight outcome is not known, so it counts as uncertain, never as sent).
9. One log line `catch_up_built` with `account_id`, `delegation_id` and the six counts. Nothing else is logged.

**A3. Rules for `DelegationCatchUpRequester.request`** (in the caller's transaction; no commit, no Telegram, no model):
1. `status = select(Delegation.status)` for this id **and** `account_id`. No row ⇒ log `catch_up_delegation_not_found` (ids only), return (a foreign id enqueues nothing, RG-21). `status == "active"` ⇒ log `catch_up_delegation_still_active`, return.
2. Otherwise `notification_id = await enqueue(session, account_id=account_id, kind=NotificationKind.DELEGATION_CATCH_UP, priority=CATCH_UP_PRIORITY, dedupe_key=catch_up_dedupe_key(delegation_id), refs=NotificationRefs(delegation_ids=(delegation_id,)), clock=self._clock, scheduler=self._scheduler)`: standard alert class (the delivery class `held_in_quiet` comes from F0-S's table by kind), no `conversation_id`, no `contact_id`. A second request for the same delegation returns `None` from `enqueue` (dedupe) and adds nothing. With a scheduler, delivery is deferred after commit; without one, M05's notification reconciler delivers it within about a minute.
3. Log `catch_up_requested` with `account_id`, `delegation_id`, `queued` (`notification_id is not None`).
4. `now` is part of the port's signature; the row's `created_at` comes from the requester's clock (the same clock in production).

`refs` holds only `delegation_ids`: P3-schema §7.2 step 6 deletes a queued catch-up whose delegation a scoped deletion names, and the managed and "everything" scopes delete every notification already.

**A4. Steps (each ends with a check).**
1. **Branch.** `git fetch origin`; `git switch -c p3/m6-catch origin/int/p3`. **Check:** `… uv run python -c "from assistant.automation.delegation_models import Delegation, DelegationConversation; from assistant.core.p3_ports import CatchUpRequester; from assistant.inbox.notification_types import NotificationKind; print(NotificationKind.DELEGATION_CATCH_UP.value)"` prints `delegation_catch_up`. If not, F0 is not on your base: stop and ask.
2. **Red tests.** Create `tests/integration/test_delegation_catch_up.py` with every test of §7.1, and the module with the API of A1 and each function or method body `raise NotImplementedError`. **Check:** `… uv run pytest tests/integration/test_delegation_catch_up.py -q` → every test fails with `NotImplementedError` or an assertion, none with `ImportError`.
3. **Rules A2.1–A2.3** (delegation, chats, messages). **Check:** `… -k "lists_every or zero_activity or handed or cancelled or active_or_missing"` → pass.
4. **A2.4–A2.6** (replies, cards, decisions). **Check:** `… -k "links_sent or cards or decisions or rg09 or never_reads"` → pass.
5. **A2.7–A2.9** (caveats, counts, log). **Check:** `… -k "caveats or links_sent or zero_activity"` → pass.
6. **A3** (requester). **Check:** `… -k "requester"` → pass; then the whole file passes.
7. **Hygiene** (§7.3 first test). **Check:** `… uv run pytest tests/integration/test_log_hygiene_m6_catch.py -q` → pass.
8. **Static checks.** `… uv run ruff check src/assistant/memory/delegation_catch_up.py tests/integration/test_delegation_catch_up.py tests/integration/test_log_hygiene_m6_catch.py`; the same with `uv run ruff format --check`; `… uv run mypy src/assistant/memory/delegation_catch_up.py`; `… uv run lint-imports`. **Check:** all clean.
9. **Diff.** `git diff --stat origin/int/p3...HEAD` → exactly the three Part A files.
10. **Commit and PR** (§12): one commit with the red tests, one with the green code.

### 6.2 Part B — the renderer and the wiring (`src/assistant/inbox/catch_up_texts.py`)

**B1. The exact API.**

```python
"""The `delegation_catch_up` renderer (M6-CATCH; FR-AUT-09; P3-interfaces §11, §12.5, §13).

Runs inside the notification delivery's claim transaction and reads the
catch-up when it is delivered (a card approved meanwhile is no longer listed
as waiting). Returns None when the delegation is gone, so the alert is
suppressed rather than sent about deleted data. Escape once (GW-ESC §3.3):
each contact name and caveat through html.escape(value, quote=False), the
application's own <b> markup raw. English (P2-O11). Wording is truthful
(RG-20): "may or may not have arrived" and "still being sent" are never
"sent"."""

EVENT_CATCH_UP_RENDERED: Final = "delegation_catch_up_rendered"
MAX_CHAT_LINES: Final = 15
MAX_REPLY_LINES: Final = 20
MAX_CARD_LINES: Final = 10
MAX_DECISION_LINES: Final = 10
#: Space, EN DASH (U+2013), space between two times. Written as an escape: ruff
#: RUF001 flags the literal character in a string, and no suppression is allowed.
RANGE_SEPARATOR: Final = " – "
ENDED_REASON_LINES: Final[Mapping[str, str]] = MappingProxyType({...})  # B2
DECISION_TEXTS: Final[Mapping[str, str]] = MappingProxyType({...})  # B2


def render_catch_up(catch_up: CatchUp, *, timezone: str) -> str: ...  # pure


class CatchUpRenderer:
    """A `Renderer` (inbox/alert_texts.py) for `delegation_catch_up`."""

    def __init__(self, *, clock: Clock) -> None: ...

    async def __call__(
        self, session: AsyncSession, notification: Notification, account: Account
    ) -> RenderedAlert | None: ...
```

Imports: the standard library (`html`, `datetime`, `types`, `typing`, `zoneinfo`), `sqlalchemy.ext.asyncio.AsyncSession`, `assistant.accounts.models` (`Account`, `AuditLogEntry`), `assistant.core.clock.Clock`, `assistant.core.autonomy_texts.pause_button` (B3), `assistant.inbox.alert_texts` (`UNNAMED`, `RenderedAlert`), `assistant.inbox.models.Notification`, `assistant.memory.delegation_catch_up` (`CatchUp`, `build_catch_up`), `assistant.observability.logging`. Nothing from `assistant.telegram`.

**B2. `render_catch_up` — the exact text.** Lines joined with `"\n"`. Helpers: `_local(moment, timezone, *, full: bool) -> str` formats `moment.astimezone(zone)` as `"%Y-%m-%d %H:%M"` when `full`, else `"%H:%M"`, where `zone` is `ZoneInfo(timezone)` or `ZoneInfo("UTC")` when that raises `ZoneInfoNotFoundError` or `ValueError` (catch exactly those two). `same_day` = the local dates of `starts_at` and `ended_at` are equal; `time(m)` below means `_local(m, timezone, full=not same_day)`. `name(conversation_id)` = `html.escape(chat.name or UNNAMED, quote=False)` for that chat, or `html.escape(UNNAMED, quote=False)` when the id is not among the chats.

`ENDED_REASON_LINES`: `time_elapsed` → "It ended at its end time."; `owner_ended` → "You ended it."; `superseded` → "A newer delegation took over its chats."; `consent_revoked` → "It ended because AI processing for your chats was switched off."; `connection_disabled` → "It ended because the business connection was switched off."; `data_deleted` → "It ended because chat data was deleted."; `restored_from_backup` → "It ended because the assistant's data was restored from a backup." An unknown or `None` reason adds no line.

`DECISION_TEXTS`: `triage_urgent` → "something urgent"; `triage_decision` → "needs a decision from you"; `triage_needs_reply` → "is waiting for your reply"; `triage_suspicious` → "sent something that looked like an attempt to instruct the assistant; nothing was done"; `delegation_escalation` → "needs you". A `delegation_escalation` with priority `urgent` uses "needs your decision"; any other kind uses "needs your attention".

**Cancelled** (`status == "cancelled"`), the whole text (the reason line is left out, not blank, when there is none):
```
🧾 <b>Delegation #{id} was cancelled before it started</b>
It was due to start at {_local(starts_at, full=True)} ({timezone}).
{reason line, if any}
Nothing was handled for you, and nothing was sent.
```

**Ended**, in this order (an empty string means a blank line):
1. `🧾 <b>Catch-up: delegation #{id} has ended</b>` (for `kind == "standing"`: `🧾 <b>Catch-up: standing delegation #{id} has ended</b>`).
2. `{_local(starts_at, full=True)} → {_local(ended_at, full=True)} ({timezone})`.
3. The reason line, if any.
4. `""`, `<b>What came in</b>`, `{message_count} message(s) from {len(chats)} chat(s).`, then per chat (first `MAX_CHAT_LINES`): no messages → `• {name}: no messages`; one → `• {name}: 1 message at {time(first)}`; more → `• {name}: {n} messages, {time(first)}{RANGE_SEPARATOR}{time(last)}` (the dash is U+2013, shown below as "–"); then, if more chats, `…and {k} more chat(s).`
5. `""`, `<b>Answered automatically</b>`, `Sent: {s} · Delivery uncertain: {u} · Not sent: {f}` plus ` · Still being sent: {i}` when `i > 0` (true totals, groups never folded). No replies → `No automatic replies.` Otherwise per reply (first `MAX_REPLY_LINES`): `sent` → `• {name}: sent at {time(at)} (reply #{action_id})`; `uncertain` → `• {name}: may or may not have arrived — check the chat (reply #{action_id})`; `failed` → `• {name}: not sent (reply #{action_id})`; `in_flight` → `• {name}: still being sent; the outcome is not known yet (reply #{action_id})`; then `…and {k} more.` if cut; then `Each reply is in /activity with this delegation.`
6. `""`, `<b>Waiting for you</b>`. None → `Nothing is waiting for your approval.` Otherwise per card (first `MAX_CARD_LINES`): `awaiting_approval` or `revising` → `• {name}: a reply waits for your approval`; `blocked` → `• {name}: a draft is ready for you to copy; it cannot be sent from here`; then `…and {k} more.` if cut.
7. `""`, `<b>Decisions needed</b>`. None → `No decisions are waiting.` Otherwise per decision (first `MAX_DECISION_LINES`): `• {name}: {text}`; then `…and {k} more.` if cut.
8. Only when there are caveats: `""`, `<b>What may be missing</b>`, then `• {html.escape(caveat, quote=False)}` for each.

**B3. `CatchUpRenderer.__call__`.**
1. `delegation_id` = the first element of `notification.refs.get("delegation_ids")` when that is a non-empty list whose first element is an `int` (not a `bool`); else return `None`.
2. `catch_up = await build_catch_up(session, account_id=account.id, delegation_id=delegation_id, timezone=account.timezone)`; `None` ⇒ return `None` (suppressed: the delegation is gone, still active, or another account's).
3. `text = render_catch_up(catch_up, timezone=account.timezone)`.
4. `session.add(AuditLogEntry(account_id=account.id, at=self._clock.now(), actor="system", event=EVENT_CATCH_UP_RENDERED, subject_type="delegation", subject_id=catch_up.delegation_id, details=catch_up.counts()))` — counts only. It commits with the delivery claim; a retry after a definite send failure renders again and writes another row (the row records a render; the notification row records the delivery).
5. Return `RenderedAlert(text, conversation_id=None, extra_buttons=(pause_button(),))`. `conversation_id=None` because a catch-up spans several chats (no "Open chat" card is attached, so no Useful or Not useful either; and the delivery table's `feedback_buttons` is false for `delegation_catch_up`, R82, so the owner-created catch-up could never be muted by feedback even if a chat id were added later). **Pause button (TL ruling R45, gap G1):** F0-S provides the pure helper `core/autonomy_texts.py::pause_button() -> Button` (and the constant `PAUSE_CALLBACK_DATA`); every renderer uses it and M6-PAUSE handles the press. Only if your base still has no `pause_button` there (F0-S did not deliver it), do not create one: leave the import out of the module, return `extra_buttons=()`, mark `test_the_catch_up_carries_the_pause_button` `@pytest.mark.xfail(strict=True, reason="awaiting G1")` (import `pause_button` inside that test's body, so the file still collects), and say so in the PR; the TL assigns the two-line follow-up.

**B4. Wiring (`_wire_surfaces` in `bootstrap.py`; function-local imports only).** Your block, placed before the function's final `return`:

```python
# --- M6-CATCH: end-of-delegation catch-up (P3-interfaces §4.5, §11.2) ---
from assistant.inbox.catch_up_texts import CatchUpRenderer
from assistant.inbox.notification_types import NotificationKind
from assistant.memory.delegation_catch_up import DelegationCatchUpRequester

ctx.notification_delivery.register_renderer(
    NotificationKind.DELEGATION_CATCH_UP, CatchUpRenderer(clock=ctx.clock)
)
wiring = replace(
    wiring,
    ports=replace(
        wiring.ports,
        catch_up_requester=DelegationCatchUpRequester(clock=ctx.clock, scheduler=ctx.scheduler),
    ),
)
```

David's units share this function and use **one convention**, the one in STAT-ACT's and M7-MRV's specs: a local `wiring` value that each block extends with `dataclasses.replace`, returned at the end. Look at the function on your base first:
- It still ends with `return P3Wiring()`: change that to `wiring = P3Wiring()` at the top of the body (after the docstring), put your block after it, and end the function with `return wiring`.
- Another David unit already did that: add only your block just before `return wiring`.
- It builds its return value another way (for example `return P3Wiring(routes=ControlRoutes(...))` with local accumulators, SET-P3's shape): keep every statement and keyword already there, add your imports and the `register_renderer` call before the `return`, and add exactly **one keyword line** to the returned `P3Wiring(...)`: `ports=P3Ports(catch_up_requester=DelegationCatchUpRequester(clock=ctx.clock, scheduler=ctx.scheduler)),` (with a function-local `from assistant.core.p3_ports import P3Ports`). Never reorder, rename or reformat another unit's lines; if a rebase conflicts, keep both sides. If `ports=` is already set there, stop and ask (§10).
- Use `replace` as the module already imports it; if it does not, add `from dataclasses import replace` as a function-local import. Never touch another unit's block.

A `P3Ports` field set by two fragments raises at startup, so only `catch_up_requester` may be set here.

Wired whatever the flags (§5). The renderer and the requester are wired together in this PR, never one without the other: a requester without a renderer would enqueue catch-ups that delivery suppresses.

**B5. ARCHITECTURE §17**, a new bullet after "Owner alerts": "**Delegation catch-up (M6-CATCH, FR-AUT-09, OD-11):** after a normal end (`owner_ended`, `superseded`, `time_elapsed`, `consent_revoked`, `connection_disabled`) the ender calls `P3Ports.catch_up_requester`, which enqueues one content-free `delegation_catch_up` notification (`refs.delegation_ids`, dedupe `catchup:{delegation_id}`, priority high, delivery class held_in_quiet); deletion and restore never request one. `inbox/catch_up_texts.py::CatchUpRenderer` builds it at delivery with `memory/delegation_catch_up.py::build_catch_up`, without a model: every contact message in each delegated chat's window (`[starts_at, min(link end, ended_at))`), the delegation's automatic replies by group (sent, delivery uncertain, not sent, still being sent; each with its action id for `/activity`), its waiting held replies, the alerts M6-ESC batched with reason `delegation_catch_up`, and the window's coverage caveats; nothing from other chats or delegations. It is delivered at most once by the notification claim, carries the Pause button, and writes the audit row `delegation_catch_up_rendered` (counts only)."

**B6. Steps (each ends with a check).**
1. **Branch.** `git switch -c p3/m6-catch-render origin/p3/m6-catch` (or from `origin/int/p3` if Part A has merged). **Check:** `… uv run pytest tests/integration/test_delegation_catch_up.py -q` → pass.
2. **Red tests.** `tests/unit/test_catch_up_texts.py` and `tests/integration/test_catch_up_renderer.py` with every test of §7.2; the module with the API of B1 and bodies `raise NotImplementedError`. **Check:** `… uv run pytest tests/unit/test_catch_up_texts.py tests/integration/test_catch_up_renderer.py -q` → the tests fail, none with `ImportError`.
3. **B2.** **Check:** `… uv run pytest tests/unit/test_catch_up_texts.py -q` → pass.
4. **B3.** **Check:** `… uv run pytest tests/integration/test_catch_up_renderer.py -q -k "not wire"` → pass.
5. **B4, B5.** **Check:** `… -k "wire"` → pass; F0-R's `… uv run pytest tests/integration/test_p3_wiring.py -q` → pass (or §5's one append).
6. **Hygiene** (§7.3 second test). **Check:** `… uv run pytest tests/integration/test_log_hygiene_m6_catch.py -q` → pass.
7. **Static checks** on `src/assistant/inbox/catch_up_texts.py`, `src/assistant/bootstrap.py` and the test files (ruff, format, mypy, lint-imports). **Check:** all clean.
8. **Diff.** `git diff --stat origin/int/p3...HEAD` (after Part A merged; before that `origin/p3/m6-catch...HEAD`) → exactly the Part B files of §3 (plus §5's separate commit, if any).

## 7. Named tests (red first)

Synthetic data only. Shared values: `T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)` (14:00 in Asia/Tashkent, UTC+5) and `END = T0 + timedelta(hours=5)` (19:00 local). Accounts: `seed_account(..., timezone="Asia/Tashkent")`. A delegation: `p3_factories.seed_delegation(session, account_id=…, conversation_ids=[…], kind="time_bounded", status="active", starts_at=T0, ends_at=END, confirmed_at=T0, spec=<valid DelegationRules dump>)`, ended with `end_delegations(session, account_id=…, delegation_ids=[d.id], reason="time_elapsed", now=END)` (F0-S's real end: it sets `ended_at` on the delegation and its links). Messages: a test helper `_contact_message(session, *, account_id, conversation_id, sent_at, direction="contact") -> int` adding `Message(account_id=…, conversation_id=…, direction=direction, sent_at=sent_at, content_type="text", text="Synthetic message")` and returning its id. Automatic replies: `p3_factories.seed_autonomous_action(...)`; held replies: `p2_factories.seed_action(..., origin="agent_proposal", state=…, delegation_id=d.id)`; held alerts: `p2_factories.seed_notification(..., kind=…, status="batched", reason_code="delegation_catch_up", created_at=…)`. The canary is `CANARY-SYNTH-P3`.

### 7.1 `tests/integration/test_delegation_catch_up.py` (Part A; `db_session` for the builder, `env` for the requester)

- `test_s06_4_the_catch_up_lists_every_in_scope_message_in_the_delegation_window` (S06.4 #1) — delegation over chats C1 and C2; contact messages in C1 at `T0` (included), `T0 + 1 h`, `END - 1 s` (included), `END` (excluded), `T0 - 1 s` (excluded); an `owner_manual` message in C1 at `T0 + 1 h` (excluded); a contact message in C2 at `T0 + 2 h` → `chats[0].message_ids == (m_T0, m_1h, m_end_minus_1s)`, `chats[0].first_message_at == T0`, `chats[0].last_message_at == END - 1 s`, `chats[1].message_ids == (m_c2,)`, `message_count == 4`, every chat's `window_start == T0` and `window_end == END`.
- `test_s06_4_the_catch_up_links_sent_items_to_actions_including_failed_and_uncertain` (S06.4 #2) — automatic replies of this delegation: `succeeded` (`executed_at = T0 + 10 min`), `delivery_uncertain` (`attempt_started_at = T0 + 20 min`), `failed` (`attempt_started_at = T0 + 30 min`), each in C1, and `executing` (`attempt_started_at = T0 + 40 min`) in C2; plus a `cancelled` one in C2, an `expired` one in C1 and an owner-approved held reply of this delegation (`p2_factories.seed_action(..., origin="agent_proposal", state="succeeded", delegation_id=d.id)`) in C1 → `replies == (CatchUpReply(id_s, C1, "sent", T0 + 10 min), CatchUpReply(id_u, C1, "uncertain", T0 + 20 min), CatchUpReply(id_f, C1, "failed", T0 + 30 min), CatchUpReply(id_e, C2, "in_flight", T0 + 40 min))` (the cancelled one, the expired one and the owner-approved card absent); `counts()["sent"] == 1`, `["uncertain"] == 2`, `["failed"] == 1`.
- `test_s06_4_the_catch_up_is_produced_even_with_zero_activity` (S06.4 #3) — a delegation over one chat, nothing in it → a `CatchUp` (not `None`), `status == "ended"`, `message_count == 0`, `replies == cards == decisions == ()`, `counts() == {"messages": 0, "sent": 0, "failed": 0, "uncertain": 0, "pending_cards": 0, "decisions": 0}`.
- `test_rg09_the_catch_up_never_includes_decoy_chats_or_another_delegations_items` — decoy chat X (contact `CANARY-SYNTH-P3 Decoy`) covered by another delegation D2 of the same account, with contact messages, a held alert, an automatic reply and a held reply of D2; and in C1 (D1's chat) a D2 `delegation_escalation` held alert whose `refs` are `{"delegation_ids": [D2]}` → D1's `CatchUp` has only C1 in `chats`; no reply, card or decision has `conversation_id == X` or belongs to D2; no `name` contains the canary.
- `test_rg21_the_catch_up_never_reads_another_accounts_rows` — `build_catch_up(account_id=<account B>, delegation_id=<A's delegation>)` → `None`.
- `test_pending_cards_are_the_delegations_live_and_blocked_held_replies` — held replies of this delegation (one with its card not yet requested, OD-11): `awaiting_approval` in C1, `blocked` in C2, `rejected` in C1 (absent); an owner `awaiting_approval` card without `delegation_id` in a third chat (absent) → `cards == (CatchUpCard(id_a, C1, "awaiting_approval"), CatchUpCard(id_b, C2, "blocked"))`.
- `test_decisions_are_the_alerts_held_for_this_delegations_catch_up` — in C1: `triage_decision` batched `delegation_catch_up` at `T0 + 1 h` (in); the same kind batched at `T0 - 1 h` (out: before the window); `triage_needs_reply` batched with `reason_code="already_alerted"` (out); `triage_decision` with `status="sent"` (out); `delegation_escalation` batched `delegation_catch_up` at `T0 + 2 h` with `refs={"delegation_ids": [D1]}` and `priority="urgent"` (in) → `decisions == (CatchUpDecision(n1, C1, "triage_decision", "normal"), CatchUpDecision(n5, C1, "delegation_escalation", "urgent"))` (`seed_notification`'s default priority is `normal`).
- `test_a_chat_handed_to_a_newer_delegation_counts_only_until_the_handover` — before the end, set C1's link `ended_at = T0 + 2 h` (`UPDATE app.delegation_conversations ... WHERE delegation_id = :d AND conversation_id = :c`); contact messages in C1 at `T0 + 1 h` and `T0 + 3 h` → C1's `window_end == T0 + 2 h` and `message_ids == (m_1h,)`; C2's `window_end == END`.
- `test_a_delegation_cancelled_before_it_started_has_an_empty_catch_up` — `starts_at = T0 + 2 h`, ended at `T0` → `status == "cancelled"`, every chat has `message_ids == ()`, `caveats == ()`.
- `test_the_catch_up_carries_the_coverage_caveats_of_its_window` — a `CoverageGap(account_id=…, conversation_id=None, kind="outage", certainty="possible", at_risk_from=T0 + 1 h, at_risk_to=T0 + 90 min)` → exactly one entry of `caveats` starts with `Possible gap: messages sent between 2026-11-02 15:00 and 2026-11-02 15:30` (other caveats, such as "no business connection" when the test account has none, may also be present: assert on the entry, not on `len(caveats)`).
- `test_an_active_or_missing_delegation_has_no_catch_up` — a still-active delegation → `None`; an unknown id → `None`.
- `test_the_requester_enqueues_one_high_priority_catch_up_per_delegation` — with `env` and a `RecordingScheduler` (a small class in the test file implementing `JobScheduler.defer`, recording `(task, args)`); inside one `transaction(env.factory)`: an ended delegation, then `request(...)` twice → after commit exactly one `notifications` row with `kind == "delegation_catch_up"`, `priority == "high"`, `alert_class == "standard"`, `dedupe_key == f"catchup:{d}"`, `refs == {"delegation_ids": [d]}`, `conversation_id is None`, `status == "queued"`; the scheduler recorded exactly one `("notify.deliver", {"notification_id": <id>})`.
- `test_the_requester_enqueues_nothing_for_a_delegation_that_is_still_active` — no row, nothing deferred.
- `test_rg21_the_requester_enqueues_nothing_for_another_accounts_delegation` — `request(account_id=<B>, delegation_id=<A's ended delegation>)` → no row in either account.

### 7.2 Part B tests

**`tests/unit/test_catch_up_texts.py`** (pure; `CatchUp` built by hand; `timezone="Asia/Tashkent"`; the file defines `DASH = "–"` and writes every expected line that shows "–" below as an f-string with `{DASH}`, never the literal character, because ruff RUF001 flags it and the file may carry no suppression):
- `test_render_catch_up_shows_every_section_with_true_counts` — the catch-up: `delegation_id=12`, `kind="time_bounded"`, `status="ended"`, `ended_reason="time_elapsed"`, `starts_at=T0`, `ended_at=END`; chats: C5 named `Aziza` with three messages, first `T0 + 10 min`, last `T0 + 100 min`; C6 with `name=None` and no messages; replies: `(345, C5, "sent", T0 + 12 min)`, `(346, C5, "uncertain", T0 + 102 min)`; cards: `(400, C6, "awaiting_approval")`; decisions: `(900, C5, "triage_decision", "high")`; caveats: `("Possible gap: messages sent between 2026-11-02 15:00 and 2026-11-02 15:30 in any chat may be missing (they could not be processed).",)` → the text equals exactly:
  ```
  🧾 <b>Catch-up: delegation #12 has ended</b>
  2026-11-02 14:00 → 2026-11-02 19:00 (Asia/Tashkent)
  It ended at its end time.

  <b>What came in</b>
  3 message(s) from 2 chat(s).
  • Aziza: 3 messages, 14:10 – 15:40
  • A contact: no messages

  <b>Answered automatically</b>
  Sent: 1 · Delivery uncertain: 1 · Not sent: 0
  • Aziza: sent at 14:12 (reply #345)
  • Aziza: may or may not have arrived — check the chat (reply #346)
  Each reply is in /activity with this delegation.

  <b>Waiting for you</b>
  • A contact: a reply waits for your approval

  <b>Decisions needed</b>
  • Aziza: needs a decision from you

  <b>What may be missing</b>
  • Possible gap: messages sent between 2026-11-02 15:00 and 2026-11-02 15:30 in any chat may be missing (they could not be processed).
  ```
- `test_render_catch_up_with_zero_activity_says_so_in_every_section` — `delegation_id=13`, `ended_reason="owner_ended"`, one chat `Aziza` without messages, nothing else → exactly:
  ```
  🧾 <b>Catch-up: delegation #13 has ended</b>
  2026-11-02 14:00 → 2026-11-02 19:00 (Asia/Tashkent)
  You ended it.

  <b>What came in</b>
  0 message(s) from 1 chat(s).
  • Aziza: no messages

  <b>Answered automatically</b>
  Sent: 0 · Delivery uncertain: 0 · Not sent: 0
  No automatic replies.

  <b>Waiting for you</b>
  Nothing is waiting for your approval.

  <b>Decisions needed</b>
  No decisions are waiting.
  ```
- `test_render_catch_up_of_a_cancelled_delegation` — `delegation_id=14`, `status="cancelled"`, `ended_reason="owner_ended"`, `starts_at=T0 + 6 h`, `ended_at=T0` → exactly `"🧾 <b>Delegation #14 was cancelled before it started</b>\nIt was due to start at 2026-11-02 20:00 (Asia/Tashkent).\nYou ended it.\nNothing was handled for you, and nothing was sent."`.
- `test_rg20_uncertain_failed_and_in_flight_replies_are_never_shown_as_sent` — replies in groups `uncertain`, `failed`, `in_flight` only → the counts line is `Sent: 0 · Delivery uncertain: 1 · Not sent: 1 · Still being sent: 1`; the reply lines are `… may or may not have arrived — check the chat (reply #…)`, `… not sent (reply #…)`, `… still being sent; the outcome is not known yet (reply #…)`; the substring `sent at` does not occur.
- `test_a_standing_or_multi_day_catch_up_shows_full_dates` — `kind="standing"`, `starts_at=T0`, `ended_at=T0 + 2 days`, one message at `T0 + 1 day` → the first line is `🧾 <b>Catch-up: standing delegation #15 has ended</b>` and the chat line reads `• Aziza: 1 message at 2026-11-03 14:00`.
- `test_every_decision_kind_has_its_line` — parametrized: `triage_urgent` → `something urgent`; `triage_decision` → `needs a decision from you`; `triage_needs_reply` → `is waiting for your reply`; `triage_suspicious` → `sent something that looked like an attempt to instruct the assistant; nothing was done`; `delegation_escalation` with priority `high` → `needs you`; with `urgent` → `needs your decision`; `monitor_match` → `needs your attention`.
- `test_a_blocked_card_is_ready_to_copy` — line `• Aziza: a draft is ready for you to copy; it cannot be sent from here`.
- `test_every_ended_reason_has_its_line` — parametrized over the seven `ENDED_REASON_LINES` keys (each line present); `None` → no reason line.
- `test_long_sections_are_cut_with_true_totals` — 20 chats with one message each → 15 chat lines then `…and 5 more chat(s).` and `20 message(s) from 20 chat(s).`; 25 `sent` replies → 20 reply lines, `…and 5 more.`, counts line `Sent: 25 · …`; 12 cards → 10 lines and `…and 2 more.`; 12 decisions → 10 lines and `…and 2 more.`
- `test_catch_up_renderer_escapes_untrusted_text_once` — GW-ESC §3.3 rule 6 (final text, TL ruling R39). `NAME = 'A&B <b>x</b> <a href="https://example.invalid/">y</a> &amp;'`. **Part 1:** a catch-up with one chat named `NAME`, no messages, no replies, cards or decisions; pass `render_catch_up(...)` through `telegram.render.sanitize` (import it in the test only; the module never imports `assistant.telegram`). The output contains `&lt;b&gt;x&lt;/b&gt;`, `&lt;a href=` and `&amp;amp;` exactly once each, `A&amp;B` exactly once, and no raw `<b>x` and no `<a ` anywhere; the application's own headline `<b>Catch-up: delegation #…</b>` is still raw markup in the output. **Part 2:** the same name on one chat that has one message, one `sent` reply, one `awaiting_approval` card and one `triage_decision` decision: each of `A&amp;B`, `&lt;b&gt;x&lt;/b&gt;`, `&lt;a href=` and `&amp;amp;` occurs exactly 4 times (once per line that shows the name: the chat, reply, card and decision lines; never doubled on any line).

**`tests/integration/test_catch_up_renderer.py`** (Part B; `env`, `FakeChannel` from `tests/fakes/channel.py`, `NotificationDelivery(session_factory=env.factory, channel=channel, clock=env.clock, scheduler=env.scheduler, enabled=True)` with `register_renderer(NotificationKind.DELEGATION_CATCH_UP, CatchUpRenderer(clock=env.clock))`; `env.scheduler` is `ProcrastinateScheduler | None`, so build the delivery in a small helper that first does `assert env.scheduler is not None`, as `tests/integration/test_notifications.py::delivery` does; before delivering, `env.clock.set(END + timedelta(minutes=1))`, which is 19:01 in Asia/Tashkent, outside the default quiet hours 23:00–08:00):
- `test_the_catch_up_is_delivered_once_through_the_notification_claim` (invariant 6) — an ended delegation with one automatic `succeeded` reply; `request(...)` committed; `deliver(id)` → `DeliveryResult("sent")`, exactly one send whose text starts with `🧾 <b>Catch-up: delegation #` and contains `(reply #<id>)`; a second `deliver(id)` → `DeliveryResult("not_queued")` and still one send.
- `test_the_renderer_writes_one_content_free_audit_row_with_the_counts` — after one delivery: exactly one `audit_log` row with `event == "delegation_catch_up_rendered"`, `subject_type == "delegation"`, `subject_id == d`, `actor == "system"`, `details == {"messages": 1, "sent": 1, "failed": 0, "uncertain": 0, "pending_cards": 0, "decisions": 0}` (one contact message, one reply); every `details` value is an `int`.
- `test_the_renderer_suppresses_a_catch_up_whose_delegation_is_gone` — two rows made with `p2_factories.seed_notification(session, account_id=…, kind="delegation_catch_up", priority="high", refs=…)` (distinct `dedupe_key`s): one whose `refs` are `{"delegation_ids": [987654321]}` (an id that does not exist), one with `refs == {}` → each `deliver` gives `DeliveryResult("suppressed", "source_deleted")`, nothing sent, no audit row.
- `test_the_delivered_catch_up_never_shows_a_decoy_chat_or_another_delegations_items` (RG-09 at the text level) — the decoy data of Part A's RG-09 test (decoy chat X named `CANARY-SYNTH-P3 Decoy`, delegation D2's reply, card and `delegation_escalation` alert, contact messages in X) plus D1's own chat C1 with one message and one `succeeded` reply; end D1, request, deliver → the one send contains `(reply #<D1's reply id>)` and contains neither `CANARY-SYNTH-P3`, nor D2's reply id as `(reply #<id>)`; the audit row's `details["messages"]` counts only C1's message.
- `test_the_catch_up_needs_no_model_and_no_consent` (invariant 9, OD-11) — an account with no consent rows and no LLM anywhere in the set-up → the catch-up is delivered (`"sent"`).
- `test_the_catch_up_carries_the_pause_button` — `channel.buttons[0] == (pause_button(),)` (label `⏸ Pause automatic replies`, data `pz:pause:p`; one button, no "Open chat" card and no Useful or Not useful, R82). Strict xfail "awaiting G1" only under B3's rule.
- `test_wire_surfaces_registers_the_catch_up_requester_and_renderer` — build the `P3WireContext` with the same helper F0-R's `tests/integration/test_p3_wiring.py` uses, with `notification_delivery` replaced (`dataclasses.replace`) by a small spy class recording `register_renderer(kind, renderer)` calls; `wiring = _wire_surfaces(ctx)` → `isinstance(wiring.ports.catch_up_requester, DelegationCatchUpRequester)` and the spy recorded `NotificationKind.DELEGATION_CATCH_UP` with a `CatchUpRenderer`.

### 7.3 `tests/integration/test_log_hygiene_m6_catch.py`

- `test_the_catch_up_builder_and_requester_log_ids_and_counts_only` (Part A) — the contact name, a message text and a held alert's chat contain `CANARY-SYNTH-P3`; inside `capture_logs()` (F0-E's `tests/log_hygiene.py`): `build_catch_up`, `request` for an ended, an active and an unknown delegation → `assert_content_free(output, CANARY)`.
- `test_the_catch_up_renderer_logs_nothing_with_content` (Part B, appended) — the same canary data delivered once through `NotificationDelivery` → `assert_content_free(output, CANARY)`.

You may add tests; never fewer, never with changed expected values.

## 8. Truthfulness edge cases

- **Uncertain is never sent (RG-20).** "May or may not have arrived — check the chat" and "still being sent; the outcome is not known yet" are separate groups with separate counts; the audit counts an in-flight reply as uncertain, never as sent.
- **Zero activity still gets a catch-up** (S06.4 #3): every section says "none" in words; a cancelled delegation says nothing was handled and nothing was sent.
- **Every message counts, per chat, inside its own window**: the start included, the end excluded; a chat handed to a newer delegation stops at the handover; a message before the start or after the end never counts. Deleted or expired messages count as having come in, and the caveats say they are missing.
- **Only this delegation's items** (RG-09): no decoy chat, no other delegation's reply, card or escalation; an owner card without the delegation is not "waiting for you" here.
- **"Answered automatically" means automatic and sent (or possibly sent).** A cancelled or expired automatic action was never sent and is not counted; an owner-approved held reply is the owner's own send and is not counted; nothing is called "sent" unless the action `succeeded`.
- **Read at delivery**: a card the owner approved before the catch-up is delivered is no longer listed as waiting; the audit row's counts and the text come from the same `CatchUp` (an in-flight reply is counted under `uncertain` in the audit row and shown as "Still being sent" in the text, never as sent).
- **Gone means suppressed**: a deleted delegation, a foreign id or a still-active delegation renders nothing (`None`), never a stale or empty message.
- **Coverage is surfaced** (AGENTS §7): gaps, an inactive connection and deleted messages in the window are listed under "What may be missing".
- **No model, no consent needed** (OD-11, invariant 9): the catch-up is the owner's own records, built deterministically.
- **True totals**: a cut list says how many more; the count lines always hold every item.

## 9. Acceptance checklist

- [ ] Every test in §7 exists, was seen failing, and passes in CI (the Pause-button test passes or is strict-xfail under B3's rule only).
- [ ] Part A: `git diff --stat origin/int/p3...HEAD` lists only the three Part A files. Part B: only the Part B files (plus §5's separate commit, and QJ1's marker-removal commit under R47, if any).
- [ ] Both modules import nothing from `assistant.telegram`, `assistant.agent`, `assistant.llm` or `assistant.jobs`; `lint-imports` clean.
- [ ] Every query carries `account_id`; replies and cards are selected by `delegation_id`; decisions by chat, window and `delegation_ids`.
- [ ] The text matches §7.2's exact strings; each name and caveat is escaped exactly once.
- [ ] The requester never enqueues for an active, unknown or foreign delegation; the dedupe key is `catchup:{delegation_id}`.
- [ ] The renderer and the requester are wired together, in `_wire_surfaces`, whatever the flags; the import block is unchanged.
- [ ] `ruff check`, `ruff format --check` and `mypy` (strict) clean on the changed files; no broad `except` (only `ZoneInfoNotFoundError` and `ValueError`, in `_local`); no `assert` in production code.
- [ ] GW-ESC (the verification of P2 #133) is closed before Part B merges (no P3 renderer merges before it; WORKFLOW §7). The Part B escape test follows GW-ESC §3.3 rule 6.
- [ ] Each PR description has the filled template, the test commands and results, the counts-only audit note and the open items.

## 10. When to stop and ask

Ask on the M6-CATCH issue, starting the comment with "Question for J1:" (plain text, never an @-mention). If nobody answers within 2 h, David raises it with the TL.

- A name in §6 (`Delegation`, `DelegationConversation`, `end_delegations`, `NotificationKind.DELEGATION_CATCH_UP`, `NotificationRefs.delegation_ids`, `CatchUpRequester`, `P3Ports.catch_up_requester`, `P3WireContext.notification_delivery`, `ctx.scheduler`) is missing or named differently on your base.
- `seed_delegation` or `seed_autonomous_action` cannot create the rows §7 needs (for example it refuses `status`, `starts_at` or `delegation_id`).
- F0-R's `tests/integration/test_p3_wiring.py` has no helper that builds a `P3WireContext` you can reuse.
- `_wire_surfaces` has a shape that none of the three cases of §6.2 B4 covers, or `ports` / `catch_up_requester` is already set there (a `P3Ports` field set by two fragments raises at startup).
- `pause_button` is missing from `core/autonomy_texts.py` on your base (then B3's fallback applies; say so on the issue).
- `period_coverage` or `caveats` behaves differently from §6.1 A2.7 (for example a different signature).
- An exact string in §7.2 looks wrong: do **not** change it; ask.
- An existing notification, digest or F0-R test fails and §5 does not name it.
- You think you need any file outside §3, or a model, a job or claim code.
- You have spent more than the unit's size on a part and are not close.

**Contract gaps already known (the TL tracks them; do not wait on them):** P3-interfaces §11.2 names no `refs` for `delegation_catch_up` (this spec uses `delegation_ids` only); §13 does not say whether a re-render after a failed send writes a second `delegation_catch_up_rendered` row (this spec: one row per render); the shape of several David units' blocks inside one `_wire_surfaces` is not fixed by F0-R (this spec follows the `wiring = P3Wiring()` / `replace` convention of STAT-ACT and M7-MRV, with a fallback for SET-P3's accumulator shape, §6.2 B4; the TL should fix one canonical skeleton in P3-interfaces §15.3); the Pause button helper `core/autonomy_texts.py::pause_button()` is settled by R45 but is not yet in P3-interfaces §5.2 or §12.5 or in F0-S's spec (its fallback in §6.2 B3 covers a base without it); QA-S6 D2 says the catch-up "links each sent item to its action (the `/activity` reference)", while `/activity` prints no action id (the link is the shared `action_id`, plus time and chat name; this spec says so in §1).

## 11. Coding rules (reviewers block on these)

- No broad `except` (no bare `except`, no `except Exception`); catch only the exceptions §6 names.
- No `assert` in production code, neither for control flow nor for type narrowing.
- Full type hints; `mypy --strict` clean; names exactly as §6.
- No `datetime.now()`: the renderer uses its `Clock`; the builder takes everything as arguments; tests use fixed datetimes and `FakeClock`.
- Logs carry ids and counts only (`catch_up_built`, `catch_up_requested`, `catch_up_delegation_not_found`, `catch_up_delegation_still_active`); never a name, a message, a topic or a caveat sentence.
- No Telegram calls, no model, no job, no claim code: the renderer returns text and buttons; delivery is the existing notification path.
- Escape each untrusted field once with `html.escape(value, quote=False)`; never pre-escape for the gateway.
- Synthetic test data only (invented names; canary `CANARY-SYNTH-P3`).
- GitHub: every PR body and comment through `--body-file <file>`; no bare `@names`; no recurring status comments.
- Commits: Conventional Commits, `git commit -F <file>`; never `--no-verify`; never commit `.env`, `var/` or real data.

## 12. Pull requests

**PR 1 (Part A).**
- **Title:** `M6-CATCH (1/2): end-of-delegation catch-up builder and requester`.
- **Size:** about 400 changed lines or fewer (expected ≈ 170 code + ≈ 230 tests).
- **Labels:** `team:david`, `eng:d1` on the draft; when CI is green, mark it ready and add `needs-review:j1` and `needs-qa`; comment on the issue. QA is **before merge**: the TL merges only after QD1's review is clean.
- **Template** (WORKFLOW §4.3): *Unit:* `WU: M6-CATCH · Spec: docs/engineering/p3/specs/M6-CATCH.md · Issue: #<n> · Base: int/p3`; Tier: other; QA lane: before merge. *Safety invariants touched:* 4 (isolation of the catch-up's content to its delegation) — `test_rg09_the_catch_up_never_includes_decoy_chats_or_another_delegations_items`; 11 (honest status) — `test_s06_4_the_catch_up_links_sent_items_to_actions_including_failed_and_uncertain`; 12 (tenant scoping) — `test_rg21_the_catch_up_never_reads_another_accounts_rows`, `test_rg21_the_requester_enqueues_nothing_for_another_accounts_delegation`. *Tests:* the §7.1 names; S06.4 #1–#3 added; RG-09 and RG-21 extended; commands and results, or "not run locally: <reason>; CI is the gate". *Shared lists:* none. *Data:* no migration, no content column (the catch-up reads existing rows); logs — `test_log_hygiene_m6_catch.py`. *Prompts, flags and settings:* none. *Docs:* none in this PR. *Interface changes:* none; out-of-list edits: none. *For M6-DELEG and M6-LIFE:* `P3Ports.catch_up_requester` is a `DelegationCatchUpRequester` once Part B merges; until then it is `None` and they log `catch_up_not_wired`.
- **Open items for M06.md:** anything non-blocking the review raised.

**PR 2 (Part B).**
- **Title:** `M6-CATCH (2/2): delegation_catch_up renderer and wiring`.
- **Size:** about 400 changed lines or fewer (expected ≈ 170 code + ≈ 250 tests; if the PR passes ≈ 450 changed lines, ask on the issue before pushing, §10).
- **Labels and template** as PR 1 (base `p3/m6-catch` until Part A merges, then `int/p3`), with: *Safety invariants touched:* 6 (at-most-once delivery through the existing claim) — `test_the_catch_up_is_delivered_once_through_the_notification_claim`; 9 (no model, no consent needed) — `test_the_catch_up_needs_no_model_and_no_consent`; 11 — `test_rg20_uncertain_failed_and_in_flight_replies_are_never_shown_as_sent`. *Tests:* the §7.2 names; RG-20 extended; RG-09 extended at the text level (`test_the_delivered_catch_up_never_shows_a_decoy_chat_or_another_delegations_items`); `test_catch_up_renderer_escapes_untrusted_text_once` (GW-ESC §3.3 rule 6). *Data:* audit row `delegation_catch_up_rendered`, counts only. *Docs:* ARCHITECTURE §17 (new bullet). *Shared lists:* §5's append, if made. *Strict-xfail markers removed:* none by you. QA-S6 D1–D3 turn green with M6-DELEG and D4 with M6-LIFE (QA-S6 §3.6); if one now passes, say so on the issue: QJ1 removes the marker in a commit on your branch (TL ruling R47), which the diff check of §9 then also shows.
- After merge of both: LIVE-P3 checks "catch-up delivered" in the J5 live window.
