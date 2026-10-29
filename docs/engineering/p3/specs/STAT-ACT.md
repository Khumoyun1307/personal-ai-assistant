# STAT-ACT — `/status` P3 blocks and the delegation authorization in `/activity` (junior unit)

| | |
|---|---|
| **Unit** | STAT-ACT — (A) `/activity` and `get_activity` name the delegation that authorised every automatic reply (`Authorization.kind = "delegation"`); (B) four content-free P3 blocks for `/status` and `get_status`: `delegations`, `takeover_pauses`, `autonomy_languages`, `automations` |
| **Owner slot** | D1 (David's team, junior) |
| **Mentor** | J1 (Claude; the peer review, dispatched by the TL). J1 also reviews the named touch point in `actions/activity.py` |
| **QA** | QD1, after merge (on `int/p3` before R1) |
| **Size** | M (≈ 1.0 engineer-day, one fix round included), delivered as **two PRs** of about 400 lines each |
| **Depends on** | Part A: F0-S (`actions.delegation_id`, the `action_auto_approved` audit event, `p3_factories`). Part B: F0-S and F0-R (`StatusBlock`, `StatusBlockLoader`, `build_status(block_loaders=…)`, `P3Wiring.status_blocks`, `_wire_surfaces`); the `takeover_pauses` block also needs M6-TAKE's `actions/autonomy.py::paused_by_takeover` (§6.2 B6; README gap reported) |
| **Branch → base** | Part A: `p3/stat-act` from `origin/int/p3` → PR to `int/p3`. Before CP1 (D0) branch from `origin/p3/f0-s` and retarget to `int/p3` after F0-S merges (WORKFLOW §4.2). Part B: `p3/stat-act-status` from `origin/int/p3` after CP1 → PR to `int/p3` |
| **Normative inputs** | [P3-interfaces §12.6](../contracts/P3-interfaces.md) (status blocks), §2.1 ("a scheduled action is never 'Sent'"), §2.2 (`action_auto_approved` carries `delegation_id`), §10.1 (`AutonomyConfig`), §10.2 (flags), §15.1 (`actions/activity.py`, `telegram/control/activity.py`: named touch points); [P3-schema §2.1, §2.2, §3.1, §3.3](../contracts/P3-schema.md); [specs/F0-R.md §3.9 step 3](F0-R.md); [specs/M6-TAKE.md §3.5](M6-TAKE.md) (`paused_by_takeover`, `takeover_counts`); PRD FR-ONB-08, FR-ACT-01, FR-ACT-02, J10, NFR-06; decisions OD-5, OD-14; AGENTS §6 invariants 11, 12 |

## 1. Goal

Two small, truthful read surfaces.

**Part A — who authorised a send.** Today `/activity` and `get_activity` know one authorization: the owner's Approve tap. After M06, an automatic reply is authorised by a delegation. Every automatic reply in `/activity` and `get_activity` that was sent, is uncertain, failed or is scheduled must name its delegation (from its `action_auto_approved` audit row), and must never say "approved by you". A scheduled automatic reply is never shown as sent (RG-20). An automatic action that was cancelled or expired never reached its approval-to-send stage as a send and keeps the existing "Not sent — <reason>" line (the reason texts already say "automatic reply" or "delegation").

**Part B — what `/status` says about autonomy (FR-ONB-08).** Four blocks: whether automatic replies are off, in drafts-only (shadow) mode, paused or on; the active delegations with their end times; the chats where your own message paused automatic replies (OD-5); the languages that may be answered automatically (OD-14); and how many reminders, follow-ups, scheduled summaries and monitors are on. Counts, ids, kinds and times only: no names, no message text, no topics.

## 2. Read first

1. [handoffs/junior-engineers.md](../handoffs/junior-engineers.md), all of it.
2. [WORKFLOW](../WORKFLOW.md) §2 (safety), §3 (workspace), §4 (branches, PR template), §5 (tests), §6.2 (Definition of Done), §6.5 (standing rulings: shared lists, escape once, wire functions).
3. `src/assistant/actions/activity.py` (whole file): `group_of`, `Authorization`, `ActivityEntry`, `activity`. Note how the owner authorization is read from the `action_approved` audit row.
4. `src/assistant/telegram/control/activity.py` (whole file): `render_activity` and its group branches.
5. `src/assistant/agent/tools/activity.py`: `STATUS_RULE` and the `rendered_entries` loop in `get_activity_tool`.
6. `tests/unit/test_activity_render.py` and `tests/integration/test_activity.py`: the test style you copy (`_entry(...)`, `seed_action`, `ToolContext` with `ToolServices`).
7. `src/assistant/actions/lifecycle.py::transition` (after F0-S): the audit row of `proposed → approved` is `action_auto_approved` with `details` `{from, to, version, reason, delegation_id}`.
8. Part B only: `src/assistant/observability/status.py` after F0-R (`StatusBlock`, `StatusBlockLoader`, `build_status(..., block_loaders=...)`, `StatusReport.as_dict()["blocks"]`), `src/assistant/telegram/control/handlers.py::render_status` (it prints each block line escaped once), `src/assistant/agent/tools/status.py` (it passes `services.status_block_loaders`), `src/assistant/automation/delegations_repo.py` (`list_active`, `DelegationView`), `src/assistant/actions/config.py` (`AutonomyConfig`), `src/assistant/core/flags.py`, `src/assistant/core/autonomy_texts.py` (`GateLanguage`), and the `_wire_surfaces` function in `src/assistant/bootstrap.py`.
9. Part B only: [specs/M6-TAKE.md](M6-TAKE.md) §3.3 and the last paragraph of §3.5 (`paused_by_takeover`).
10. This spec, to the end, before you start.

## 3. Allowed files

**Part A (`p3/stat-act`):**

| Path | New or existing | Exactly what may change |
|---|---|---|
| `src/assistant/actions/activity.py` | existing (named touch point) | The `Authorization` dataclass (§6.1 A1), the two module-level constants under `_STATE_TO_GROUP` and the authorization lookup block inside `activity()` plus the one `auth = …` line that reads it (§6.1 A2; no new import is needed: `Action` and `AuditLogEntry` are already imported). Nothing else: not `group_of`, not `ActivityEntry`, not `count_activity`, not the queries above the lookup |
| `src/assistant/telegram/control/activity.py` | existing (named touch point) | A new private helper `_delegation_suffix` and the label lines of `render_activity`'s `sent`, `awaiting`, `uncertain` and `failed` branches (§6.1 A3). Nothing else |
| `src/assistant/agent/tools/activity.py` | existing (named touch point; contract gap, §10) | `STATUS_RULE` (one sentence appended) and, in the `rendered_entries` dict, the new `"origin"` key and the `"kind"` and `"delegation_id"` keys of `"authorized"` (§6.1 A4). Nothing else |
| `tests/unit/test_activity_render_delegation.py` | new | §7.1 |
| `tests/integration/test_activity_delegation.py` | new | §7.2 |

**Part B (`p3/stat-act-status`):**

| Path | New or existing | Exactly what may change |
|---|---|---|
| `src/assistant/observability/status_p3.py` | new | The whole module (§6.2) |
| `src/assistant/bootstrap.py` | existing | Lines inside `_wire_surfaces` only (§6.2 B7) |
| `docs/ARCHITECTURE.md` | existing | §17, the "Status" bullet: append the as-built sentences of §6.2 B8. Nothing else in the file |
| `tests/integration/test_status_p3.py` | new | §7.3 |
| `tests/integration/test_log_hygiene_stat_act.py` | new | §7.4, copied from F0-E's template `tests/unit/test_log_hygiene_f0e.py` |

Nothing else, in either PR, except the shared-list edits of §5.

## 4. Forbidden

- `src/assistant/policy/`; every other file in `src/assistant/actions/` (only `actions/activity.py`, and there only §6.1 A1–A2). You **import** `actions/autonomy.py::paused_by_takeover` and `actions/config.py::AutonomyConfig`; you never edit them.
- `src/assistant/telegram/gateway.py`, `src/assistant/telegram/ingress/`, `src/assistant/telegram/control/{dispatcher,routes,handlers,panels}.py`.
- `src/assistant/observability/status.py` and `src/assistant/agent/tools/status.py` (F0-R's; your blocks live in `status_p3.py`).
- `migrations/`, `src/assistant/agent/prompts/`, `src/assistant/agent/prompt_registry.py`, `src/assistant/agent/{runtime,context,schemas}.py`, `src/assistant/agent/tools/{executor,registry}.py`, `src/assistant/privacy/`, consent code (`src/assistant/llm/consent.py`), `src/assistant/config.py`, `src/assistant/core/`, `src/assistant/jobs/`.
- `src/assistant/automation/{delegation_models,delegation_rules,delegations_repo}.py` (read only).
- `src/assistant/inbox/notification_policy.py`, `inbox/notifications.py`, `inbox/triage_store.py`.
- `bootstrap.py` outside `_wire_surfaces`, and its module import block (function-local imports only).
- `tests/scenarios/test_m04_j10_activity.py` and `tests/unit/test_activity_render.py` (M04 tests: they must keep passing **unchanged**; your new tests go in your own files).
- `AGENTS.md`, every `docs/` file other than the one ARCHITECTURE bullet in Part B, other units' files, `pyproject.toml`, `uv.lock`, `.env*`, `var/`.
- **Every file not in §3.**

## 5. Shared lists you must update

- **Part A:** none.
- **Part B:** if F0-R's `tests/integration/test_p3_wiring.py::test_empty_wire_functions_change_nothing` (or another F0-R test) asserts that a process with all P3 flags off registers **no status blocks**, append your four blocks to that test's expected set and change nothing else in it (WORKFLOW §6.5, append only), in a **separate last commit**, and name it in the PR. If any other exact-set test fails, stop and ask (§10).

## 6. Step-by-step

All commands run in your directory with `$env:COMPOSE_PROJECT_NAME = 'd1'` set, from PowerShell on the host (they run inside the container). `…` below stands for `docker compose -f compose.dev.yml run --rm app`. If your session cannot run Docker, write the tests, push, let CI run them, and say so in the PR (WORKFLOW §5).

### 6.1 Part A — the delegation authorization

**A1. `Authorization` (`actions/activity.py`).** Replace the dataclass with exactly:

```python
@dataclass(frozen=True, slots=True)
class Authorization:
    """Who authorised a send (FR-ACT-01, J10).

    `owner_approval`: the owner's Approve tap (audit `action_approved`).
    `delegation`: the delegation's rules at the auto-approval (audit
    `action_auto_approved`, M06). `delegation_id` is None for an owner
    approval, and for an automatic reply whose delegation row was deleted
    since (`actions.delegation_id` is then NULL)."""

    kind: Literal["owner_approval", "delegation"]
    approved_at: datetime
    version: int
    delegation_id: int | None = None
```

The default keeps every existing `Authorization("owner_approval", t, 2)` call and equality assertion working.

**A2. The lookup in `activity()`.** Add two module-level constants under `_STATE_TO_GROUP`:

```python
#: Groups whose entries carry an owner authorization (unchanged since S04.5).
_OWNER_AUTH_GROUPS = frozenset({"sent", "uncertain", "failed"})
#: An automatic reply is authorised from its auto-approval on, so a scheduled
#: or in-flight one (group "awaiting") also names its delegation.
_DELEGATION_AUTH_GROUPS = frozenset({"sent", "uncertain", "failed", "awaiting"})
```

Replace the block that starts at `# Look up authorization for sent / uncertain / failed actions` and ends after the `for audit in audit_rows:` loop with two lookups that fill one `auth_by_action: dict[int, Authorization]`:

1. **Owner approvals**, exactly as today but only for rows with `action.origin != "delegation_auto"` and a group in `_OWNER_AUTH_GROUPS` (event `action_approved`; newest first; first row per action wins; `details["version"]` required).
2. **Delegation authorizations**, for rows with `action.origin == "delegation_auto"` and a group in `_DELEGATION_AUTH_GROUPS`: the same query shape with `AuditLogEntry.event == "action_auto_approved"` (account-scoped, `subject_type == "action"`, `subject_id.in_(ids)`, ordered `at desc, id desc`); for the first row per action whose `details` is a dict with `"version"`: `Authorization(kind="delegation", approved_at=audit.at, version=int(details["version"]), delegation_id=<that action's delegation_id>)` (keep a `dict[int, Action]` of the fetched rows to read it). The `delegation_id` comes from the **column** only (NULL ⇒ `None`), never from the audit details.

Then replace the line `auth = auth_by_action.get(action.id) if group in ("sent", "uncertain", "failed") else None` with `auth = auth_by_action.get(action.id)` (the two lookups already chose the groups). An action with no matching audit row keeps `authorization=None`. Never build an authorization without its audit row.

**Check:** `… uv run pytest tests/integration/test_activity.py -q` → all pass (M04 behaviour unchanged).

**A3. `render_activity` (`telegram/control/activity.py`).** Add, above `render_activity`:

```python
def _delegation_suffix(entry: ActivityEntry, tz: ZoneInfo) -> str:
    """The "automatic reply under delegation #N" suffix, or "" for other entries.

    Never contains "sent": it is appended to scheduled lines too (RG-20)."""
    auth = entry.authorization
    if auth is not None and auth.kind == "delegation":
        at = _format_time(auth.approved_at, tz)
        if auth.delegation_id is None:
            return f" — automatic reply under a delegation that was later deleted, allowed by its rules at {at}"
        return f" — automatic reply under delegation #{auth.delegation_id}, allowed by its rules at {at}"
    if entry.origin == "delegation_auto" and entry.group in ("sent", "uncertain", "failed"):
        return " — automatic reply; its authorization record was not found"
    return ""
```

(Wrap the two long f-strings over two lines each to stay within 100 characters.) Then, in `render_activity`:
- **`sent` branch:** the existing `if entry.authorization is not None:` becomes `if entry.authorization is not None and entry.authorization.kind == "owner_approval":` (its body, the "approved by you at … (version …)" text, is unchanged). Directly after that `if` block, before the deletion check, add `label += _delegation_suffix(entry, tz)`.
- **`awaiting`, `uncertain` and `failed` branches:** after the line that sets `label`, add `label += _delegation_suffix(entry, tz)`.
- The `not_sent` and `blocked` branches are unchanged.

So: an owner-approved send keeps "✓ Sent 13:05 — approved by you at 13:04 (version 2)"; an automatic reply reads "✓ Sent 14:03 — automatic reply under delegation #12, allowed by its rules at 14:02"; a scheduled one reads "📝 Scheduled — automatic reply under delegation #12, allowed by its rules at 14:02" and contains no "Sent".

**Check:** `… uv run pytest tests/unit/test_activity_render.py -q` → all pass unchanged.

**A4. `get_activity` (`agent/tools/activity.py`).**
- `STATUS_RULE` becomes: `"Report each entry's group exactly; never call a draft or an uncertain send 'sent'. An entry with origin 'delegation_auto' or authorized.kind 'delegation' is an automatic reply under that delegation, never one the owner approved."`
- In each `rendered_entries` dict, add `"origin": entry.origin,` after `"group"`; and the `"authorized"` dict becomes `{"kind": entry.authorization.kind, "approved_at": local_time(entry.authorization.approved_at, tz), "version": entry.authorization.version, "delegation_id": entry.authorization.delegation_id}` (still `None` when there is no authorization).

**Check:** `… uv run pytest tests/integration/test_activity.py tests/scenarios/test_m04_j10_activity.py -q` → all pass unchanged.

**A5. Red tests, then green.** Write §7.1 and §7.2 first and run them before A1–A4 (they fail on `kind="delegation"` or on the missing suffix, never with an `ImportError` once F0-S is on your base). Then A1–A4. **Check:** `… uv run pytest tests/unit/test_activity_render_delegation.py tests/integration/test_activity_delegation.py -q` → all pass.

**A6. Static checks.** `… uv run ruff check src/assistant/actions/activity.py src/assistant/telegram/control/activity.py src/assistant/agent/tools/activity.py tests/unit/test_activity_render_delegation.py tests/integration/test_activity_delegation.py`; the same with `uv run ruff format --check`; `… uv run mypy src/assistant/actions/activity.py src/assistant/telegram/control/activity.py src/assistant/agent/tools/activity.py`; `… uv run lint-imports`. **Check:** all clean.

**A7. Diff and PR.** `git diff --stat origin/int/p3...HEAD` → exactly the five Part A files of §3. Commit the red tests, then the green code (`git commit -F <file>`), open the PR (§12).

### 6.2 Part B — the status blocks

**B1. The module API (`src/assistant/observability/status_p3.py`).**

```python
"""P3 blocks of the visibility report (STAT-ACT; FR-ONB-08; P3-interfaces §12.6).

Content-free like the rest of /status: counts, ids, kinds, states and
owner-local times only; never a contact name, a message or a delegation
topic. Each loader reads in the caller's transaction and never swallows an
error. render_status escapes every line once, so lines are plain text."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account
from assistant.actions.autonomy import paused_by_takeover
from assistant.actions.config import AutonomyConfig
from assistant.actions.models import Action
from assistant.automation import delegations_repo
from assistant.automation.delegation_models import Delegation
from assistant.automation.models import Automation
from assistant.core.autonomy_texts import GateLanguage
from assistant.core.flags import FOLLOWUPS_RUN, REMINDERS_RUN, SCHEDULED_SUMMARIES_RUN
from assistant.observability.status import StatusBlock, StatusBlockLoader

DELEGATIONS_KEY: Final = "delegations"
TAKEOVER_PAUSES_KEY: Final = "takeover_pauses"
AUTONOMY_LANGUAGES_KEY: Final = "autonomy_languages"
AUTOMATIONS_KEY: Final = "automations"
MAX_LISTED_DELEGATIONS: Final = 5
#: Kinds the automations block counts. `digest` has its own setting and
#: `delegation_end` is internal (the delegations block shows the end time).
SHOWN_AUTOMATION_KINDS: Final = ("reminder", "follow_up", "scheduled_summary", "monitor")
AUTOMATION_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "reminder": "reminders",
        "follow_up": "follow-ups",
        "scheduled_summary": "scheduled summaries",
        "monitor": "monitors",
    }
)
#: The P3 kinds whose firing needs a flag; monitors are M05 and not shown as off.
AUTOMATION_FLAGS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "reminder": REMINDERS_RUN,
        "follow_up": FOLLOWUPS_RUN,
        "scheduled_summary": SCHEDULED_SUMMARIES_RUN,
    }
)


class P3StatusBlocks:
    def __init__(self, *, config: AutonomyConfig, enabled_flags: frozenset[str]) -> None: ...
    def loaders(
        self,
    ) -> tuple[
        StatusBlockLoader, ...
    ]: ...  # (delegations, takeover_pauses, autonomy_languages, automations)
    async def delegations(
        self, session: AsyncSession, account_id: int, now: datetime
    ) -> StatusBlock | None: ...
    async def takeover_pauses(
        self, session: AsyncSession, account_id: int, now: datetime
    ) -> StatusBlock: ...
    async def autonomy_languages(
        self, session: AsyncSession, account_id: int, now: datetime
    ) -> StatusBlock: ...
    async def automations(
        self, session: AsyncSession, account_id: int, now: datetime
    ) -> StatusBlock: ...
```

These names are binding (the wiring and the tests use them). Each method has exactly the `StatusBlockLoader` shape `(session, account_id, now)`. Every query carries `account_id` (invariant 12, RG-21). A private helper `_local(moment: datetime, timezone: str) -> str` returns `moment.astimezone(zone).strftime("%Y-%m-%d %H:%M")`, where `zone` is `ZoneInfo(timezone)`, or `ZoneInfo("UTC")` when that raises `ZoneInfoNotFoundError` or `ValueError` (catch exactly those two).

**B2. `delegations`.**
1. `row = (await session.execute(select(Account.timezone, Account.autonomy_paused).where(Account.id == account_id))).one_or_none()`; `None` ⇒ return `None` (no account, nothing to show).
2. `mode`: `"off"` when `not config.run_enabled`; else `"paused"` when `autonomy_paused`; else `"shadow"` when `not config.send_enabled`; else `"on"`.
3. `active = await delegations_repo.list_active(session, account_id=account_id, now=now)` (it already excludes not-yet-started, past-end and invalid rows).
4. `starting_later` = `count(*)` of `Delegation` rows with `account_id`, `status == "active"`, `starts_at > now`.
5. `scheduled_replies` = `count(*)` of `Action` rows with `account_id`, `origin == "delegation_auto"`, `state == "scheduled"`.
6. `lines`, in order:
   - mode line: `"off"` → `Automatic replies: off on this server — delegations send nothing`; `"paused"` → `Automatic replies: paused for your account — send /resume to turn them on`; `"shadow"` → `Automatic replies: drafts only — each automatic reply comes to you as a draft instead`; `"on"` → `Automatic replies: on`.
   - count line: both zero → `Delegations: none`; else `Delegations: {n} active`, plus `, {k} starting later` when `k > 0` (so `Delegations: 0 active, 1 starting later` is possible).
   - for each of the first `MAX_LISTED_DELEGATIONS` active views, in `list_active` order: time-bounded → `  #{id}: {c} chat(s), until {_local(ends_at, tz)}`; standing → `  #{id} (standing): {c} chat(s), no end time`; `c = len(view.conversation_ids)` (live chats).
   - when `n > MAX_LISTED_DELEGATIONS`: `  …and {n - 5} more (ask me to list your delegations)`.
   - when `scheduled_replies > 0`: `  Automatic replies scheduled: {s} — each is checked again before it goes out`.
7. `data`: `{"automatic_replies": mode, "active": n, "starting_later": k, "scheduled_replies": s, "timezone": tz, "delegations": [{"delegation_id": v.delegation_id, "kind": v.kind, "ends": None if v.ends_at is None else _local(v.ends_at, tz), "chats": len(v.conversation_ids)} for v in active]}` (every active delegation, not capped; JSON-ready: strings, ints, lists, `None`).
8. Return `StatusBlock(key=DELEGATIONS_KEY, lines=tuple(lines), data=data)`.

**B3. `takeover_pauses`** (OD-5). `ids = await paused_by_takeover(session, account_id=account_id, now=now)`. `lines` = `()` when empty, else `(f"Automatic replies paused in {len(ids)} delegated chat(s) because you wrote there yourself — tap Resume here on the notice to restart them",)`. `data = {"count": len(ids), "conversation_ids": list(ids)}`. The rule that decides which chats count is M6-TAKE's (`takeover_counts`); never re-implement it here.

**B4. `autonomy_languages`** (OD-14). No query. `languages = [g.value for g in GateLanguage if g in config.languages]` (enum order: uz-Latn, uz-Cyrl, ru, en). Line: `Automatic-reply languages: {", ".join(languages)}` or `Automatic-reply languages: none` when empty. `data = {"languages": languages}`.

**B5. `automations`.** One query: `select(Automation.kind, Automation.status, func.count())` with `account_id`, `kind.in_(SHOWN_AUTOMATION_KINDS)`, `status.in_(("active", "paused"))`, grouped by kind and status. `data` = for every kind in `SHOWN_AUTOMATION_KINDS` (zeros included): `{kind: {"active": a, "paused": p}}`, plus `"switched_off": [kind for kind in SHOWN_AUTOMATION_KINDS if kind in AUTOMATION_FLAGS and AUTOMATION_FLAGS[kind] not in enabled_flags and (a + p) > 0]`. Lines:
- all counts zero → `Automations: none`.
- else `Automations on: ` + `" · ".join(f"{AUTOMATION_LABELS[k]} {active[k]}" + (" (switched off on this server)" if k in switched_off else "") for k in SHOWN_AUTOMATION_KINDS)`, then, when the paused total is above zero, a second line `Automations paused: {total_paused}`.

**B6. `loaders()`** returns `(self.delegations, self.takeover_pauses, self.autonomy_languages, self.automations)` in that order. If `actions/autonomy.py::paused_by_takeover` is not on `origin/int/p3` when you start Part B (M6-TAKE not merged yet), build everything else first **without** the `paused_by_takeover` import, the `takeover_pauses` method, its entry in `loaders()` and its two tests (the module must import cleanly); push, keep the PR a draft, comment on the STAT-ACT issue, and go on to your next unit. When M6-TAKE merges, `git rebase origin/int/p3`, add the import, B3, the `loaders()` entry and the two tests, and mark the PR ready. Do not write a substitute rule.

**B7. Wiring (`_wire_surfaces` in `bootstrap.py`; function-local imports only).**

```python
# --- STAT-ACT: P3 status blocks (P3-interfaces §12.6) ---
from assistant.observability.status_p3 import P3StatusBlocks

status_blocks = P3StatusBlocks(config=ctx.autonomy_config, enabled_flags=ctx.settings.feature_flags)
wiring = replace(wiring, status_blocks=(*wiring.status_blocks, *status_blocks.loaders()))
```

Convention for several David units in one function (the same text is in M6-CATCH and M7-MRV): if `_wire_surfaces` still ends with `return P3Wiring()`, change that to `wiring = P3Wiring()` at the top of the body, your block, and `return wiring` at the end; if another David unit already did so, add your block just before `return wiring`. If the function builds its return value another way (for example `return P3Wiring(routes=ControlRoutes(...))` with local accumulators, SET-P3's shape), keep every statement and keyword already there, drop the `wiring = replace(...)` line of your block, and add exactly one keyword line to the returned `P3Wiring(...)`: `status_blocks=tuple(status_blocks.loaders()),` (stop and ask if `status_blocks=` is already set there). Use `dataclasses.replace` the way the module already imports it, or a function-local `from dataclasses import replace` when it does not. The blocks are registered whatever the flags: `/status` must say "off" while a flag is off (truthfulness), and nothing here changes behaviour.

**B8. ARCHITECTURE §17** (the "Status" bullet; append, as built): "**P3 (STAT-ACT):** `observability/status_p3.py::P3StatusBlocks`, registered in `_wire_surfaces`, adds four content-free blocks after the M05 lines and under `get_status`'s `blocks`: `delegations` (automatic-reply mode off/paused/drafts only/on, active delegations with owner-local end and live chat count, delegations starting later, scheduled automatic replies, never shown as sent), `takeover_pauses` (chats paused by a counting takeover, M6-TAKE's `paused_by_takeover`), `autonomy_languages` (`AUTONOMY_LANGUAGES`, "none" when empty) and `automations` (reminders, follow-ups, scheduled summaries and monitors on or paused, with the P3 kinds whose flag is off marked). `/activity` and `get_activity` name the delegation of every automatic reply (`Authorization.kind = "delegation"`, from the `action_auto_approved` audit row)."

**B9. Steps and checks.**
1. **Branch.** `git fetch origin`; `git switch -c p3/stat-act-status origin/int/p3`. **Check:** `… uv run python -c "from assistant.observability.status import StatusBlock, StatusBlockLoader; from assistant.automation.delegations_repo import list_active; print('ok')"` prints `ok`. If it fails, F0-R or F0-S is not on your base: stop and ask.
2. **Red tests.** Create `tests/integration/test_status_p3.py` with §7.3 and the module with the API of B1 and each method body `raise NotImplementedError`. **Check:** `… uv run pytest tests/integration/test_status_p3.py -q` → the tests fail with `NotImplementedError` or assertion errors, none with `ImportError` (in B6's case the takeover import, method and tests are left out until the rebase).
3. **B2.** **Check:** `… -k "delegations_block"` → pass.
4. **B4, B5.** **Check:** `… -k "languages or automations"` → pass.
5. **B3, B6** (after M6-TAKE is on your base). **Check:** `… -k "takeover"` → pass.
6. **Parity and RG-21.** **Check:** the whole file passes.
7. **B7, B8, hygiene (§7.4).** **Check:** `… uv run pytest tests/integration/test_status_p3.py tests/integration/test_log_hygiene_stat_act.py -q` → pass; F0-R's `… uv run pytest tests/integration/test_p3_wiring.py tests/integration/test_control_hooks.py -q` → pass (or §5's one append).
8. **Static checks** on `src/assistant/observability/status_p3.py`, `src/assistant/bootstrap.py` and the two test files (ruff, format, mypy, lint-imports). **Check:** all clean.
9. **Diff.** `git diff --stat origin/int/p3...HEAD` → exactly the five Part B files of §3 (plus §5's separate commit, if any).

## 7. Named tests (red first)

Synthetic data only. Shared values: `T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)` (14:00 in Asia/Tashkent, UTC+5); accounts are `seed_account(..., timezone="Asia/Tashkent")`; delegations come from `p3_factories.seed_delegation(session, account_id=…, conversation_ids=[…], kind=…, status="active", starts_at=…, ends_at=…, confirmed_at=…, spec=…)` with `spec` a valid `DelegationRules(...).model_dump(mode="json")` (a small `_rules(conversation_ids)` helper in the test file: `allowed=(Behaviour.ASK_FOR_DETAILS,)`, `limits=DelegationLimits(per_conversation_hour=5, per_delegation=30)`); automatic replies come from `p3_factories.seed_autonomous_action(session, account_id=…, conversation_id=…, delegation_id=…, state=…)`; an auto-approval is an `AuditLogEntry(account_id=…, actor="system", event="action_auto_approved", subject_type="action", subject_id=<action id>, details={"from": "proposed", "to": "approved", "version": 1, "reason": None, "delegation_id": <id>}, at=<time>)`.

### 7.1 `tests/unit/test_activity_render_delegation.py` (Part A)

Use an `_entry(...)` helper like `tests/unit/test_activity_render.py`'s, with `origin` as a parameter.
- `test_an_automatic_reply_names_its_delegation_and_never_says_approved_by_you` — `succeeded`/`sent`, origin `delegation_auto`, `executed_at` 13:05 UTC, `Authorization("delegation", 13:04 UTC, 1, delegation_id=12)`, `timezone="UTC"` → the text contains `✓ Sent 13:05 — automatic reply under delegation #12, allowed by its rules at 13:04` and not `approved by you`.
- `test_an_owner_approved_send_still_says_approved_by_you` — `Authorization("owner_approval", 13:04, 2)` → contains `approved by you at 13:04 (version 2)` and not `automatic reply`.
- `test_rg20_a_scheduled_automatic_reply_is_never_shown_as_sent` — state `scheduled`, group `awaiting`, origin `delegation_auto`, delegation authorization #12 → the entry's line contains `📝 Scheduled — automatic reply under delegation #12` and the word `Sent` (any case) does not appear anywhere in the output.
- `test_uncertain_and_failed_automatic_replies_name_their_delegation` — `delivery_uncertain`/`uncertain` → contains `⚠ Delivery uncertain — please check the chat — automatic reply under delegation #12`; `failed`/`failed` → contains `✗ Not sent — delivery failed — automatic reply under delegation #12`; neither line contains `✓ Sent`.
- `test_an_automatic_reply_whose_delegation_was_deleted_says_so` — `delegation_id=None` → contains `automatic reply under a delegation that was later deleted`.
- `test_an_automatic_reply_without_an_authorization_record_says_so` — origin `delegation_auto`, `sent`, `auth=None` → contains `automatic reply; its authorization record was not found`.
- `test_an_owner_draft_awaiting_approval_gets_no_suffix` — origin `agent_proposal`, `awaiting_approval`, no auth → the line is exactly `📝 Draft — awaiting your approval` (no ` — automatic`).
- `test_render_activity_escapes_untrusted_text_once` — GW-ESC §3.3 rule 6 (final text, TL ruling R39): `NAME = 'A&B <b>x</b> <a href="https://example.invalid/">y</a> &amp;'` as the `display_name` of one delegation entry (`sent`, with a delegation authorization); pass `render_activity(...)` through `telegram.render.sanitize` (import it in the test only). The output contains `A&amp;B`, `&lt;b&gt;x&lt;/b&gt;`, `&lt;a href=` and `&amp;amp;` exactly once each, and no raw `<b>x` and no `<a ` anywhere.

### 7.2 `tests/integration/test_activity_delegation.py` (Part A)

Use the `env` fixture for every test here: seed inside `async with env.factory() as session:` and `await session.commit()`; call `activity()` in a fresh `env.factory()` session with `since=T0 - 1 h`, `until=T0 + 1 h`; call `get_activity_tool` with `ToolContext(account_id=…, run_id=1, run_type="owner_request", tainted=False, services=ToolServices(session_factory=env.factory, clock=FakeClock(T0 + timedelta(hours=1)), monthly_budget_usd=None))` and all-null `GetActivityArgs` unless a test says otherwise (the default period, "today" in Asia/Tashkent, then covers `T0` to `T0 + 1 h`). Every seeded action's times lie between `T0` and `T0 + 50 min`.

- `test_s04_5_every_autonomous_send_shows_its_delegation` — the S04.5 completeness test extended (J10, NFR-06): one delegation; three automatic replies in three chats (`succeeded`, `delivery_uncertain`, `failed`), each with its `action_auto_approved` row; one owner-approved `succeeded` `agent_proposal` with its `action_approved` row → `activity()` gives the three automatic replies `Authorization(kind="delegation", approved_at=<its audit at>, version=1, delegation_id=<id>)` and the owner one `kind="owner_approval"` with `delegation_id=None`; `render_activity` names `delegation #<id>` three times and `approved by you` once; `get_activity_tool` returns, for each automatic reply, `origin == "delegation_auto"` and `authorized == {"kind": "delegation", "approved_at": <local>, "version": 1, "delegation_id": <id>}`, and for the owner one `authorized["kind"] == "owner_approval"`.
- `test_rg20_get_activity_never_groups_a_scheduled_automatic_reply_as_sent` — one `scheduled` automatic reply with its auto-approval → `get_activity_tool(show="sent")` returns no entry; `show=None` returns it with `group == "awaiting"` and `authorized["kind"] == "delegation"`; `outcome.data["status_rule"]` contains `never one the owner approved`.
- `test_a_delegation_authorization_needs_its_auto_approval_row` — an automatic `succeeded` reply with **no** `action_auto_approved` row, and one with only an `action_approved` row → both have `authorization is None` (an owner approval is never attributed to an automatic reply).
- `test_the_delegation_id_comes_from_the_action_and_is_none_after_the_delegation_is_deleted` — seed the automatic `succeeded` reply with a real delegation, its auto-approval row with `"delegation_id": 99` in the details, then `UPDATE app.actions SET delegation_id = NULL WHERE id = :a` (what the `ON DELETE SET NULL` leaves after a delegation row is deleted) → `authorization.kind == "delegation"` and `authorization.delegation_id is None`; `render_activity` shows `automatic reply under a delegation that was later deleted`.
- `test_rg21_activity_never_reads_another_accounts_auto_approval` — account B's `action_auto_approved` row whose `subject_id` is account A's action id → A's entry has `authorization is None`.

### 7.3 `tests/integration/test_status_p3.py` (Part B; `db_session`; a `_config(run: bool, send: bool, languages: frozenset[GateLanguage] = frozenset())` helper builds `AutonomyConfig` with the contract defaults: 45 s, 20 s, 30 min, 120 s, 5, 30, 3, 10 min, 30 s, `standing_enabled=False`; `now = T0 + 1 h` and a delegation is `kind="time_bounded"`, `starts_at=T0`, `ends_at=T0 + 5 h`, `confirmed_at=T0` unless a test says otherwise; each delegation covers its own chats, because one live delegation per chat is a database rule)

- `test_delegations_block_lists_active_delegations_with_owner_local_end` — two active delegations, both `starts_at=T0`, `confirmed_at=T0`, A seeded first: #A time-bounded over 2 chats, `ends_at=T0 + 5 h`; #B standing (`ends_at=None`) over 1 other chat; `now = T0 + 1 h`; `_config(run=True, send=True)` → `lines == ("Automatic replies: on", "Delegations: 2 active", f"  #{A}: 2 chat(s), until 2026-11-02 19:00", f"  #{B} (standing): 1 chat(s), no end time")` (A before B: `list_active` order) and `data["delegations"][0] == {"delegation_id": A, "kind": "time_bounded", "ends": "2026-11-02 19:00", "chats": 2}`.
- `test_delegations_block_counts_a_delegation_that_starts_later` — one with `starts_at = now + 2 h` → `Delegations: 0 active, 1 starting later`; `data["starting_later"] == 1`.
- `test_delegations_block_leaves_out_ended_and_past_end_delegations` — one ended through `end_delegations(..., reason="owner_ended")` and one still `active` with `ends_at = T0 + 30 min` (before `now`) → `Delegations: none`, `data["active"] == 0`.
- `test_delegations_block_states_the_automatic_reply_mode` — parametrized: `(run=False, send=True)` → `Automatic replies: off on this server — delegations send nothing`; `(True, False)` → `Automatic replies: drafts only — each automatic reply comes to you as a draft instead`; account `autonomy_paused=True` with `(True, True)` → `Automatic replies: paused for your account — send /resume to turn them on`; `(True, True)` → `Automatic replies: on`; `data["automatic_replies"]` is `off`, `shadow`, `paused`, `on`.
- `test_delegations_block_caps_the_list_and_keeps_the_true_count` — seven active delegations → `Delegations: 7 active`, five `  #` lines, then `  …and 2 more (ask me to list your delegations)`; `len(data["delegations"]) == 7`.
- `test_rg20_scheduled_automatic_replies_are_counted_as_scheduled_never_as_sent` — one active delegation over two chats, one `scheduled` automatic reply in each chat (one in-flight autonomous action per chat is the database rule) → the line `  Automatic replies scheduled: 2 — each is checked again before it goes out`; `data["scheduled_replies"] == 2`; no block line contains `sent` (lower-cased check).
- `test_takeover_pauses_block_lists_chats_paused_by_a_counting_takeover` (OD-5) — one active delegation over chats C1, C2, C3 (`confirmed_at = T0`): C1 `takeover_at = T0 + 10 min`; C2 `takeover_at = T0 + 10 min` and `autonomy_resumed_at = T0 + 20 min`; C3 `takeover_at = T0 - 1 h` → `data == {"count": 1, "conversation_ids": [C1]}` and `lines == ("Automatic replies paused in 1 delegated chat(s) because you wrote there yourself — tap Resume here on the notice to restart them",)`.
- `test_takeover_pauses_block_has_no_line_when_nothing_is_paused` — `lines == ()`, `data == {"count": 0, "conversation_ids": []}`.
- `test_autonomy_languages_block_lists_enabled_languages_in_gate_order_or_none` — `{GateLanguage.EN, GateLanguage.UZ_LATN}` → `("Automatic-reply languages: uz-Latn, en",)`, `data == {"languages": ["uz-Latn", "en"]}`; empty → `("Automatic-reply languages: none",)`.
- `test_automations_block_counts_by_kind_and_status` — seeded with `p2_factories.seed_automation`: 2 active reminders, 1 paused reminder, 1 active follow-up, 1 active monitor, plus rows that must not count: a `digest`, a `delegation_end`, a `cancelled` reminder, a `completed` follow-up; `enabled_flags = frozenset({REMINDERS_RUN, FOLLOWUPS_RUN, SCHEDULED_SUMMARIES_RUN})` → `lines == ("Automations on: reminders 2 · follow-ups 1 · scheduled summaries 0 · monitors 1", "Automations paused: 1")`, `data["reminder"] == {"active": 2, "paused": 1}`, `data["switched_off"] == []`.
- `test_automations_block_marks_a_kind_whose_flag_is_off` — 1 active reminder, `enabled_flags = frozenset()` → the line contains `reminders 1 (switched off on this server)`; `data["switched_off"] == ["reminder"]`; follow-ups and scheduled summaries (zero rows) are not marked.
- `test_automations_block_says_none_when_there_are_none` — `("Automations: none",)`.
- `test_rg21_status_blocks_never_count_another_accounts_rows` — account B has an active delegation, a scheduled automatic reply, a counting takeover and two automations; every block for account A shows none / zero.
- `test_status_and_get_status_show_the_same_p3_blocks` — `build_status(db_session, account, now, monthly_budget_usd=None, block_loaders=P3StatusBlocks(...).loaders())`; `render_status(report, visibility="synthetic")` contains every block line in loader order after the M05 lines; `report.as_dict()["blocks"]` has exactly the four keys and each equals the block's `data`.
- `test_status_blocks_carry_no_names_topics_or_text` — a contact named `CANARY-SYNTH-P3 Name`, a delegation topic `CANARY-SYNTH-P3 topic`, a message with `CANARY-SYNTH-P3 text` → no block line and no `data` value (as `json.dumps`) contains `CANARY-SYNTH-P3`; no line contains `&`, `<` or `>` (application text only, so `render_status`'s single escape can never double-escape).

### 7.4 `tests/integration/test_log_hygiene_stat_act.py` (Part B)

- `test_status_blocks_log_nothing_with_content` — inside `capture_logs()` (F0-E's `tests/log_hygiene.py`), run all four loaders over the canary data of §7.3's last test; `assert_content_free(output, CANARY)`.

You may add tests; never fewer, never with changed expected values.

## 8. Truthfulness edge cases

- **An automatic reply is never "approved by you".** The owner text appears only for `kind == "owner_approval"` (§7.1 first test).
- **Scheduled is never sent (RG-20).** The delegation suffix contains no "sent"; a scheduled reply stays group `awaiting` ("📝 Scheduled"); `/status` counts it as "scheduled — checked again before it goes out", and `ActionsStatus` (M04) does not count it as sending either.
- **Uncertain stays uncertain.** The suffix is appended after "Delivery uncertain — please check the chat", never instead of it.
- **No record, no claim.** An automatic reply without its `action_auto_approved` row has no authorization and says "its authorization record was not found" rather than guessing; an owner approval row never becomes a delegation authorization and vice versa.
- **A deleted delegation is named as deleted**, not given an id the owner cannot find.
- **"Off" is shown as off.** With `autonomy.run` off, `/status` says automatic replies are off even when delegations exist; with `autonomy.send` off it says drafts only; with the account paused it says paused; a P3 automation kind whose flag is off is marked.
- **True totals.** The delegation list is capped at five lines but the count and `data` hold every active delegation.
- **No content.** Status blocks never hold a name, topic or message; the takeover block gives a count and ids only.
- **Not started is not active.** A delegation that starts later is counted separately, never as active.

## 9. Acceptance checklist

- [ ] Every test in §7 exists, was seen failing, and passes in CI.
- [ ] Part A: `git diff --stat origin/int/p3...HEAD` lists only the five Part A files. Part B: only the five Part B files (plus §5's separate commit, if any).
- [ ] `tests/unit/test_activity_render.py`, `tests/integration/test_activity.py` and `tests/scenarios/test_m04_j10_activity.py` pass **unchanged**.
- [ ] In `actions/activity.py` only `Authorization`, the two constants, the lookup block and the one `auth = …` line changed.
- [ ] `status_p3.py` holds no copy of the takeover rule; it calls `paused_by_takeover`.
- [ ] Every query is account-scoped; no block holds a name, topic or message.
- [ ] `ruff check`, `ruff format --check`, `mypy` (strict) and `lint-imports` clean on the changed files.
- [ ] No broad `except`; only `ZoneInfoNotFoundError` and `ValueError` caught, in `_local`; no `assert` in production code.
- [ ] The wiring block is inside `_wire_surfaces` with function-local imports; the module import block is unchanged.
- [ ] GW-ESC is closed before either PR merges (WORKFLOW §7: no P3 renderer merges before it).
- [ ] Each PR description has the filled template, the test commands and results, and the open items.

## 10. When to stop and ask

Ask on the STAT-ACT issue, starting the comment with "Question for J1:" (plain text, never an @-mention). If nobody answers within 2 h, David raises it with the TL.

- `Action.delegation_id`, the `action_auto_approved` event, `seed_delegation` or `seed_autonomous_action` is missing on your base (F0-S not merged).
- `StatusBlock`, `build_status(block_loaders=…)`, `P3Wiring.status_blocks`, `ctx.settings.feature_flags` or `ctx.autonomy_config` is missing or named differently (F0-R).
- `paused_by_takeover` exists but has a different signature than `(session, *, account_id, now) -> list[int]`.
- An M04 activity test, a `/status` test or an F0-R test fails and §5 does not name it.
- You think you need to edit `observability/status.py`, `telegram/control/handlers.py`, `agent/tools/status.py` or any file outside §3.
- An expected string in §7 looks wrong: do **not** change it; ask.
- You have spent more than one day on the two parts and are not close.

**Contract gaps already known (the TL tracks them; do not wait on them):** P3-interfaces §15.1 allows STAT-ACT "`Authorization` and one line only" in `actions/activity.py`, but the delegation authorization needs the two constants and the lookup block of §6.1 A2 as well (the allow-list of §3 is exactly that, nothing more); the contract's "one line" in `telegram/control/activity.py` is the helper plus four suffix lines of §6.1 A3; `agent/tools/activity.py` is a named touch point the contract does not list (§6.1 A4); "Scheduled — <owner-local time>" (P3-interfaces §2.1) cannot be shown because `ActivityEntry` has no `scheduled_for` and STAT-ACT may change only `Authorization` (the scheduled line says "📝 Scheduled — automatic reply under delegation #N"); the `takeover_pauses` block depends on M6-TAKE.

## 11. Coding rules (reviewers block on these)

- No broad `except` (no bare `except`, no `except Exception`); catch only the exceptions this spec names.
- No `assert` in production code, neither for control flow nor for type narrowing.
- Full type hints; `mypy --strict` clean; names exactly as §6.
- No `datetime.now()`: loaders get `now`; tests use fixed datetimes and `FakeClock`.
- Logs, if any, carry ids and counts only; no names, topics or text. No exception message built from content.
- No Telegram calls: the blocks return text and data; `/status` and `/activity` send through the existing handlers and the gateway.
- Synthetic test data only (invented names; the canary is `CANARY-SYNTH-P3`).
- GitHub: every PR body and comment through `--body-file <file>` (never an inline body in PowerShell); no bare `@names`; no recurring status comments.
- Commits: Conventional Commits, `git commit -F <file>`; never `--no-verify`; never commit `.env`, `var/` or real data. Out-of-list edits (only §5's) go in a separate last commit with a comment on the issue.

## 12. Pull requests

**PR 1 (Part A).**
- **Title:** `STAT-ACT (1/2): delegation authorization in /activity and get_activity`.
- **Size:** about 400 changed lines or fewer (expected ≈ 60 code + ≈ 250 tests).
- **Labels:** `team:david`, `eng:d1` on the draft; when CI is green, mark it ready and add `needs-review:j1` and `needs-qa`; comment on the issue.
- **Template** (WORKFLOW §4.3): *Unit:* `WU: STAT-ACT · Spec: docs/engineering/p3/specs/STAT-ACT.md · Issue: #<n> · Base: int/p3`; Tier: other; QA lane: after merge. *Safety invariants touched:* 11 (honest status) — `test_rg20_a_scheduled_automatic_reply_is_never_shown_as_sent`, `test_an_automatic_reply_names_its_delegation_and_never_says_approved_by_you`, `test_s04_5_every_autonomous_send_shows_its_delegation`; 12 (tenant scoping) — `test_rg21_activity_never_reads_another_accounts_auto_approval`. *Tests:* the §7.1–§7.2 names; RG-20 extended; the commands you ran and their result, or "not run locally: <reason>; CI is the gate". *Shared lists:* none. *Data:* no migration, no content column. *Prompts, flags and settings:* none (`STATUS_RULE` is a tool-result string, not a prompt). *Docs:* none in this PR (Part B updates ARCHITECTURE §17). *Interface changes:* none beyond the named touch points of §3; out-of-list edits: none. *Rule text for PR-OR10:* "get_activity: `authorized.kind` 'delegation' (and origin 'delegation_auto') means an automatic reply under that delegation, never one the owner approved; relay the group exactly ('awaiting' is not sent)." *Open items for M06.md:* the "Scheduled — <time>" gap (§10).

**PR 2 (Part B).**
- **Title:** `STAT-ACT (2/2): P3 status blocks for /status and get_status`.
- **Size:** about 400 changed lines or fewer (expected ≈ 170 code + ≈ 230 tests).
- **Labels and template** as PR 1, with: *Safety invariants touched:* 11 — `test_rg20_scheduled_automatic_replies_are_counted_as_scheduled_never_as_sent`, `test_delegations_block_states_the_automatic_reply_mode`; 12 — `test_rg21_status_blocks_never_count_another_accounts_rows`. *Data:* logs — `test_log_hygiene_stat_act.py`. *Docs:* ARCHITECTURE §17 (Status bullet). *Shared lists:* §5's append, if made. *Rule text for PR-OR10:* "get_status `blocks`: `delegations` (mode, active delegations, end times, scheduled replies — scheduled is never sent), `takeover_pauses`, `autonomy_languages`, `automations`; relay them as given."
- After merge: QD1 checks both PRs on `int/p3`; a finding becomes one fix PR from you.
