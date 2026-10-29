# OPS-RATE — Weekly usefulness rating (junior unit)

| | |
|---|---|
| **Unit** | OPS-RATE — the handler for F0-R's pre-registered periodic `system.weekly_rating` (one `usefulness_rating` card per account on Sunday at 19:00 owner-local or later, at most one per ISO week; R23, P3-T60), the `usefulness_rating` renderer (a 1–5 card), the `rate` press (content-free audit `usefulness_rated`) and `deploy/sql/usefulness.sql` |
| **Owner slot** | D1 (David's team, junior) |
| **Mentor** | E3 (Codex; peer review dispatched by the TL) |
| **QA** | QD1, after merge (on `int/p3` before R1) |
| **Size** | S (≈ 0.5 engineer-day, one fix round included) |
| **Depends on** | F0-R (`jobs/tasks_surfaces.py::SurfaceJobHandlers.weekly_rating`, the periodic shell, `_wire_surfaces`, `ControlRoutes`, the `HELD_IN_QUIET` delivery rule); F0-S (`core/flags.py::RATING_WEEKLY`, `NotificationKind.USEFULNESS_RATING` and its `DELIVERY_CLASSES` row); F0-E (`tests/log_hygiene.py`; `deploy/server.sh report`, which runs your SQL file). GW-ESC closed (it landed in P2 #133 and is a verification only; the TL posts "GW-ESC closed" on the `P3 integration` issue, and no P3 renderer merges before that post; P3-interfaces §15.5 step 0). M05 code reused unchanged: `inbox/notifications.py::enqueue` and `register_renderer`, `accounts/service.py::create_card`/`claim_card`, `accounts/callbacks.py::encode` |
| **Consumed by** | The owner (the card); SQ and the TL, who read `report usefulness` for the trial's weekly rating (LIVE-P3, PRD §21 R2 #7) |
| **Branch → base** | `p3/ops-rate` from `origin/int/p3` → PR to `int/p3` (D+4; F0 is merged by then) |
| **Normative inputs** | [P3-interfaces](../contracts/P3-interfaces.md) §7.1 (`WEEKLY_RATING_TASK` row), §7.2, §10.2 (`rating.weekly`), §11.2 (the `usefulness_rating` row: `held_in_quiet`, `high`, dedupe `rating:{account_id}:{iso_year}{iso_week:02d}`), §11.3, §12.3 (`rate` kind, ops `1`…`5`), §12.5 (Usefulness rating card), §13 (`usefulness_rated`: `score`, `iso_week`), §14.3, §15.1, §15.3, §15.4; decisions OD-19, P3-T52, P3-T60 (R23); [specs/GW-ESC.md](GW-ESC.md) §3.3 (escape-once rule); m07 evidence D15 ("deterministic weekly card (1–5), content-free audit row") |

## 1. Goal

The Personal v1 trial passes only if the owner rates the assistant at least 4 out of 5 every week (PRD §21 R2 #7). Nothing asks for that rating today. This unit adds:

- a handler that, once a week (Sunday from 19:00 in the owner's timezone), queues one `usefulness_rating` notification per active account;
- a renderer that turns it into a control-chat card "How useful was the assistant to you over the past week?" with five buttons `1` … `5`;
- a press handler that records the score in a content-free audit row and thanks the owner, only after the row is committed;
- a read-only SQL report, `deploy/sql/usefulness.sql`, that lists each ISO week's card status and score.

No model call, no message content, no new table, no new flag.

## 2. Read first

1. [handoffs/junior-engineers.md](../handoffs/junior-engineers.md), all of it.
2. [WORKFLOW](../WORKFLOW.md) §2, §3, §4 (PR template), §5, §6.2, §6.5 ("Escape once").
3. [P3-interfaces](../contracts/P3-interfaces.md) §11.2–§11.3, §12.3, §12.5, §13 (the `usefulness_rated` row), §15.3.
4. `src/assistant/inbox/notifications.py`: `enqueue`; `NotificationDelivery.deliver` (the renderer runs **inside the claim transaction**, before the send; a standard alert without `conversation_id` gets no "Useful/Not useful" card, only your `extra_buttons`); `register_renderer`.
5. `src/assistant/inbox/alert_texts.py`: `RenderedAlert`, `Renderer`.
6. `src/assistant/automation/monitors.py::stop_button` and `MonitorMatchRenderer` (a renderer that creates its own `pending_inputs` card inside the claim transaction and returns its buttons).
7. `src/assistant/telegram/control/monitors.py::handle_mon_press` (the press pattern you copy: `claim_card` in the press transaction, answer and edit after commit through the gateway).
8. `src/assistant/accounts/service.py`: `create_card`, `claim_card` (single use; scoped to the account; `None` = unknown, foreign, expired or already pressed). `src/assistant/accounts/callbacks.py`: `encode`, `parse`.
9. `src/assistant/telegram/control/handlers.py`: `CardPress`, `HandlerResult`, `CALLBACK_EXPIRED_TEXT`. `src/assistant/telegram/control/routes.py`: `CallbackHandler`, `ControlRoutes`.
10. `src/assistant/jobs/tasks_surfaces.py` (F0-R): `SurfaceJobHandlers.weekly_rating`, `PeriodicHandler`; the shell runs hourly at minute 41 and passes `datetime.fromtimestamp(timestamp, UTC)`.
11. `deploy/sql/nfr05.sql` and `tests/integration/test_nfr05_query.py` (the pattern for a read-only report and its test).
12. This spec, to the end.

## 3. Allowed files

| Path | New or existing | Exactly what may change |
|---|---|---|
| `src/assistant/inbox/rating.py` | new | The whole module (§6.1): constants, key helpers, `WeeklyRating`, `UsefulnessRatingRenderer`, `record_rating` |
| `src/assistant/telegram/control/rating.py` | new | The whole module (§6.2): `handle_rate_press` |
| `src/assistant/bootstrap.py` | existing | Inside `_wire_surfaces` only: the function-local imports and the lines of §6.3 |
| `deploy/sql/usefulness.sql` | new | The query of §6.4 |
| `tests/unit/test_rating_keys.py` | new | §7.1 |
| `tests/integration/test_weekly_rating.py` | new | §7.2 |
| `tests/integration/test_rating_press.py` | new | §7.3 |
| `tests/integration/test_usefulness_query.py` | new | §7.4 |
| `tests/integration/test_log_hygiene_ops_rate.py` | new | §7.5, copied from F0-E's template `tests/unit/test_log_hygiene_f0e.py` |
| `docs/ARCHITECTURE.md` | existing | §12.2, the "Periodic system tasks" table only: append the row of §6.5 at the end of the table |

Nothing else.

## 4. Forbidden

- `src/assistant/policy/`, `src/assistant/actions/`, `src/assistant/telegram/gateway.py`, `src/assistant/telegram/ingress/`, `src/assistant/telegram/control/{dispatcher,routes,handlers,panels}.py` and every other existing `telegram/control/` file, `migrations/`, `src/assistant/agent/` (including `prompts/` and `prompt_registry.py`), `src/assistant/privacy/`, consent code (`src/assistant/llm/consent.py`), `src/assistant/config.py`, `src/assistant/core/` (import only), `src/assistant/jobs/` (including `tasks_surfaces.py`), `AGENTS.md`.
- `src/assistant/inbox/notifications.py`, `inbox/notification_policy.py`, `inbox/notification_types.py`, `inbox/alert_texts.py`, `inbox/triage_store.py` and every other existing `inbox/` file; `src/assistant/accounts/` (import only).
- `deploy/server.sh` (F0-E's `report` action already runs any `deploy/sql/<name>.sql`; you add the file only), every other `deploy/sql/*.sql`.
- Every other wire function in `bootstrap.py` and every line outside `_wire_surfaces`.
- Any Bot API call or aiogram shortcut (`Message.answer`, `CallbackQuery.answer`, …): answers and edits go through `OutboundGateway` after commit (AGENTS §5, §7).
- `pyproject.toml`, `uv.lock`, `.env*`, `var/`, every `docs/` file except the one §12.2 row.
- **Every file not in §3.**

## 5. Shared lists you must update

None. The notification kind, its delivery class, the flag and the callback kind are F0's; you only register the renderer and the `rate` callback in `_wire_surfaces`. The audit event is a module constant in your own module (P3-interfaces §13: "each unit writes its events through module-level constants"). F0-R's duplicate check stops the process if `rate` is already registered; if a test reports a duplicate `rate` kind, stop and ask (§10).

## 6. Step-by-step

### 6.1 `src/assistant/inbox/rating.py` (exact API)

```python
"""Weekly usefulness rating (P3 OPS-RATE; PRD §21 R2 #7; OD-19; P3-T60, R23).

F0-R's hourly periodic `system.weekly_rating` (minute 41) calls
`WeeklyRating.run(now)`. Each active account with a control chat whose
owner-local time is Sunday 19:00 or later gets one `usefulness_rating`
notification. Its dedupe key holds the account and the ISO week of the
owner-local date, so there is at most one per account per ISO week, and a run
missed at 19:41 is caught up at 20:41. The renderer shows a single-use 1-5
card; a press records the content-free audit row `usefulness_rated`
(`score`, `iso_week`) through `record_rating`. No model call, no content."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.callbacks import encode
from assistant.accounts.models import Account, AuditLogEntry
from assistant.accounts.service import create_card
from assistant.core.channel import Button
from assistant.core.clock import Clock
from assistant.core.jobs import JobScheduler
from assistant.db.uow import transaction
from assistant.inbox.alert_texts import RenderedAlert
from assistant.inbox.models import Notification
from assistant.inbox.notification_types import NotificationKind, NotificationRefs, Priority
from assistant.inbox.notifications import enqueue
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

RATE_CARD: Final = "rate"
RATING_CARD_TTL: Final = timedelta(days=7)
RATING_WEEKDAY: Final = 7  # ISO weekday: Sunday
RATING_HOUR: Final = 19  # owner-local hour
SCORES: Final = (1, 2, 3, 4, 5)
EVENT_USEFULNESS_RATED: Final = "usefulness_rated"
CARD_TEXT: Final = (
    "📊 <b>Weekly check-in</b>\n"
    "How useful was the assistant to you over the past week? "
    "Tap 1 (not useful) to 5 (very useful)."
)
_KEY = re.compile(r"^rating:([0-9]+):([0-9]{4})([0-9]{2})$")


def iso_week_of(local_date: date) -> int: ...  # e.g. 202645
def rating_dedupe_key(account_id: int, local_date: date) -> str: ...
def iso_week_from_key(dedupe_key: str, account_id: int) -> int | None: ...
def is_rating_time(local: datetime) -> bool: ...


class RatingResult(StrEnum):
    RECORDED = "recorded"
    NOT_FOUND = "not_found"


async def record_rating(
    session: AsyncSession, *, account_id: int, notification_id: int, score: int, now: datetime
) -> RatingResult: ...


@dataclass(frozen=True, slots=True)
class WeeklyRating:
    session_factory: async_sessionmaker[AsyncSession]
    clock: Clock
    scheduler: JobScheduler | None
    enabled: bool  # RATING_WEEKLY in settings.feature_flags

    async def run(self, now: datetime) -> int: ...  # the PeriodicHandler: rows created


class UsefulnessRatingRenderer:
    def __init__(self, clock: Clock) -> None: ...
    async def __call__(
        self, session: AsyncSession, notification: Notification, account: Account
    ) -> RenderedAlert | None: ...
```

These names are binding (the press handler, the wiring and the tests import them).

**Rules (each is tested in §7):**

1. **`iso_week_of(local_date)`**: `year, week, _ = local_date.isocalendar()`; return `year * 100 + week`. The ISO year, not the calendar year: `date(2027, 1, 3)` → `202653`.
2. **`rating_dedupe_key(account_id, local_date)`** returns `f"rating:{account_id}:{iso_week_of(local_date)}"` (always six digits: the week is zero-padded by the arithmetic, `202601`).
3. **`iso_week_from_key(dedupe_key, account_id)`** returns the `YYYYWW` integer when `_KEY.fullmatch(dedupe_key)` matches (`fullmatch`, never `match`: `$` would accept a trailing newline), the first group equals `str(account_id)`, and the week (third group) is from `01` to `53`; otherwise `None`. Never raises.
4. **`is_rating_time(local)`**: `local.tzinfo is None` ⇒ `ValueError("local must be timezone-aware")`; otherwise `local.isoweekday() == RATING_WEEKDAY and local.hour >= RATING_HOUR`.
5. **`WeeklyRating.run(now)`**:
   - `enabled` is `False` ⇒ return `0` before opening any session.
   - `now.tzinfo is None` ⇒ `ValueError("now must be timezone-aware")`.
   - In one short transaction (`async with self.session_factory() as session, session.begin():`) read `select(Account.id, Account.timezone).where(Account.status == "active", Account.control_chat_id.is_not(None)).order_by(Account.id)`.
   - For each account: `zone = ZoneInfo(timezone)`; on `ZoneInfoNotFoundError` or `ValueError` (catch exactly these two) use `UTC` and log `weekly_rating_timezone_fallback` with `account_id` only. `local = now.astimezone(zone)`. If `not is_rating_time(local)`, skip the account.
   - Otherwise, in its own `async with transaction(self.session_factory) as uow:` (so `enqueue`'s after-commit defer runs):

     ```python
     notification_id = await enqueue(
         uow.session,
         account_id=account_id,
         kind=NotificationKind.USEFULNESS_RATING,
         priority=Priority.HIGH,
         dedupe_key=rating_dedupe_key(account_id, local.date()),
         refs=NotificationRefs(),
         clock=self.clock,
         scheduler=self.scheduler,
     )
     ```

     When it returned an id, count it and log `weekly_rating_enqueued` with `account_id` and `iso_week` only.
   - Return the count.
   - The class is `held_in_quiet` (F0-S table): `high` ⇒ immediate outside quiet hours, held to their end inside them, never capped; `notifications.deliver` still gates it. You do not test delivery; F0-R does.
6. **`UsefulnessRatingRenderer.__call__(session, notification, account)`** (runs in the delivery's claim transaction; no commit, no Telegram call):
   - `notification.account_id != account.id` ⇒ `None` (suppressed).
   - `iso_week_from_key(notification.dedupe_key, account.id) is None` ⇒ `None` (a malformed row is suppressed, never sent with a guessed week).
   - `card = await create_card(session, account_id=account.id, kind=RATE_CARD, clock=self._clock, ref_id=notification.id, ttl=RATING_CARD_TTL)`; if `card.token is None` ⇒ `raise RuntimeError("rating card has no token")` (cannot happen; never an `assert`).
   - `buttons = tuple(Button(str(score), encode(RATE_CARD, card.token, str(score))) for score in SCORES)` (one row, in this order; the channel lays them out in one row).
   - Return `RenderedAlert(CARD_TEXT, conversation_id=None, extra_buttons=buttons)`. No feedback buttons (R82): `conversation_id=None` means delivery attaches no alert card, and F0-S's `feedback_buttons` is false for `usefulness_rating`, so the 1–5 buttons are the only ones and a Useful or Not useful tap can never mute the weekly rating.
   - `CARD_TEXT` is application text with one balanced `<b>` pair and no untrusted field, so nothing is escaped (GW-ESC §3.3 rules 1–2).
7. **`record_rating(session, *, account_id, notification_id, score, now)`** (runs in the press transaction):
   - `score not in SCORES` ⇒ `ValueError("score must be 1 to 5")` (the press handler never passes one, §6.2).
   - `select(Notification.dedupe_key).where(Notification.account_id == account_id, Notification.id == notification_id, Notification.kind == NotificationKind.USEFULNESS_RATING.value)`; no row ⇒ `RatingResult.NOT_FOUND`.
   - `week = iso_week_from_key(key, account_id)`; `None` ⇒ `NOT_FOUND`.
   - `session.add(AuditLogEntry(account_id=account_id, at=now, actor="owner", event=EVENT_USEFULNESS_RATED, subject_type="account", subject_id=account_id, details={"score": score, "iso_week": week}))`; `await session.flush()`; return `RECORDED`. The details hold exactly these two integers (P3-interfaces §13; content-free).

### 6.2 `src/assistant/telegram/control/rating.py` (exact API)

```python
"""Usefulness card presses: `rate:<token>:<1-5>` (P3 OPS-RATE; P3-interfaces §12.3).

Follows `telegram/control/monitors.py::handle_mon_press`: the card is claimed
once, within the pressing owner's account only, in the press's transaction;
the score is recorded there; the answer and the card edit run after the
commit through the gateway, so the owner is thanked only for a rating that
is stored (invariant 11). An unknown op neither records nor uses up the card."""

RATED_ANSWER: Final = "Thanks!"
RATED_TEXT: Final = "📊 Weekly check-in: you rated the past week {score}/5. Thank you."
SCORE_BY_OP: Final[Mapping[str, int]] = MappingProxyType({str(score): score for score in SCORES})


async def handle_rate_press(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    scheduler: JobScheduler | None,
    account: Account,
    press: CardPress,
) -> HandlerResult: ...
```

It matches `routes.CallbackHandler`. Steps:
1. `target = ControlChat(account_id=account.id, chat_id=press.chat_id)`.
2. `score = SCORE_BY_OP.get(press.op)`; `None` ⇒ after commit answer `CALLBACK_EXPIRED_TEXT`; return `HandlerResult(False, "rate", None, note="no_longer_active")`. The card is **not** claimed (a forged op must not use it up).
3. `card = await claim_card(session, account_id=account.id, token=press.token, clock=clock)`; `card is None`, `card.kind != RATE_CARD` or `card.ref_id is None` ⇒ the same answer and result.
4. `result = await record_rating(session, account_id=account.id, notification_id=card.ref_id, score=score, now=clock.now())`. `NOT_FOUND` ⇒ answer `CALLBACK_EXPIRED_TEXT`; return `HandlerResult(False, "rate", None, note="not_found")`.
5. `RECORDED` ⇒ after commit: `gateway.answer_callback(press.callback_id, RATED_ANSWER)`, then, when `press.message_id` is not `None`, `gateway.edit_control_text(target, press.message_id, RATED_TEXT.format(score=score))` (the edit replaces the text and removes the buttons), else `gateway.send_text(target, RATED_TEXT.format(score=score))`. Return `HandlerResult(True, "rate", None, note="rated")`.
6. "After commit" means a local `_after(session, gateway, press, target, message_id, *, answer, text=None)` helper that registers one `db.uow.after_commit(session, run)` callback, exactly like `monitors._after`. No database work after the commit; no Bot API call before it.
7. Log nothing in the handler (the dispatcher already logs the press with codes).

### 6.3 Wiring (`_wire_surfaces` in `bootstrap.py`; function-local imports only)

Read `_wire_surfaces` on your base first.

```python
    # OPS-RATE
    from assistant.core.flags import RATING_WEEKLY
    from assistant.inbox.notification_types import NotificationKind
    from assistant.inbox.rating import RATE_CARD, UsefulnessRatingRenderer, WeeklyRating
    from assistant.telegram.control.rating import handle_rate_press

    weekly_rating = WeeklyRating(
        session_factory=ctx.session_factory,
        clock=ctx.clock,
        scheduler=ctx.scheduler,
        enabled=RATING_WEEKLY in ctx.settings.feature_flags,
    )
    ctx.notification_delivery.register_renderer(
        NotificationKind.USEFULNESS_RATING, UsefulnessRatingRenderer(ctx.clock)
    )
```

Then, in the returned `P3Wiring(...)`:
- `weekly_rating=weekly_rating.run` in its `SurfaceJobHandlers(...)`: if `_wire_surfaces` already builds one (COMMIT-DUE merged first), add only your keyword; otherwise import `SurfaceJobHandlers` function-locally from `assistant.jobs.tasks_surfaces` and add `surfaces=SurfaceJobHandlers(weekly_rating=weekly_rating.run)`;
- `RATE_CARD: handle_rate_press` in its `ControlRoutes(callbacks={...})`: if a routes fragment already exists (SET-P3, M6-CATCH or M7-MRV merged first), add only your entry to its `callbacks` mapping; otherwise import `ControlRoutes` function-locally from `assistant.telegram.control.routes` and add `routes=ControlRoutes(callbacks={RATE_CARD: handle_rate_press})`.

Keep every keyword already in the `P3Wiring(...)` call. The renderer and the route are registered whatever the flag; the handler checks `enabled` itself.

### 6.4 `deploy/sql/usefulness.sql`

Exactly this query, with this header comment (the comment may wrap differently; the code may not change):

```sql
-- Weekly usefulness rating (PRD §21 R2 #7: at least 4/5 every week; P3
-- OPS-RATE, P3-T60). Run on the server with
-- `just prod <ip> report usefulness [days]` (deploy/server.sh `report`:
-- psql, read-only transaction, psql variable `days`, default 7); tested in
-- tests/integration/test_usefulness_query.py.
--
-- Content-free by construction: notification kinds, statuses and dedupe
-- keys (`rating:<account>:<YYYYWW>`) and the integer `score` and `iso_week`
-- of the `usefulness_rated` audit rows; never a content column.
--
-- One row per account and ISO week with a rating card created, or a rating
-- recorded, in the window. card_status is the notification's status ('none'
-- when the card row is gone); the latest rating of a week counts; verdict:
-- 'met' (score >= 4), 'NOT met', or 'not rated'.
WITH cards AS (
    SELECT
        n.account_id,
        CAST(split_part(n.dedupe_key, ':', 3) AS integer) AS iso_week,
        n.status AS card_status
    FROM app.notifications AS n
    WHERE n.kind = 'usefulness_rating'
        AND n.dedupe_key ~ '^rating:[0-9]+:[0-9]{6}$'
        AND n.created_at >= now() - make_interval(days => CAST(:days AS integer))
),
ratings AS (
    SELECT DISTINCT ON (a.account_id, a.details ->> 'iso_week')
        a.account_id,
        CAST(a.details ->> 'iso_week' AS integer) AS iso_week,
        CAST(a.details ->> 'score' AS integer) AS score,
        a.at AS rated_at
    FROM app.audit_log AS a
    WHERE a.event = 'usefulness_rated'
        AND a.details ->> 'iso_week' ~ '^[0-9]{6}$'
        AND a.details ->> 'score' ~ '^[1-5]$'
        AND a.at >= now() - make_interval(days => CAST(:days AS integer))
    ORDER BY a.account_id, a.details ->> 'iso_week', a.at DESC
)
SELECT
    coalesce(c.account_id, r.account_id) AS account_id,
    coalesce(c.iso_week, r.iso_week) AS iso_week,
    coalesce(c.card_status, 'none') AS card_status,
    r.score,
    r.rated_at,
    CASE
        WHEN r.score IS NULL THEN 'not rated'
        WHEN r.score >= 4 THEN 'met'
        ELSE 'NOT met'
    END AS verdict
FROM cards AS c
FULL OUTER JOIN ratings AS r
    ON r.account_id = c.account_id
    AND r.iso_week = c.iso_week
ORDER BY 1, 2;
```

### 6.5 ARCHITECTURE §12.2 row (append at the end of the "Periodic system tasks" table)

Append it after the last row that is there. If COMMIT-DUE's `system.surface_due_commitments` row or a row of F0-C's part 2 is already the last row, put yours below it; if the two of you conflict in this spot, keep both rows.

```markdown
| `system.weekly_rating` | hourly (minute 41) | **As built (P3 OPS-RATE; flag `rating.weekly`; shell F0-R, `jobs/tasks_surfaces.py`):** `inbox/rating.py::WeeklyRating.run` gives each active account with a control chat, once its owner-local time is Sunday 19:00 or later, one `usefulness_rating` notification (priority `high`, class `held_in_quiet`; dedupe `rating:{account_id}:{iso_year}{iso_week:02d}` from the owner-local date, so at most one per ISO week and a missed 19:41 run is caught up later that evening). The renderer shows "How useful was the assistant to you over the past week?" with buttons 1–5 (a single-use `rate` card, 7 days); a press writes the content-free audit row `usefulness_rated` (`score`, `iso_week`, actor `owner`) and only then thanks the owner. `deploy/sql/usefulness.sql` (`server.sh report usefulness [days]`) lists each week's card status, score and verdict (≥ 4 = met) |
```

### 6.6 Steps (each ends with a check)

All commands run in your directory with `$env:COMPOSE_PROJECT_NAME = 'd1'` set, from PowerShell on the host (they run inside the container). If your session cannot run Docker, write the tests, push, let CI run them, and say so in the PR (WORKFLOW §5).

1. **Branch.** `git fetch origin`; `git switch -c p3/ops-rate origin/int/p3`. **Check:** `docker compose -f compose.dev.yml run --rm app uv run python -c "from assistant.inbox.notification_types import NotificationKind; from assistant.core.flags import RATING_WEEKLY; print(NotificationKind.USEFULNESS_RATING.value, RATING_WEEKLY)"` → prints `usefulness_rating rating.weekly`.
2. **Red tests.** Create the five test files of §7 and both modules with the docstrings, imports, constants and classes, every function and method body `raise NotImplementedError`, and `deploy/sql/usefulness.sql` containing only `SELECT 1;`. **Check:** `just test tests/unit/test_rating_keys.py tests/integration/test_weekly_rating.py tests/integration/test_rating_press.py tests/integration/test_usefulness_query.py -q` → every test fails for the right reason (`NotImplementedError`, a wrong value, or the wiring field being `None`), none with `ImportError` or `NameError`.
3. **Key helpers** (§6.1 rules 1–4). **Check:** `just test tests/unit/test_rating_keys.py -q` → passes.
4. **`WeeklyRating.run`.** **Check:** `just test tests/integration/test_weekly_rating.py -q -k "not wire"` → passes.
5. **Renderer and `record_rating`, then the press handler.** **Check:** `just test tests/integration/test_rating_press.py -q` → passes.
6. **Wiring** (§6.3). **Check:** `just test tests/integration/test_weekly_rating.py -q` → all pass.
7. **SQL** (§6.4). **Check:** `just test tests/integration/test_usefulness_query.py -q` → passes.
8. **Hygiene test** (§7.5). **Check:** `just test tests/integration/test_log_hygiene_ops_rate.py -q` → passes.
9. **ARCHITECTURE row** (§6.5). **Check:** `git diff origin/int/p3 -- docs/ARCHITECTURE.md` shows exactly one added table row.
10. **Static checks.** `docker compose -f compose.dev.yml run --rm app uv run ruff check src/assistant/inbox/rating.py src/assistant/telegram/control/rating.py src/assistant/bootstrap.py tests/unit/test_rating_keys.py tests/integration/test_weekly_rating.py tests/integration/test_rating_press.py tests/integration/test_usefulness_query.py tests/integration/test_log_hygiene_ops_rate.py`; the same files with `uv run ruff format --check`; `… uv run mypy src/assistant/inbox/rating.py src/assistant/telegram/control/rating.py src/assistant/bootstrap.py`; `… uv run lint-imports`; `just test tests/unit/test_no_aiogram_shortcuts.py tests/unit/test_layering.py -q` (the AST scan that enforces the gateway-only rule for `telegram/control/`, and the layering test). **Check:** all clean.
11. **Diff.** `git diff --stat origin/int/p3...HEAD` → exactly the ten files of §3.
12. **Commit and PR** (§12). Commit at step 2 (red tests) and at step 10 (green), each with `git commit -F <message file>`.

## 7. Named tests (red first)

Integration fixtures: the `env` fixture of `tests/integration/business_env.py` (clean database per test). For the press tests, build the environment with the route: `environment(migrated_url, owner_ids=frozenset({OWNER, OTHER_OWNER}), routes=ControlRoutes(callbacks={RATE_CARD: handle_rate_press}))` with `OTHER_OWNER = 7000000002`, then `onboard(env)` (and `onboard(env, OTHER_OWNER)` where named). Seed with `tests/integration/p2_factories.py` (`seed_account(session, timezone=…, status=…, control_chat_id=…)`, `seed_notification(session, account_id=…, kind="usefulness_rating", priority="high", dedupe_key=…)`). `RecordingScheduler` from `tests/integration/execution_env.py`. Call `env.clock.set(<the run time>)` before each `run`. Dates: 2026-11-08 and 2026-11-15 are Sundays (ISO weeks `202645`, `202646`); 2027-01-03 is the Sunday of ISO week `202653`; 2026-10-25 is the Sunday Berlin leaves summer time (UTC+1 from 01:00 UTC); Asia/Tashkent is UTC+5 with no DST; America/New_York is UTC−5 after 2026-11-01. The handler under test is `WeeklyRating(session_factory=env.factory, clock=env.clock, scheduler=None, enabled=True)` unless a test says otherwise.

### 7.1 `tests/unit/test_rating_keys.py`

- `test_iso_week_of_uses_the_iso_year_and_week` — `iso_week_of(date(2026, 11, 8)) == 202645`; `date(2026, 11, 15)` → `202646`; `date(2026, 1, 4)` → `202601`; `date(2027, 1, 3)` → `202653`; `date(2026, 11, 9)` → `202646`.
- `test_rating_key_names_the_account_and_the_local_iso_week` — `rating_dedupe_key(7, date(2026, 11, 8)) == "rating:7:202645"`; `rating_dedupe_key(7, date(2027, 1, 3)) == "rating:7:202653"`; `rating_dedupe_key(12, date(2026, 1, 4)) == "rating:12:202601"`.
- `test_iso_week_from_key_reads_only_this_accounts_well_formed_key` — `iso_week_from_key("rating:7:202645", 7) == 202645`; parametrized `None` for `("rating:8:202645", 7)`, `("rating:7:202600", 7)`, `("rating:7:202654", 7)`, `("rating:7:20264", 7)`, `("rating:7:2026455", 7)`, `("rating:x:202645", 7)`, `("auto:7:202645", 7)`, `("rating:7:202645:1", 7)`, `("rating:07:202645", 7)`, `("rating:7:202645\n", 7)`, `("", 7)`.
- `test_rating_time_is_sunday_from_19_00_owner_local` — with `tz = ZoneInfo("Asia/Tashkent")`: `datetime(2026, 11, 8, 19, 0, tzinfo=tz)` → `True`; `datetime(2026, 11, 8, 23, 59, tzinfo=tz)` → `True`; `datetime(2026, 11, 8, 18, 59, tzinfo=tz)` → `False`; `datetime(2026, 11, 7, 20, 0, tzinfo=tz)` (Saturday) → `False`; `datetime(2026, 11, 9, 19, 0, tzinfo=tz)` (Monday) → `False`.
- `test_rating_time_refuses_a_naive_time` — `is_rating_time(datetime(2026, 11, 8, 19, 0))` → `ValueError`.

### 7.2 `tests/integration/test_weekly_rating.py`

- `test_the_card_is_enqueued_on_sunday_at_19_owner_local` — one account, `timezone="Asia/Tashkent"`. `run(datetime(2026, 11, 8, 14, 41, tzinfo=UTC)) == 1` (19:41 local). One `notifications` row: `kind == "usefulness_rating"`, `priority == "high"`, `alert_class == "standard"`, `status == "queued"`, `dedupe_key == f"rating:{a.id}:202645"`, `refs == {}`, `conversation_id is None`, `contact_id is None`, `created_at == datetime(2026, 11, 8, 14, 41, tzinfo=UTC)`.
- `test_no_card_before_19_00_or_on_another_day` — Tashkent account: `run(2026-11-08T13:41Z)` (18:41 local) `== 0`; `run(2026-11-07T14:41Z)` (Saturday 19:41) `== 0`; `run(2026-11-08T19:41Z)` (Monday 00:41 local) `== 0`; no row.
- `test_at_most_one_card_per_iso_week` — Tashkent account: `run(2026-11-08T14:41Z) == 1`; `run(2026-11-08T15:41Z) == 0`; `run(2026-11-08T18:41Z) == 0` (23:41 local); `run(2026-11-15T14:41Z) == 1`; exactly two rows, keys `…:202645` and `…:202646`.
- `test_a_late_first_run_on_sunday_still_enqueues` — Tashkent account: `run(2026-11-08T17:41Z) == 1` (22:41 local; the 19:41 run was missed).
- `test_the_week_comes_from_the_owner_local_date` — `timezone="America/New_York"`: `run(datetime(2026, 11, 9, 0, 41, tzinfo=UTC)) == 1` (Sunday 19:41 local); the key is `f"rating:{a.id}:202645"` (the UTC date, a Monday, would give `202646`).
- `test_the_dst_change_day_uses_the_new_offset` — `timezone="Europe/Berlin"`: `run(2026-10-25T17:41Z) == 0` (18:41 local); `run(2026-10-25T18:41Z) == 1` (19:41 local); key `…:202643`.
- `test_the_iso_year_boundary_uses_the_iso_year` — `timezone="UTC"`: `run(datetime(2027, 1, 3, 19, 41, tzinfo=UTC)) == 1`; key `f"rating:{a.id}:202653"`.
- `test_rg21_each_account_gets_only_its_own_card` — account A (`Asia/Tashkent`) and B (`UTC`). `run(2026-11-08T14:41Z) == 1` and the row's `account_id == A.id` with key `f"rating:{A.id}:202645"`; then `run(2026-11-08T19:41Z) == 1` and the new row's `account_id == B.id` with key `f"rating:{B.id}:202645"`.
- `test_inactive_or_unreachable_accounts_get_no_card` — `timezone="UTC"` accounts with `status` `"onboarding"`, `"blocked"` and `"deleted"`, and one `"active"` with `control_chat_id=None`. `run(datetime(2026, 11, 8, 19, 41, tzinfo=UTC)) == 0`.
- `test_an_unknown_timezone_falls_back_to_utc` — `timezone="Mars/Base"`: `run(2026-11-08T14:41Z) == 0`; `run(2026-11-08T19:41Z) == 1`.
- `test_the_handler_does_nothing_with_its_flag_off` — `enabled=False` and a `session_factory` stub that raises `AssertionError` when called; a Tashkent account. `run(2026-11-08T14:41Z) == 0`.
- `test_a_naive_time_is_refused` — `run(datetime(2026, 11, 8, 19, 41))` → `ValueError`.
- `test_each_new_card_is_deferred_for_delivery_after_commit` — `scheduler=RecordingScheduler()`: `run(2026-11-08T14:41Z) == 1` with a Tashkent account; exactly one deferred entry with `task == "notify.deliver"` and `args == {"notification_id": <the row's id>}`; a second run that hour adds none.
- `test_wire_surfaces_wires_the_weekly_rating_handler_renderer_and_route` — call `bootstrap._wire_surfaces(ctx)` with the context helper F0-R's `tests/integration/test_p3_wiring.py` uses (import it; never copy it), once with `rating.weekly` in the feature flags and once without: `wiring.surfaces.weekly_rating` is a bound `WeeklyRating.run` whose `__self__.enabled` is `True` and `False` respectively; `wiring.routes.callbacks["rate"] is handle_rate_press`; `ctx.notification_delivery._renderers[NotificationKind.USEFULNESS_RATING]` is a `UsefulnessRatingRenderer`.

### 7.3 `tests/integration/test_rating_press.py`

Setup per test: `NOW = datetime(2026, 11, 8, 14, 41, tzinfo=UTC)`; `env.clock.set(NOW)`; the onboarded owner's account `a`; a notification `n = seed_notification(session, account_id=a.id, kind="usefulness_rating", priority="high", dedupe_key=f"rating:{a.id}:202645")`; render it inside `async with transaction(env.factory) as uow:` with `UsefulnessRatingRenderer(env.clock)` (load `n` and `a` in that session); the token is `callbacks.parse(rendered.extra_buttons[0].callback_data).token`. A press is `deliver(env, callback(f"rate:{token}:{op}", message_id=900))` (and `from_id`/`chat_id` = `OTHER_OWNER` where named).

**Card**
- `test_the_card_shows_five_score_buttons` — `rendered.text == CARD_TEXT`; `rendered.conversation_id is None`; labels `["1", "2", "3", "4", "5"]`; callback data `[f"rate:{token}:{s}" for s in "12345"]` (one shared token); exactly one `pending_inputs` row of kind `"rate"`, `ref_id == n.id`, `expires_at == NOW + timedelta(days=7)`.
- `test_usefulness_card_text_has_no_untrusted_field` — `telegram.render.sanitize(CARD_TEXT) == CARD_TEXT` (the one `<b>` pair is kept as markup, nothing is escaped; GW-ESC §3.3: there is no untrusted field to escape). This test stands in for GW-ESC §3.3 rule 6's `test_<renderer>_escapes_untrusted_text_once`, which needs a name or title to escape; the rating card has none.
- `test_a_malformed_rating_notification_is_suppressed` — a notification with `dedupe_key=f"rating:{a.id}:2026"`: the renderer returns `None`; no `pending_inputs` row is created.
- `test_another_accounts_rating_notification_is_suppressed` — rendering account B's notification with account A returns `None`; no card.

**Press**
- `test_a_press_records_the_score_and_then_thanks_the_owner` — op `"4"`. One `audit_log` row with `event == "usefulness_rated"`, `actor == "owner"`, `subject_type == "account"`, `subject_id == a.id`, `details == {"score": 4, "iso_week": 202645}`, `at == NOW`. `env.sender.answered_callbacks[-1]["text"] == "Thanks!"`; `env.sender.edits[-1]` has `message_id == 900`, `text == "📊 Weekly check-in: you rated the past week 4/5. Thank you."`, `reply_markup is None`; `env.sender.business_calls == 0` (a control-chat press never acts in a business chat). No `pending_inputs` row of kind `"rate"` is left.
- `test_each_score_from_1_to_5_is_recorded` — parametrized over ops `"1"` … `"5"`: `details["score"] == int(op)`.
- `test_a_second_press_on_the_same_card_changes_nothing` — op `"4"`, then op `"2"` with the same token: one audit row (score 4); the second answer is `CALLBACK_EXPIRED_TEXT`; no second edit.
- `test_an_unknown_op_neither_records_nor_uses_up_the_card` — ops `"6"`, `"0"` and `"x"`: no audit row, each answered `CALLBACK_EXPIRED_TEXT`, the card row still exists; then op `"3"` records score 3.
- `test_an_expired_card_records_nothing` — `env.clock.set(NOW + timedelta(days=7, seconds=1))`, op `"5"`: no audit row; answered `CALLBACK_EXPIRED_TEXT`.
- `test_rg21_another_owners_press_records_nothing` — `onboard(env, OTHER_OWNER)`; the other owner presses op `"5"` with the first owner's token (`from_id=OTHER_OWNER, chat_id=OTHER_OWNER`): no audit row in either account; answered `CALLBACK_EXPIRED_TEXT`; the first owner can still press (`"4"` records).
- `test_a_card_whose_notification_is_gone_records_nothing` — delete the notification row after rendering; op `"4"`: no audit row; answered `CALLBACK_EXPIRED_TEXT`.
- `test_the_owner_is_thanked_only_after_the_rating_commits` — call `handle_rate_press` directly inside `async with env.factory() as session, session.begin():` with `OutboundGateway(env.sender, env.clock, sleep=FakeSleep())` and a `CardPress(kind="rate", token=token, op="4", callback_id="cb-1", chat_id=a.control_chat_id, message_id=900)`, then `await session.rollback()`: no audit row, no answered callback, no edit.
- `test_a_press_without_a_message_id_sends_the_thanks_as_a_new_message` — call `handle_rate_press` directly inside `async with transaction(env.factory) as uow:` (so the after-commit step runs) with the same gateway and a `CardPress(kind="rate", token=token, op="4", callback_id="cb-2", chat_id=a.control_chat_id, message_id=None)`: one audit row with score 4; `env.sender.answered_callbacks[-1]["text"] == "Thanks!"`; `env.sender.sends[-1]["text"] == "📊 Weekly check-in: you rated the past week 4/5. Thank you."`; `len(env.sender.edits)` is unchanged from before the call.

### 7.4 `tests/integration/test_usefulness_query.py`

Copy the shape of `tests/integration/test_nfr05_query.py`: `QUERY = <repo>/deploy/sql/usefulness.sql`, `_code()` strips comments, the query runs on `db_session` exactly as written with `{"days": n}`; each test first runs `DELETE FROM app.notifications` and `DELETE FROM app.audit_log` (rolled back with the test) and inserts one account (`INSERT INTO app.accounts (telegram_user_id) VALUES (50606) RETURNING id`). Cards: `INSERT INTO app.notifications (account_id, kind, alert_class, priority, status, dedupe_key, refs, created_at) VALUES (:a, 'usefulness_rating', 'standard', 'high', :status, :key, '{}', now() - make_interval(secs => CAST(:ago AS float8)))`. Ratings: `INSERT INTO app.audit_log (account_id, at, actor, event, subject_type, subject_id, details) VALUES (:a, now() - make_interval(secs => CAST(:ago AS float8)), 'owner', 'usefulness_rated', 'account', :a, CAST(:d AS jsonb))`. Compare `(account_id, iso_week, card_status, score, verdict)` per row, in order.

- `test_the_query_reads_no_content_column` — for every `(table, column)` in `privacy.content_columns.CONTENT_COLUMNS` whose `app.<table>` appears in the code, the bare column name does not appear.
- `test_the_query_reads_only_notifications_and_audit_log` — the set of `app.<name>` references in the code is `{"notifications", "audit_log"}`.
- `test_the_query_is_one_read_only_statement` — as in `test_nfr05_query.py` (starts with `WITH `, exactly one `;` at the end, no `insert|update|delete|truncate|drop|alter|create` word).
- `test_rated_weeks_report_their_score_and_verdict` — card `rating:<a>:202645` `sent` 3 days ago with rating `{"score": 4, "iso_week": 202645}` 3 days ago; card `rating:<a>:202646` `sent` 1 hour ago with rating `{"score": 3, "iso_week": 202646}` 30 minutes ago; `days=7` → `[(a, 202645, "sent", 4, "met"), (a, 202646, "sent", 3, "NOT met")]`.
- `test_an_unrated_card_is_reported_as_not_rated` — card `queued` 1 hour ago, no rating → `[(a, 202645, "queued", None, "not rated")]`.
- `test_a_rating_without_its_card_row_still_counts` — rating `{"score": 5, "iso_week": 202645}` only → `[(a, 202645, "none", 5, "met")]`.
- `test_the_latest_rating_of_a_week_wins` — card `sent`; ratings score 2 (2 hours ago) and score 5 (1 hour ago), both week `202645` → one row with score 5, `"met"`.
- `test_malformed_rows_are_skipped` — a card with key `rating:<a>:2026`; ratings `{"score": 9, "iso_week": 202645}` and `{"score": 4, "iso_week": "x"}` → no row.
- `test_rows_outside_the_window_are_ignored` — card and rating 8 days ago: `days=7` → no row; `days=9` → one row.

### 7.5 `tests/integration/test_log_hygiene_ops_rate.py`

- `test_rating_paths_log_ids_and_codes_only` — the account also has a task titled `CANARY` (`tests/log_hygiene.py`). Inside `capture_logs()`: `run` at Sunday 19:41 local, render the card, press `"4"`. `assert_content_free(output, CANARY)`; the audit row's `details` keys are exactly `{"score", "iso_week"}` with integer values.

You may add tests; never fewer, never with changed expected values.

## 8. Truthfulness edge cases

- **"Thanks" only for a stored rating.** The answer and the edit run after the commit; a rollback says nothing (`test_the_owner_is_thanked_only_after_the_rating_commits`).
- **One rating per card.** The card is single use; a double tap answers "no longer active" and records nothing.
- **A forged or unknown op records nothing and does not use up the card.**
- **The week is the owner's week.** It comes from the owner-local date at the run (ISO year at the year boundary), never from UTC.
- **At most one card per ISO week,** however many hourly runs fall on Sunday evening.
- **"Past week", not "this week".** The card can be held to the end of quiet hours and arrive on Monday; the wording stays true.
- **A malformed or foreign notification is suppressed,** never shown with a guessed week.
- **Flag off means no card,** and nothing claims one was sent.
- The report says `not rated` for a week without a score and `none` for a card row that no longer exists; it never fills in a score.
- **A missed Sunday is not back-filled.** If no hourly run happens between Sunday 19:00 and midnight owner-local (the worker was down), that ISO week gets no card and the report has no row for it; Monday never creates last week's card (`test_no_card_before_19_00_or_on_another_day`). SQ records such a week as "no card sent" in the trial log, never as a rating.
- **A card already queued stays queued if the flag is turned off.** The flag stops new cards; a row that exists is delivered by the normal rules. The owner's rating counts only when the press commits.
- **The owner's score is the owner's alone.** Only a press from the owner's own chat, on a live card of that account, records a rating (`test_rg21_another_owners_press_records_nothing`); nothing infers, defaults or edits a score, and the report never averages across weeks.

## 9. Acceptance checklist

- [ ] Every test in §7 exists, was seen failing for the right reason, and passes.
- [ ] `git diff --stat origin/int/p3...HEAD` lists only the ten files of §3; the `bootstrap.py` hunk is inside `_wire_surfaces` only; the ARCHITECTURE hunk is one table row.
- [ ] `ruff check`, `ruff format --check` and `mypy` (strict) are clean on the changed files; `lint-imports` is clean (`inbox/rating.py` imports no `telegram`, `jobs`, `agent` or `llm` module).
- [ ] No `assert` in production code; the only `except` catches `ZoneInfoNotFoundError` and `ValueError` in `run`.
- [ ] No Bot API call or aiogram shortcut; answers and edits go through the gateway after commit.
- [ ] The audit row holds exactly `score` and `iso_week`; logs hold ids, the ISO week and counts only.
- [ ] `usefulness.sql` reads only `notifications` and `audit_log` and no content column.
- [ ] The PR description lists the card text and the report's columns for SQ (LIVE-P3).

## 10. When to stop and ask

Ask on the OPS-RATE issue, starting the comment with "Question for E3:" (plain text, never an @-mention). If nobody answers within 2 h, David raises it with the TL.

- `SurfaceJobHandlers.weekly_rating`, `RATING_WEEKLY`, `NotificationKind.USEFULNESS_RATING` or the `held_in_quiet` row does not exist on your base, or has another name or type.
- `_wire_surfaces` has no `P3Wiring` return you can extend as §6.3 says; F0-R's `test_p3_wiring.py` has no reusable context helper; or the `rate` callback kind is already registered.
- GW-ESC is not closed yet (GW-ESC landed in P2 #133, but the TL has not posted "GW-ESC closed" on the `P3 integration` issue): finish the PR but do not mark it ready.
- `RenderedAlert` has no `extra_buttons`, or the renderer does not run inside the claim transaction (`create_card` fails there).
- A named test cannot pass without changing a file outside §3.
- An expected value in §7 looks wrong when you work it out by hand: do **not** change it; ask.
- You have spent more than half a day and are not close.

## 11. Coding rules (reviewers block on these)

- No broad `except` (no bare `except`, no `except Exception`); catch only `ZoneInfoNotFoundError` and `ValueError`, only where §6.1 rule 5 says.
- No `assert` in production code, neither for control flow nor for type narrowing.
- Full type hints; `mypy --strict` clean; names exactly as §6.1 and §6.2.
- No `datetime.now()`: `run` takes `now`; the renderer, `record_rating` and the press use the injected clock or `now`.
- Content-free logs: `weekly_rating_enqueued` (`account_id`, `iso_week`) and `weekly_rating_timezone_fallback` (`account_id`) only.
- No Telegram call outside the gateway; no aiogram shortcut; nothing sent before the commit.
- GitHub: every PR body and comment through `--body-file <file>` (never an inline body in PowerShell); no bare `@names`; no recurring status comments.
- Commits: Conventional Commits, `git commit -F <file>`; never `--no-verify`; never commit `.env`, `var/` or real data.

## 12. Pull request

- **Title:** `OPS-RATE: weekly usefulness rating card and report`.
- **Size:** about 400 changed lines or fewer is the target (expected ≈ 230 code and SQL + ≈ 420 tests + 1 doc row). If the diff exceeds about 450 lines, move `deploy/sql/usefulness.sql` and `tests/integration/test_usefulness_query.py` into a second PR, `OPS-RATE (2/2): usefulness report`, on branch `p3/ops-rate-report` from `origin/int/p3` once the first PR has merged; the two PRs together contain exactly the files of §3.
- **Labels:** `team:david`, `eng:d1` on the draft; when CI is green (and GW-ESC is closed), mark it ready and add `needs-review:e3` and `needs-qa`; comment on the issue that it is ready.
- **Template** (WORKFLOW §4.3), filled honestly:
  - *Unit:* `WU: OPS-RATE · Spec: docs/engineering/p3/specs/OPS-RATE.md · Issue: #<n> · Base: int/p3`; Tier: other; QA lane: after merge.
  - *Safety invariants touched:* 7 fail closed (`test_a_malformed_rating_notification_is_suppressed`, `test_another_accounts_rating_notification_is_suppressed`, `test_an_unknown_op_neither_records_nor_uses_up_the_card`); 11 honest status (`test_the_owner_is_thanked_only_after_the_rating_commits`, `test_a_second_press_on_the_same_card_changes_nothing`); 12 tenant scoping (`test_rg21_another_owners_press_records_nothing`, `test_rg21_each_account_gets_only_its_own_card`).
  - *Tests:* the §7 names; the commands you ran and their result, or "not run locally: <reason>; CI is the gate". RG tests: RG-21 extended (the two `test_rg21_…`); RG-10 by `test_log_hygiene_ops_rate.py`.
  - *Shared lists updated:* none.
  - *Data:* no migration, no content column; one content-free audit event `usefulness_rated`; logs: `test_log_hygiene_ops_rate.py`.
  - *Prompts, flags and settings:* none new (reads `rating.weekly`, added by F0-S, default off).
  - *Docs:* ARCHITECTURE §12.2, one periodic row (E3 checks it as your mentor).
  - *Interface changes:* none; out-of-list edits: none.
  - *Open items for M07.md:* anything non-blocking the review raised.
- After merge: QD1 checks it on `int/p3`; a finding becomes one fix PR from you.
