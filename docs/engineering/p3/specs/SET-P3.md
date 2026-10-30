# SET-P3 — `/settings` P3 screens and the conversation panel's Delegate row (junior unit)

| | |
|---|---|
| **Unit** | SET-P3 — `/settings` rows "Delegations…" and "Automations…", the delegations list and detail screens with End (confirm step) through `DelegationControls`, the AI-notice wording view and edit (`deleg_wording` input) through `DelegationControls.set_disclosure_wording`, the automations list with Pause / Resume / Delete (confirm step) through `AutomationControls`, and the conversation panel's Delegate row (J12) |
| **Owner slot** | D1 (David's team, junior) |
| **Mentor** | J3 (Claude; peer review dispatched by the TL), plus a read by E1 (Codex) of the `AutomationControls` side (part B) |
| **QA** | QD1, **after merge** (on `int/p3` before R1; depth: exact keyboards, hidden rows, truthful "off" lines, the confirm steps) |
| **Size** | M (≈ 1 engineer-day, one fix round included) |
| **Depends on** | F0-R: `ControlRoutes.settings_rows` / `panel_rows`, `SettingsRowBuilder`, `PanelRowBuilder`, `Screen.extra_rows`, `core/p3_ports.py`, the rule models in `automation/rules.py`, `_wire_surfaces`. F0-S: `automation/delegation_models.py::Delegation`, `automation/delegations_repo.py` (`get`, `active_covering`, `list_current`: R89), `automation/delegation_rules.py`, `core/autonomy_texts.py`, `actions/disclosure.py::validate_wording`, `actions/config.py::AutonomyConfig`, `core/flags.py`, `automations.paused_reason`, `tests/integration/p3_factories.py`. **GW-ESC** landed in P2 #133 (verification only; no P3 screen merges before that verification is closed, WORKFLOW §7). M7-AUTO, for part B only (R51): `automation/management.py::LISTED_KINDS` (the shared listing rule, no `delegation_end`) and `allowed_ops`; part B is ready after M7-AUTO is on `int/p3` (stack on `p3/m7-auto` if it is not). The two ports may be unwired when you start: every test uses recording fakes; a real-port test skips itself until M6-DELEG (part A) or M7-AUTO's `AutomationManager` (part B) is on your base |
| **Branch → base** | Part A: `p3/set-p3` from `origin/int/p3` → PR to `int/p3`. Part B: `p3/set-p3-auto` from `p3/set-p3` → PR to `p3/set-p3` (retarget to `int/p3` after part A merges; WORKFLOW §4.2). Two PRs keep each near 400 changed lines (§12) |
| **Normative inputs** | [P3-interfaces](../contracts/P3-interfaces.md) §4.5 (ports, "no-op default = hidden"), §5 (disclosure), §6.1 (read seam), §10.2 (flags), §12.2 (row builders), §12.3 (`p3d`, `p3w`, `p3a`), §12.4 (`deleg_wording`), §13 (no SET-P3 audit event), §15.1, §15.3, §15.4; [P3-schema](../contracts/P3-schema.md) §2.1, §3.5; decisions OD-4, OD-7, OD-19 (a hard delete; R51: delegation-end and digest automations are not listed), ADR-17 (the application inserts the AI notice), P3-T19, P3-T26, P3-T36; [specs/GW-ESC.md](GW-ESC.md) §3.3 (escape-once rule); [specs/M6-DELEG.md](M6-DELEG.md) §3.4–§3.5 (what the delegation port returns); [specs/M7-AUTO.md](M7-AUTO.md) §3.1–§3.2 (what the automation port returns); PRD FR-CTL-10, J12; IMPLEMENTATION_PLAN S06.1 ("/settings: active and standing delegations with End; disclosure wording"), S07.3 ("/settings: automations list, pause, delete") |

## 1. Goal

The owner can see and stop what acts for them without asking the model. `/settings` gains two rows: **Delegations (n)…** lists the delegations that are active or about to start, shows each one's rules, ends one after a confirm step, and shows and edits the AI-notice line per language (never empty; the AI marker is always added in front, OD-4). **Automations (n)…** lists reminders, scheduled summaries, monitors and follow-ups that are on or paused, pauses or resumes them at once, and deletes one after a confirm step (OD-19: a hard delete). A chat covered by a delegation shows a **Delegate row** on its conversation panel with View and End. Everything is deterministic, works with the model down, and is truthful: a row whose port is not wired is hidden; a feature switched off says "off"; an outcome is never reported as done unless the port said `done`.

SET-P3 writes nothing itself. Every change goes through a port whose owner validates it and writes the audit row (P3-interfaces §13: SET-P3 has no audit event).

## 2. Read first

1. [handoffs/junior-engineers.md](../handoffs/junior-engineers.md), all of it.
2. [WORKFLOW](../WORKFLOW.md) §2, §3, §4 (PR template), §5, §6.2, §6.5 ("Keyboards", "Escape once", "Wire functions").
3. `src/assistant/telegram/control/panels.py`: `Screen`, `_screen`, `_after_press`, `settings_screen`, `panel_screen`, `handle_press`, `_press_list` (how a list press sends a new message), and how F0-R appends `extra_rows`.
4. `src/assistant/telegram/control/monitors.py::handle_mon_press`: the press pattern you copy (claim the card in the press transaction, answer and edit after the commit through the gateway).
5. `src/assistant/accounts/service.py`: `create_card`, `claim_card`, `set_pending_input`, `clear_pending_input`; `src/assistant/accounts/callbacks.py::encode`.
6. `src/assistant/telegram/control/dispatcher.py`: precedence rules 1–3 (a command cancels a pending text question; a forwarded or quoted message is never an answer).
7. `src/assistant/telegram/control/routes.py` after F0-R: `CallbackHandler`, `PendingInputHandler`, `SettingsRowBuilder`, `PanelRowBuilder`.
8. `src/assistant/core/p3_ports.py` (F0-R) and `src/assistant/automation/delegations_repo.py` (F0-S).
9. `src/assistant/automation/monitors.py`: `parse_rule`, `describe` (plain text; you escape it).
10. `tests/integration/test_settings_m05_panels.py` and `tests/integration/test_monitor_tools.py`: how tests build `environment(..., routes=...)`, press cards with `callback(...)` and read `env.sender.sends`, `.edits`, `.answered_callbacks`.
11. This spec, to the end.

## 3. Allowed files

| Path | New or existing | Exactly what may change |
|---|---|---|
| `src/assistant/telegram/control/settings_p3.py` | new | The whole module (§6.1–§6.8) |
| `src/assistant/bootstrap.py` | existing | Inside `_wire_surfaces` only: the SET-P3 block of §6.9 (and the two shared lines `wiring = P3Wiring()` / `return wiring` if no David unit added them yet). Function-local imports only; the module import block is frozen |
| `docs/ARCHITECTURE.md` | existing | §6.7 only: one new bullet "**/settings P3 screens and the Delegate row (as built, SET-P3).**" right after the existing "/settings (as built, S03.3/S03.5; C4)" bullet (§6.10). Your mentor checks the wording |
| `tests/unit/test_settings_p3_rules.py` | new | §7.1 |
| `tests/integration/test_settings_p3.py` | new | §7.2 (part A) |
| `tests/integration/test_settings_p3_automations.py` | new | §7.3 (part B) |
| `tests/integration/test_log_hygiene_set_p3.py` | new | §7.4, copied from F0-E's template `tests/unit/test_log_hygiene_f0e.py` |

## 4. Forbidden

- `src/assistant/policy/`, `src/assistant/actions/` (you **import** `actions.disclosure` and `actions.config`, never edit them), `src/assistant/telegram/gateway.py`, `src/assistant/telegram/render.py`, `src/assistant/telegram/ingress/`, `migrations/`, `src/assistant/agent/` (including `prompts/` and `prompt_registry.py`), `src/assistant/privacy/`, consent code (`src/assistant/llm/consent.py`), `src/assistant/config.py`, `src/assistant/core/` (including `core/p3_ports.py` and `core/flags.py`: read only), `src/assistant/jobs/`, `AGENTS.md`.
- `src/assistant/telegram/control/{dispatcher,routes,handlers,panels,cards}.py` (frozen after F0-R; your screen lives in the new module and plugs in through `ControlRoutes`).
- `src/assistant/automation/*` (including `delegations.py` of M6-DELEG and `management.py` of M7-AUTO: you call them only through the ports), `src/assistant/accounts/*`, `src/assistant/inbox/*`, `src/assistant/memory/*`, `src/assistant/conversations/*`.
- `tests/integration/test_conversation_modes.py` and `tests/integration/test_settings_m05_panels.py` (frozen after F0-R; you may **import** their helpers, never edit them), `tests/fakes/*`, `tests/integration/{p2_factories,p3_factories,business_env,agent_env}.py`.
- Every other unit's files, `pyproject.toml`, `uv.lock` (no new dependency), `.env*`, `var/`.
- **Every file not in §3.**

## 5. Shared lists you must update

None. The callback kinds `p3d`, `p3w`, `p3a` and the pending-input kinds of §6.1 are registered only through your `_wire_surfaces` block; F0-R's fragment merge raises at startup on a duplicate, so no list needs a hand edit. The P2 keyboard tests are per builder since F0-R: you do not edit them; your rows are asserted in your own test files (P3-interfaces §15.1).

## 6. Step-by-step

### 6.1 Module constants and the pure rules (`src/assistant/telegram/control/settings_p3.py`)

```python
"""/settings P3 screens and the conversation panel's Delegate row (P3 SET-P3).

FR-CTL-10, J12, OD-4, OD-7, OD-19. Deterministic: no model. Every button is a
card (`pending_inputs`, single-use token, scoped to the pressing owner's
account). Changes go only through the P3 ports (`DelegationControls`,
`AutomationControls`): a row whose port is not wired is hidden, and a press
that reaches an unwired port changes nothing. Database work runs in the
press transaction; every Telegram call (answer, edit, send) runs after the
commit through the gateway. Untrusted text (contact names, topics, AI-notice
wording, reminder texts, monitor descriptions) is escaped exactly once with
`telegram.render.escape` (GW-ESC rule)."""

P3D_CARD: Final = "p3d"  # delegation rows and the panel Delegate row (P3-interfaces §12.3)
P3W_CARD: Final = "p3w"  # AI-notice wording
P3A_CARD: Final = "p3a"  # automation rows

OP_LIST: Final = "ls"
OP_VIEW: Final = "vw"
OP_END: Final = "end"
OP_END_OK: Final = "eok"
OP_END_CANCEL: Final = "ecx"
OP_PAUSE: Final = "pa"
OP_RESUME: Final = "rs"
OP_DELETE: Final = "dl"
OP_DELETE_OK: Final = "dok"
OP_DELETE_CANCEL: Final = "dcx"

#: `ed` (P3-interfaces §12.3) carries its language in the op, because a card and a
#: pending input have nowhere else to keep it (contract gap reported, §10).
WORDING_EDIT_OPS: Final[Mapping[str, GateLanguage]] = MappingProxyType(
    {
        "ed_ul": GateLanguage.UZ_LATN,
        "ed_uc": GateLanguage.UZ_CYRL,
        "ed_ru": GateLanguage.RU,
        "ed_en": GateLanguage.EN,
    }
)
#: `deleg_wording` (P3-interfaces §12.4), one kind per language for the same reason;
#: `ref_id` = the delegation id.
WORDING_INPUT_KINDS: Final[Mapping[str, GateLanguage]] = MappingProxyType(
    {
        "deleg_wording_ul": GateLanguage.UZ_LATN,
        "deleg_wording_uc": GateLanguage.UZ_CYRL,
        "deleg_wording_ru": GateLanguage.RU,
        "deleg_wording_en": GateLanguage.EN,
    }
)
LOCALE_LABELS: Final[Mapping[GateLanguage, str]] = MappingProxyType(
    {
        GateLanguage.UZ_LATN: "Uzbek (Latin)",
        GateLanguage.UZ_CYRL: "Uzbek (Cyrillic)",
        GateLanguage.RU: "Russian",
        GateLanguage.EN: "English",
    }
)
BEHAVIOUR_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "answer_from_knowledge": "answer from your notes",
        "ask_for_details": "ask for missing details",
        "acknowledge_and_defer": "say you will follow up",
        "decline": "politely decline",
    }
)
#: R51: the listing rule is M7-AUTO's (`management.LISTED_KINDS`: every kind except
#: `delegation_end`, whose rows are managed through the delegation); the digest (managed under
#: Alerts & digest) is left out as well. Neither is listed, counted or acted on here.
#: `from assistant.automation.management import LISTED_KINDS` (part B).
SETTINGS_KINDS: Final = tuple(kind for kind in LISTED_KINDS if kind != "digest")
PAUSABLE_KINDS: Final = frozenset({"reminder", "scheduled_summary", "monitor"})
LIST_LIMIT: Final = 10
CONFIRM_TTL: Final = timedelta(hours=1)


def automation_ops(*, kind: str, status: str, paused_reason: str | None) -> tuple[str, ...]: ...
def mode_line(config: AutonomyConfig | None) -> str: ...
```

**`automation_ops`** (pure; the buttons of one automation row, in display order). It mirrors the Pause, Resume and Delete columns of M7-AUTO's `allowed_ops` table (M7-AUTO §3.1) for the four listed kinds; the port re-checks every press:
1. `kind not in SETTINGS_KINDS` → `()`.
2. `kind in PAUSABLE_KINDS` and `status == "active"` → start with `(OP_PAUSE,)`; `kind in PAUSABLE_KINDS` and `status == "paused"` and `paused_reason == "owner"` → start with `(OP_RESUME,)`; otherwise start with `()`. (A paused row whose `paused_reason` is anything else, `NULL` or `"invalid"`, has an invalid stored rule: resume is never offered, P3-schema §3.5.)
3. Append `OP_DELETE` and return.

**`mode_line`** (pure; the same four sentences M6-DELEG's "Delegation on" notice uses), first match wins:

| Condition | Text (exact) |
|---|---|
| `config is None` or not `config.run_enabled` | `Automatic handling is switched off, so nothing is sent on its own in delegated chats.` |
| not `config.send_enabled` | `Shadow mode: replies that pass every check come to you as drafts; nothing is sent on its own.` |
| `config.languages` is empty | `No language is switched on for automatic replies yet, so replies come to you as drafts.` |
| otherwise | `Replies that pass every check are sent on their own with the AI notice, in: {codes}. Everything else comes to you as a draft or an alert.` where `{codes}` = `", ".join(sorted(lang.value for lang in config.languages))` |

**Check:** `docker compose -f compose.dev.yml run --rm app uv run pytest tests/unit/test_settings_p3_rules.py -q` → pass once §7.1 is written (step 6.11 orders red first).

### 6.2 Texts (exact; module constants)

| Constant | Text |
|---|---|
| `NOT_AVAILABLE` | `Not available right now; nothing changed.` |
| `DELEGATIONS_EMPTY` | `🤝 <b>Delegations</b>\nNo delegation is active. To start one, tell me which chats and for how long.` (used when `TOOL_CREATE_DELEGATION` is in `enabled_flags`) |
| `DELEGATIONS_EMPTY_OFF` | `🤝 <b>Delegations</b>\nNo delegation is active, and creating one is switched off right now.` (used when it is not) |
| `PAUSED_LINE` | `⏸️ Automatic replies are paused until you send /resume.` (shown when `account.autonomy_paused`) |
| `NEVER_LINE` | `Never: promise dates, money or meetings that are not in your notes; mention other chats; send media; discuss sensitive topics.` |
| `END_QUESTION` | `<b>End the delegation for {names}?</b>\nAutomatic replies there stop now, and any reply still waiting to be sent is cancelled.` |
| `ENDED_TEXT` | `✅ Delegation ended for {names}. No more automatic replies there.` |
| `END_ALREADY_TEXT` | `This delegation had already ended; nothing changed.` |
| `END_GONE_TEXT` | `This delegation no longer exists; nothing changed.` |
| `END_KEPT_TEXT` | `Kept: the delegation continues.` |
| `NOT_ACTIVE_TEXT` | `This delegation is no longer active; nothing to change.` |
| `WORDING_ENDED_TEXT` | `This delegation has ended; its AI notice can no longer be changed.` |
| `WORDING_PROMPT` | `✏️ Send the new AI-notice line for {language} as one message: one line, at most 200 characters, no formatting characters. {marker} is added in front automatically. To keep the current line, send any command, for example /settings.` |
| `WORDING_SAVED` | `✅ AI notice for {language} is now: {marker} {line}\nIt applies to replies written from now on.` |
| `WORDING_ALREADY` | `That is already the AI notice for {language}; nothing changed.` |
| `WORDING_INVALID` | `That line cannot be used, or the delegation has ended; nothing changed.` |
| `WORDING_PROBLEMS` | `EMPTY`: `The line cannot be empty; nothing changed.` · `TOO_LONG`: `The line can be at most 200 characters; nothing changed.` · `MULTILINE`: `The line must be a single line; nothing changed.` · `MARKUP`: `The line cannot contain formatting or invisible characters; nothing changed.` (a `Mapping[WordingProblem, str]`) |
| `AUTOMATION_GONE_TEXT` | `This automation no longer exists; nothing changed.` |
| `DELETE_QUESTION` | `<b>Delete {label} completely?</b>\nIt cannot be undone.` |
| `DELETED_TEXT` | `🗑️ Deleted: {label}.` |
| `DELETE_KEPT_TEXT` | `Kept; nothing changed.` |
| `DELETE_INVALID_TEXT` | `It cannot be deleted right now; nothing changed. If a reminder is going off at this moment, try again in a minute.` |

`{names}`, `{label}`, `{line}` and `{marker}` are already escaped when they are put in (§6.3). The `<b>` tags are application markup, written raw.

### 6.3 Helpers (private; in the same module)

```python
P3Screen = tuple[str, Sequence[Sequence[Button]]]  # message text, button rows (core.channel.Button)


async def _live_card(
    session: AsyncSession, *, account_id: int, token: str, kind: str, now: datetime
) -> PendingInput | None: ...
async def _card_row(
    session: AsyncSession,
    *,
    account_id: int,
    kind: str,
    ref_id: int | None,
    clock: Clock,
    buttons: Sequence[tuple[str, str]],
    ttl: timedelta = CARD_TTL,
) -> list[Button]: ...
def _markup(rows: Sequence[Sequence[Button]]) -> InlineKeyboardMarkup | None: ...
def _local(moment: datetime, timezone: str) -> str: ...  # "Tue 03 Nov, 18:00" (no zone name)
def _short(moment: datetime, timezone: str) -> str: ...  # "Tue 18:00"
async def _chat_names(
    session: AsyncSession, *, account_id: int, conversation_ids: Sequence[int]
) -> str: ...
def _after(
    session: AsyncSession,
    gateway: OutboundGateway,
    press: CardPress,
    target: ControlChat,
    *,
    answer: str,
    edit: P3Screen | None = None,
    send: P3Screen | None = None,
) -> None: ...
def _reply(
    session: AsyncSession, gateway: OutboundGateway, target: ControlChat, text: str
) -> None: ...
```

- **`_live_card`**: `SELECT pending_inputs WHERE account_id = :a AND token = :t AND kind = :k AND expires_at > :now` (no lock, no delete). Used only by presses that **change nothing** (`ls`, `vw`, `end`, `dl`, `ed_*`), so those buttons keep working for the card's lifetime. Every press that changes something (`eok`, `ecx`, `pa`, `rs`, `dok`, `dcx`) uses `accounts.service.claim_card` (single use).
- **`_card_row`**: one `accounts.service.create_card(session, account_id=…, kind=…, clock=…, ref_id=…, ttl=ttl)`; returns `[Button(label, accounts.callbacks.encode(kind, card.token, op)) for label, op in buttons]`. If `card.token` is `None`, raise `RuntimeError("card without a token")` (never `assert`). `CARD_TTL` is `accounts.service.CARD_TTL` (24 h).
- **`_markup`**: `None` for no rows; otherwise `InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=b.label, callback_data=b.callback_data) for b in row] for row in rows])` (`aiogram.types`, as `panels.py` uses).
- **`_local` / `_short`**: `moment.astimezone(ZoneInfo(timezone)).strftime("%a %d %b, %H:%M")` / `strftime("%a %H:%M")`. Screens state the timezone once (`Times are in {tz}.`).
- **`_chat_names`**: `SELECT conversations.id, contacts.display_name, contacts.username FROM conversations JOIN contacts ... WHERE conversations.account_id = :a AND contacts.account_id = :a AND conversations.id IN (...)`; for each id in the given order: `escape(memory.summaries.preferred_name(display_name, username) or "a contact")`; a missing chat reads `a contact`. Joined with `", "`. Returns escaped text.
- **`_after`**: `db.uow.after_commit(session, run)` where `run` is `async def run(factory: async_sessionmaker[AsyncSession]) -> None` (the hook passes the session factory; copy `panels._after_press`, minus its `_record` call) and does, in order: `await gateway.answer_callback(press.callback_id, answer)`; if `edit`: `await gateway.edit_control_text(target, press.message_id, text, keyboard=_markup(rows))`, or `await gateway.send_text(target, text, keyboard=_markup(rows))` when `press.message_id is None`; if `send`: `await gateway.send_text(target, text, keyboard=_markup(rows))`.
- **`_reply`**: `after_commit` with the same `run(factory)` shape that runs `await gateway.send_text(target, text)`.

Never call aiogram `Message` or `CallbackQuery` shortcuts; the AST test of `telegram/control/` fails if you do (AGENTS §5).

### 6.4 The class and its two row builders

```python
@dataclass(frozen=True, slots=True)
class SettingsP3:
    delegation_controls: DelegationControls | None
    automation_controls: AutomationControls | None
    autonomy: AutonomyConfig | None
    enabled_flags: frozenset[str]

    async def settings_rows(self, session: AsyncSession, clock: Clock,
                            account: Account) -> list[list[Button]]: ...
    async def panel_rows(self, session: AsyncSession, clock: Clock, account: Account,
                         panel: ConversationPanel) -> list[list[Button]]: ...
    async def press_delegation(self, session: AsyncSession, gateway: OutboundGateway, clock: Clock,
                               scheduler: JobScheduler | None, account: Account,
                               press: CardPress) -> HandlerResult: ...
    async def press_wording(self, ...same parameters...) -> HandlerResult: ...
    async def press_automation(self, ...same parameters...) -> HandlerResult: ...          # part B
    async def wording_input(self, session: AsyncSession, gateway: OutboundGateway, clock: Clock,
                            scheduler: JobScheduler | None, account: Account,
                            message: IncomingMessage, pending: PendingInput) -> HandlerResult: ...
```

The methods match `SettingsRowBuilder`, `PanelRowBuilder`, `CallbackHandler` and `PendingInputHandler` exactly (F0-R, P3-interfaces §12.2). `scheduler` is unused (keep the parameter).

**Current delegations (R89):** `views = await delegations_repo.list_current(session, account_id=account.id, now=clock.now())` (F0-S): the delegations that are active **or not yet started** (`status = 'active'`, `ends_at IS NULL OR now < ends_at`, no `starts_at` condition), ordered by `starts_at`, `id`, account-scoped. A delegation whose stored spec no longer validates is skipped by the seam (and logged there with its id only). There is no query of SET-P3's own for delegations; `delegations_repo.get(...)` is used only for one delegation's details and the End and wording presses.

**Current automations** (`_current_automations(account_id, now)`): `Automation.account_id == account_id`, `Automation.kind.in_(SETTINGS_KINDS)`, `Automation.status.in_(("active", "paused"))`, `or_(Automation.expires_at.is_(None), Automation.expires_at > now)`, ordered by `next_run_at` ascending with nulls last, then `id`.

**`settings_rows`** (called inside the `/settings` transaction by F0-R's `settings_screen`):
1. `rows = []`.
2. `self.delegation_controls is not None` → `n` = the count of current delegations; append `await _card_row(..., kind=P3D_CARD, ref_id=None, buttons=[(f"🤝 Delegations ({n})…", OP_LIST)])`.
3. `self.automation_controls is not None` (part B) → `m` = the count of current automations; append `_card_row(..., kind=P3A_CARD, ref_id=None, buttons=[(f"🗓️ Automations ({m})…", OP_LIST)])`.
4. Return `rows` (both ports `None` → `[]`: the rows are hidden).

**`panel_rows`** (J12):
1. `self.delegation_controls is None` or `panel.is_bot` → `[]`.
2. `view = await delegations_repo.active_covering(session, account_id=account.id, conversation_id=panel.conversation_id, now=clock.now())`; `None` → `[]` (no delegation covers this chat now: no row).
3. Label: `"🤝 Delegated (standing)"` when `view.ends_at is None`; else `f"🤝 Delegated until {_short(view.ends_at, account.timezone)}"` when `view.ends_at - clock.now()` is under 6 days; else `f"🤝 Delegated until {_local(view.ends_at, account.timezone)}"` (a bare weekday would be ambiguous a week away).
4. Return `[await _card_row(..., kind=P3D_CARD, ref_id=view.delegation_id, buttons=[(label, OP_VIEW), ("End…", OP_END)])]`.

### 6.5 Delegation screens and presses (`press_delegation`)

Screens (private async methods of `SettingsP3`, so they can read `self.autonomy` and `self.enabled_flags`; each returns a `P3Screen`):

**Delegations list** `_delegations_screen(session, clock, account)`:
- `views` = `list_current(...)` (above); `total = len(views)`; the screen shows the first `LIST_LIMIT` views.
- `total == 0` → text `DELEGATIONS_EMPTY` (or `DELEGATIONS_EMPTY_OFF` when `TOOL_CREATE_DELEGATION` is not in `self.enabled_flags`; import the constant from `assistant.core.flags`), no rows.
- Otherwise the text is these lines joined with `\n`:
  1. `🤝 <b>Delegations</b>: {total} active or upcoming. Times are in {escape(account.timezone)}.`
  2. `mode_line(self.autonomy)`
  3. `PAUSED_LINE` only when `account.autonomy_paused` (the kill switch is on: `mode_line` alone would claim replies are sent).
  4. One line per shown view, numbered from 1 in order: `{i}. {names} — {window}`, where `names = await _chat_names(..., conversation_ids=view.conversation_ids)` and `window` is `standing, until you end it` (`ends_at is None`), `from {_local(starts_at)} until {_local(ends_at)}` (`starts_at > now`), else `until {_local(ends_at)}`.
  5. When `total > LIST_LIMIT`: `Showing {LIST_LIMIT} of {total}. Ask me "list my delegations" to see the rest.`
- Rows: one per shown view, same numbering: `await _card_row(..., kind=P3D_CARD, ref_id=view.delegation_id, buttons=[(f"{i}. Details", OP_VIEW), (f"{i}. End…", OP_END)])`.

**Delegation details** `_delegation_screen(session, clock, account, delegation_id)`:
- `view = await delegations_repo.get(...)`; `None` or `view.status != "active"` → `(NOT_ACTIVE_TEXT, ())`.
- Text lines: `🤝 <b>Delegation</b>`; `Chats: {names}`; `When: {window} ({escape(tz)})` (window as in the list); `Topic: “{escape(topic)}”` or `Topic: any message in these chats`; `Allowed: {", ".join(BEHAVIOUR_LABELS[b] for b in view.rules.allowed)}`; `NEVER_LINE`; `Limits: {per_conversation_hour} per chat per hour, {per_delegation} in total.`; `Urgent decisions: alert you at once, also during quiet hours.` when `view.rules.escalation.urgent_bypass_quiet_hours` else `Urgent decisions: alert you at once.`; `mode_line(self.autonomy)`; `PAUSED_LINE` when `account.autonomy_paused`.
- Rows: `[await _card_row(..., kind=P3W_CARD, ref_id=id, buttons=[("AI notice wording…", OP_VIEW)]), await _card_row(..., kind=P3D_CARD, ref_id=id, buttons=[("End delegation…", OP_END)])]`.

**End question** `_end_confirm(session, clock, account, view)`: text `END_QUESTION.format(names=…)`; rows `[_card_row(..., kind=P3D_CARD, ref_id=view.delegation_id, buttons=[("End now", OP_END_OK), ("Cancel", OP_END_CANCEL)], ttl=CONFIRM_TTL)]`.

**`press_delegation`** (`now = clock.now()`, `target = ControlChat(account_id=account.id, chat_id=press.chat_id)`):
1. `self.delegation_controls is None` → `_after(answer=NOT_AVAILABLE)`; `HandlerResult(False, "p3d", None, note="not_wired")`.
2. `press.op in (OP_LIST, OP_VIEW, OP_END)`: `card = await _live_card(..., kind=P3D_CARD)`. `None`, or `card.ref_id is None` for `vw`/`end` → `_after(answer=CALLBACK_EXPIRED_TEXT)`, note `no_longer_active`.
   - `ls` → `_after(answer="Delegations", send=await _delegations_screen(...))`, note `listed`.
   - `vw` → `_after(answer="Delegation", send=await _delegation_screen(..., card.ref_id))`, note `viewed`.
   - `end` → `view = get(...)`; `None` or not active → `_after(answer="Already ended", send=(NOT_ACTIVE_TEXT, ()))`, note `already_ended`; otherwise `_after(answer="Confirm to end", send=await _end_confirm(...))`, note `asked`. The port is **not** called.
3. `press.op in (OP_END_OK, OP_END_CANCEL)`: `card = await claim_card(session, account_id=account.id, token=press.token, clock=clock)`; `None`, `card.kind != P3D_CARD` or `card.ref_id is None` → expired as above.
   - `ecx` → `_after(answer="Kept", edit=(END_KEPT_TEXT, ()))`, note `cancelled`.
   - `eok` → read `names` first (`get` → its `conversation_ids`; `a delegation` if `None`), then `result = await self.delegation_controls.end(session, account_id=account.id, delegation_id=card.ref_id, now=now)`:

     | `PortResult` | Answer | Edited message text | Note |
     |---|---|---|---|
     | `DONE` | `Ended` | `ENDED_TEXT` | `ended` |
     | `ALREADY` | `Already ended` | `END_ALREADY_TEXT` | `already_ended` |
     | `NOT_FOUND` | `No longer exists` | `END_GONE_TEXT` | `not_found` |
     | `INVALID` | `Not possible` | `NOT_AVAILABLE` | `invalid` |
4. Any other op → expired, note `unknown_op`.

Ending is never flag-gated (P3-interfaces §4.5): the Delegations row and End work with `autonomy.run` off.

### 6.6 Wording screen, presses and the text input (`press_wording`, `wording_input`)

**Wording screen** `_wording_screen(session, clock, account, delegation_id)`:
- `view = get(...)`; `None` or not active → `(WORDING_ENDED_TEXT, ())`.
- Text lines: `✏️ <b>AI notice</b> for the delegation in: {names}`; `Every automatic reply starts with this line in the reply's language. {escape(AI_MARKER)} is always added in front and cannot be removed.`; then for each `lang` in `GateLanguage` order: `• {LOCALE_LABELS[lang]}: {escape(AI_MARKER)} {escape(line)} — {kind}` where `line = view.rules.disclosure.get(lang.value) or DISCLOSURE_DEFAULTS[lang]` and `kind` is `custom` when `lang.value in view.rules.disclosure`, else `default`.
- Rows (one `P3W_CARD` card, `ref_id` = the delegation): `[("Edit Uzbek (Latin)", "ed_ul"), ("Edit Uzbek (Cyrillic)", "ed_uc")]` and `[("Edit Russian", "ed_ru"), ("Edit English", "ed_en")]` (two rows of one card: create the card once and build both rows from its token).

**`press_wording`**:
1. Port `None` → `NOT_AVAILABLE`, note `not_wired`.
2. `vw` → `_live_card(kind=P3W_CARD)` with a `ref_id` → `_after(answer="AI notice", send=await _wording_screen(...))`, note `viewed`.
3. `press.op in WORDING_EDIT_OPS` → `_live_card(kind=P3W_CARD)` with a `ref_id`; `view = get(...)`; not active → `_after(answer="Already ended", send=(WORDING_ENDED_TEXT, ()))`, note `ended`. Otherwise `lang = WORDING_EDIT_OPS[press.op]`; `await set_pending_input(session, account_id=account.id, kind=<the WORDING_INPUT_KINDS key whose value is lang>, clock=clock, ref_id=view.delegation_id)`; `_after(answer="Send the new line", send=(WORDING_PROMPT.format(language=LOCALE_LABELS[lang], marker=escape(AI_MARKER)), ()))`, note `asked`.
4. Anything else, or a missing card → `CALLBACK_EXPIRED_TEXT`, note `no_longer_active`.

**`wording_input`** (the dispatcher calls it for the owner's next plain message; it never sees a forwarded or quoted message, dispatcher rule 3). `target = ControlChat(account_id=account.id, chat_id=message.chat_id)`:
1. `await clear_pending_input(session, account_id=account.id)` first (the question is answered whatever happens next).
2. `lang = WORDING_INPUT_KINDS.get(pending.kind)`; `lang is None` or `pending.ref_id is None` or `self.delegation_controls is None` → `_reply(NOT_AVAILABLE)`; `HandlerResult(False, "p3w", None, note="not_wired")`.
3. `text = message.text.strip()`; `problem = validate_wording(text)`; not `None` → `_reply(WORDING_PROBLEMS[problem])`, note `invalid`; the port is not called.
4. `result = await self.delegation_controls.set_disclosure_wording(session, account_id=account.id, delegation_id=pending.ref_id, locale=lang.value, text=text, now=clock.now())`:

   | `PortResult` | Reply | Note |
   |---|---|---|
   | `DONE` | `WORDING_SAVED.format(language=LOCALE_LABELS[lang], marker=escape(AI_MARKER), line=escape(text))` | `saved` |
   | `ALREADY` | `WORDING_ALREADY.format(language=…)` | `unchanged` |
   | `INVALID` | `WORDING_INVALID` | `invalid` |
   | `NOT_FOUND` | `END_GONE_TEXT` | `not_found` |
5. Return `HandlerResult(True, "p3w", None, note=…)`. Never log the text.

### 6.7 Automation screens and presses (`press_automation`, part B)

**Label** `_automation_label(session, account, row, now) -> str` (escaped text; build the plain parts, truncate, then escape once):

| Row | Label |
|---|---|
| `reminder` with `rrule` | `🔁 Reminder “{text}” — next {_local(next_run_at)}` with `text = load_schedule_rule("reminder", row.spec).text`; `next_run_at` `None` → `— no next time` |
| `reminder` without `rrule` | `⏰ Reminder “{title}” — {_local(run_at)}` with `title` = this account's `Task.title` for `row.task_id` (P3-T49); no task → `⏰ Reminder (its task is gone)` |
| `scheduled_summary` | `🗓️ Scheduled summary — next {_local(next_run_at)}` |
| `monitor` | `🔔 Monitor: {text}` with `text = await automation.monitors.describe(session, account_id=…, rule=parse_rule(row.spec), expires_at=row.expires_at, timezone=account.timezone)` cut to 120 characters (`text[:119] + "…"` when longer); `parse_rule` `None` → `🔔 Monitor (its rule is invalid)`; `NotInAccountError` from `describe` → `🔔 Monitor` |
| `follow_up` | `⏳ Follow-up on the chat with {name} — due {_local(next_run_at or run_at)}` with `rule = load_schedule_rule("follow_up", row.spec)` and `name` = this account's contact `rule.contact_id` (`preferred_name`, else `a contact`) |
| any rule that raises `pydantic.ValidationError` | `{emoji} {Kind} (its rule is invalid)` (`🔁`/`⏰` Reminder, `🗓️` Scheduled summary, `⏳` Follow-up) |
| suffix | ` (paused)` when `status == "paused"` and `paused_reason == "owner"`; ` (paused: its rule is invalid)` when `status == "paused"` and `paused_reason` is anything else (`None` or `"invalid"`) |

Catch only `pydantic.ValidationError` and `NotInAccountError`, nothing broader.

**Automations list** `_automations_screen(session, clock, account, notice: str | None = None)`:
- `total` = the count of current automations; `rows_db` = the first `LIST_LIMIT` of them.
- `total == 0` → `🗓️ <b>Automations</b>\nNo reminders, scheduled summaries, monitors or follow-ups are on or paused.` (with `notice` first, then a blank line, when given), no rows.
- Otherwise lines: (`notice`, blank line, when given); `🗓️ <b>Automations</b>: {total} on or paused. Times are in {escape(tz)}.`; `{i}. {label}` per row; `Showing {LIST_LIMIT} of {total}. Ask me "list my automations" to see the rest.` when `total > LIST_LIMIT`; then, each only when at least one listed row has that kind and its flag is **not** in `self.enabled_flags`:
  - `reminder` without `REMINDERS_RUN` → `Reminders are switched off right now, so none of them will fire.`
  - `follow_up` without `FOLLOWUPS_RUN` → `Follow-ups are switched off right now, so they are not checked.`
  - `scheduled_summary` without `SCHEDULED_SUMMARIES_RUN` → `Scheduled summaries are switched off right now, so none will be sent.`
  
  and last `The daily digest is under Alerts & digest…, and delegations under Delegations….`
- Rows: per listed row with `ops = automation_ops(kind=row.kind, status=row.status, paused_reason=row.paused_reason)` not empty: one `_card_row(..., kind=P3A_CARD, ref_id=row.id, buttons=[…])` with labels `f"{i}. Pause"` (`pa`), `f"{i}. Resume"` (`rs`), `f"{i}. Delete…"` (`dl`).

**Delete question** `_delete_confirm(session, clock, account, row)`: text `DELETE_QUESTION.format(label=await _automation_label(...))`; rows `[_card_row(..., kind=P3A_CARD, ref_id=row.id, buttons=[("Delete", OP_DELETE_OK), ("Cancel", OP_DELETE_CANCEL)], ttl=CONFIRM_TTL)]`.

**`press_automation`**:
1. `self.automation_controls is None` → `NOT_AVAILABLE`, note `not_wired`.
2. `ls` → `_live_card(kind=P3A_CARD)` → `_after(answer="Automations", send=await _automations_screen(...))`, note `listed`.
3. `dl` → `_live_card` with a `ref_id`; `row` = this account's `Automation` by id (`select ... where id = :id and account_id = :a`); `None` → `_after(answer="No longer exists", send=(AUTOMATION_GONE_TEXT, ()))`, note `not_found`; `row.kind not in SETTINGS_KINDS` (R51: a digest or delegation-end row, even through a stale or odd card) → `_after(answer="Not possible", send=(DELETE_INVALID_TEXT, ()))`, note `invalid`; otherwise `_after(answer="Confirm to delete", send=await _delete_confirm(...))`, note `asked`. The port is **not** called.
4. `pa` / `rs` → `claim_card` (`kind == P3A_CARD`, `ref_id` set); load `row` (this account's, by `card.ref_id`); compute `label` first (row `None` → `a deleted automation`). A row that exists but whose `kind not in PAUSABLE_KINDS` (a follow-up, a digest, a delegation end) is **not** sent to the port: treat it as `INVALID` (notice and answer from the table, no port call, note `invalid`). Otherwise `result = await self.automation_controls.pause(...)` or `.resume(session, account_id=account.id, automation_id=card.ref_id, now=now)`; `notice` per the table; then `_after(answer=…, edit=await _automations_screen(..., notice=notice))` (the list message is re-drawn with fresh cards), note = the result value.

   | Op | `PortResult` | Answer | Notice |
   |---|---|---|---|
   | `pa` | `DONE` | `Paused` | `⏸️ Paused: {label}.` |
   | `rs` | `DONE` | `Resumed` | `▶️ Resumed: {label}.` |
   | either | `ALREADY` | `Nothing to change` | `Nothing changed: it was already so.` |
   | `pa` | `INVALID` | `Not possible` | `Could not pause it; nothing changed.` |
   | `rs` | `INVALID` | `Not possible` | `Could not resume it: its time has passed, it has expired, or its rule is invalid. Nothing changed; delete it, or ask me to reschedule it.` |
   | either | `NOT_FOUND` | `No longer exists` | `It no longer exists; nothing changed.` |
5. `dok` → `claim_card`; `label` first; a row whose `kind not in SETTINGS_KINDS` is not sent to the port (`INVALID` path below, note `invalid`); otherwise `result = await self.automation_controls.delete(...)`; `_after(answer=…, edit=(text, ()))` with `DONE` → answer `Deleted`, text `DELETED_TEXT`; `NOT_FOUND` or `ALREADY` → `No longer exists`, `AUTOMATION_GONE_TEXT`; `INVALID` → `Not possible`, `DELETE_INVALID_TEXT`.
6. `dcx` → `claim_card` → `_after(answer="Kept", edit=(DELETE_KEPT_TEXT, ()))`, note `cancelled`.
7. Anything else or a missing card → `CALLBACK_EXPIRED_TEXT`, note `no_longer_active`.

Pause and resume apply at once (they only change what notifies the owner; P3-interfaces §12.3 has no confirm op for them). Delete always asks first (OD-19: it cannot be undone).

### 6.8 What SET-P3 never does

- Writes `delegations`, `automations`, `tasks` or `audit_log` itself (only the ports write).
- Calls the model, a job, or Telegram before the commit.
- Shows a row or button whose port is `None`, or says "ended", "paused" or "deleted" unless the port returned `DONE`.
- Treats a forwarded or quoted message as the AI-notice line (the dispatcher already routes those to the agent; your handler is never called for them).

### 6.9 Wiring (`_wire_surfaces` in `src/assistant/bootstrap.py`)

F0-R leaves `_wire_surfaces` returning a bare `P3Wiring()`. Several David units add blocks to it (M6-CATCH, STAT-ACT, SET-P3, M7-MRV, OPS-RATE, COMMIT-DUE, OPS-AV), so every one uses **one convention** (STAT-ACT's and M7-MRV's): a local `wiring` value that each block extends with `dataclasses.replace`, returned at the end. Look at the function on your base first:

- It still ends with `return P3Wiring()`: change that to `wiring = P3Wiring()` at the top of the body (after the docstring), put your block after it, and end the function with `return wiring`.
- Another David unit already did that: add only your block just before `return wiring`.
- It is built any other way (for example `return P3Wiring(ports=...)`): keep every keyword and statement already there, merge your entries into it in the same style, and ask (§10) if unsure.

Use `replace` as the module already imports it; if it does not, add `from dataclasses import replace` as a function-local import. Never touch another unit's block.

```python
def _wire_surfaces(ctx: P3WireContext) -> P3Wiring:
    """David's units. Function-local imports only; one block per unit."""
    wiring = P3Wiring()

    # ... other David units' blocks ...

    # --- SET-P3 (D1): /settings P3 screens and the panel Delegate row
    from assistant.telegram.control.settings_p3 import (
        P3D_CARD,
        P3W_CARD,
        WORDING_INPUT_KINDS,
        SettingsP3,
    )

    settings_p3 = SettingsP3(
        delegation_controls=ctx.ports.delegation_controls,
        automation_controls=ctx.ports.automation_controls,
        autonomy=ctx.autonomy_config,
        enabled_flags=ctx.settings.feature_flags,
    )
    routes = wiring.routes
    wiring = replace(
        wiring,
        routes=replace(
            routes,
            callbacks={
                **routes.callbacks,
                P3D_CARD: settings_p3.press_delegation,
                P3W_CARD: settings_p3.press_wording,
                # part B adds: P3A_CARD: settings_p3.press_automation (and the P3A_CARD import)
            },
            pending_inputs={
                **routes.pending_inputs,
                **{kind: settings_p3.wording_input for kind in WORDING_INPUT_KINDS},
            },
            settings_rows=(*routes.settings_rows, settings_p3.settings_rows),
            panel_rows=(*routes.panel_rows, settings_p3.panel_rows),
        ),
    )

    return wiring
```

`ctx.ports` already holds `DelegationControls` and `AutomationControls` when `_wire_surfaces` runs (order autonomy → automation → surfaces, P3-interfaces §15.3); read them once here, never `final_ports()`. If M6-DELEG or M7-AUTO has not merged, the port is `None` and its row stays hidden; the row appears with the merge that wires the port, without a SET-P3 change.

**Check:** `docker compose -f compose.dev.yml run --rm app uv run pytest tests/integration/test_p3_wiring.py -q` → pass (F0-R's duplicate-key checks still hold with your kinds).

### 6.10 ARCHITECTURE §6.7 bullet (exact content; your mentor checks it)

One bullet after the `/settings (as built, S03.3/S03.5; C4)` bullet: **/settings P3 screens and the Delegate row (as built, SET-P3).** The rows Delegations (n)… and Automations (n)… (each hidden while its port is not wired); the delegations list (active and upcoming, at most 10 with the true total), details, AI-notice wording per language (never empty; the marker is always added; input kinds `deleg_wording_*`), End with a confirm step through `DelegationControls`; the automations list (reminders, scheduled summaries, monitors, follow-ups; the digest and delegation ends are managed elsewhere) with Pause and Resume at once and Delete with a confirm step through `AutomationControls`; "switched off" lines for features whose flag is off and a "paused" line while the kill switch is on; the conversation panel's Delegate row (View, End) for a chat a delegation covers now. No audit rows of its own: the ports audit.

### 6.11 Steps (each ends with a check)

All commands run in your directory with `$env:COMPOSE_PROJECT_NAME = 'd1'` set (they run inside the container). If your session cannot run Docker, write the tests, push, let CI run them, and say so in the PR.

**Part A (`p3/set-p3`)**
1. **Branch** from `origin/int/p3`. **Check:** `docker compose -f compose.dev.yml run --rm app uv run python -c "from assistant.telegram.control.routes import ControlRoutes; from assistant.core.p3_ports import DelegationControls, AutomationControls, PortResult; from assistant.automation import delegations_repo; from assistant.actions.disclosure import validate_wording; print(sorted(f for f in ControlRoutes.__dataclass_fields__ if f.endswith('_rows')))"` prints `['panel_rows', 'settings_rows']`. Any import error: F0 is not on your base; stop and ask (§10).
2. **Red tests.** Write §7.1 and the part-A tests of §7.2; create `settings_p3.py` with the docstring, constants, texts, `P3Screen`, the class with every method raising `NotImplementedError`, and the two pure functions raising `NotImplementedError`. **Check:** `docker compose -f compose.dev.yml run --rm app uv run pytest tests/unit/test_settings_p3_rules.py tests/integration/test_settings_p3.py -q` → failures are `NotImplementedError` or assertion errors, none `ImportError`.
3. **Pure rules** (§6.1). **Check:** `… uv run pytest tests/unit/test_settings_p3_rules.py -q` → pass.
4. **Helpers and `settings_rows` / `panel_rows`** (§6.3, §6.4; part A shows only the delegations row). **Check:** `… uv run pytest tests/integration/test_settings_p3.py -q -k "rows or row"` → pass.
5. **Delegation screens and `press_delegation`** (§6.5). **Check:** `… -k "delegations_screen or delegation_details or details_of or end_ or cancel_keeps or second_tap or invalid_stored_spec"` → pass.
6. **Wording** (§6.6). **Check:** the whole of `tests/integration/test_settings_p3.py` passes.
7. **Wiring** (§6.9, without the `P3A_CARD` import and line; part B adds both) and the hygiene test (§7.4). **Check:** `… uv run pytest tests/integration/test_settings_p3.py tests/integration/test_log_hygiene_set_p3.py tests/integration/test_p3_wiring.py -q` → pass.
8. **Docs** (§6.10). **Check:** `git diff origin/int/p3 -- docs/ARCHITECTURE.md` shows one added bullet in §6.7 and nothing else.
9. **Static checks.** `… uv run ruff check src/assistant/telegram/control/settings_p3.py src/assistant/bootstrap.py tests/unit/test_settings_p3_rules.py tests/integration/test_settings_p3.py tests/integration/test_log_hygiene_set_p3.py`; the same with `uv run ruff format --check`; `… uv run mypy src/assistant/telegram/control/settings_p3.py src/assistant/bootstrap.py`; `… uv run lint-imports`. **Check:** all clean.
10. **Diff.** `git diff --stat origin/int/p3...HEAD` → only §3 files (part B's test file absent). Commit (red tests, then green code), push, open the draft PR (§12).

**Part B (`p3/set-p3-auto`, from `p3/set-p3`)**
11. **Red tests** of §7.3; `press_automation` and the label raise `NotImplementedError`. **Check:** `… uv run pytest tests/integration/test_settings_p3_automations.py -q` → assertion or `NotImplementedError` failures only.
12. **Automations row, list, label and presses** (§6.4 step 3, §6.7) and the `P3A_CARD` wiring line. **Check:** `… uv run pytest tests/integration/test_settings_p3_automations.py tests/integration/test_settings_p3.py -q` → pass.
13. **Static checks** as step 9 plus the new test file. **Check:** clean. **Diff:** `git diff --stat origin/p3/set-p3...HEAD` → only `settings_p3.py`, `bootstrap.py` (the `P3A_CARD` import and line) and `test_settings_p3_automations.py`. Push, open the draft PR on base `p3/set-p3`.

## 7. Named tests (red first)

Synthetic data only; real PostgreSQL 18; the `business_env` clock (`NOW` = Sun 2026-10-18 12:00 UTC; the onboarded owner's timezone is `UTC`); no sleeps. Setup used by §7.2–§7.4:

- **Recording fakes** in each test file (never in `tests/fakes/`): `RecordingDelegationControls` with `calls: list[tuple[str, int, str | None, str | None]]` (`("end", delegation_id, None, None)` or `("wording", delegation_id, locale, text)`) and `end_result` / `wording_result` (`PortResult`, default `DONE`); `RecordingAutomationControls` with `calls: list[tuple[str, int]]` (`("pause" | "resume" | "delete", automation_id)`) and one result field per method (default `DONE`). Their methods have exactly the `core/p3_ports.py` signatures.
- **`AUTONOMY_ON`** = `AutonomyConfig(send_delay=timedelta(seconds=45), debounce=timedelta(seconds=20), max_message_age=timedelta(minutes=30), max_lateness=timedelta(seconds=120), limit_per_conversation_hour=5, limit_per_delegation=30, loop_replies=3, loop_window=timedelta(minutes=10), loop_answer=timedelta(seconds=30), languages=frozenset({GateLanguage.EN}), run_enabled=True, send_enabled=True, standing_enabled=False)`; variants through `dataclasses.replace`.
- **Fixture `env`**: build `SettingsP3(...)` with the fakes, then `environment(migrated_url, owner_ids=frozenset({OWNER, OTHER_OWNER}), routes=ControlRoutes(callbacks=…, pending_inputs=…, settings_rows=(p3.settings_rows,), panel_rows=(p3.panel_rows,)))`, exactly as §6.9 wires it; yield the env and the fakes. `OTHER_OWNER = 7000000002`.
- **Chats:** `await connect(env)`; Kamola: `deliver(env, business_message(message_id=1, first_name="Kamola"))`; Bobur: `business_message(from_id=7000000009, chat_id=7000000009, message_id=2, first_name="Bobur")`.
- **Delegations:** `p3_factories.seed_delegation(session, account_id=…, conversation_ids=[…], kind=…, status="active", starts_at=…, ends_at=…, confirmed_at=…, spec=DelegationRules(conversation_ids=(…,), topic=…, allowed=(Behaviour.ASK_FOR_DETAILS, Behaviour.ACKNOWLEDGE_AND_DEFER), limits=DelegationLimits(per_conversation_hour=5, per_delegation=30), disclosure={…}).model_dump(mode="json"))` inside `transaction(env.factory)`. **Automations:** `p2_factories.seed_automation(...)`; one-off reminder tasks: `p3_factories.seed_owner_task(...)`.
- **Presses:** read the newest card of a kind with `test_conversation_modes.card(env, kind)` (import it; never edit that file) and press with `deliver(env, callback(f"{kind}:{token}:{op}", message_id=…))`. Read `env.sender.sends`, `.edits`, `.answered_callbacks`; keyboards with `test_conversation_modes.keyboard_labels` / `keyboard_ops`.

### 7.1 `tests/unit/test_settings_p3_rules.py`

- `test_automation_ops_table` — parametrized, `automation_ops(kind=k, status=s, paused_reason=r)`:

  | kind | status | paused_reason | expected |
  |---|---|---|---|
  | `reminder` | `active` | `None` | `("pa", "dl")` |
  | `reminder` | `paused` | `"owner"` | `("rs", "dl")` |
  | `reminder` | `paused` | `None` | `("dl",)` |
  | `reminder` | `paused` | `"invalid"` | `("dl",)` |
  | `scheduled_summary` | `active` | `None` | `("pa", "dl")` |
  | `scheduled_summary` | `paused` | `"owner"` | `("rs", "dl")` |
  | `monitor` | `active` | `None` | `("pa", "dl")` |
  | `monitor` | `paused` | `"owner"` | `("rs", "dl")` |
  | `follow_up` | `active` | `None` | `("dl",)` |
  | `follow_up` | `paused` | `"owner"` | `("dl",)` |
  | `reminder` | `completed` | `None` | `("dl",)` |
  | `digest` | `active` | `None` | `()` |
  | `delegation_end` | `active` | `None` | `()` |
- `test_mode_line_states_the_effective_mode` — parametrized: `None` and `replace(AUTONOMY_ON, run_enabled=False)` → the run-off sentence; `replace(AUTONOMY_ON, send_enabled=False)` → the shadow sentence; `replace(AUTONOMY_ON, languages=frozenset())` → the no-language sentence; `replace(AUTONOMY_ON, languages=frozenset({GateLanguage.RU, GateLanguage.EN}))` → `"Replies that pass every check are sent on their own with the AI notice, in: en, ru. Everything else comes to you as a draft or an alert."`.
- `test_wording_ops_and_input_kinds_cover_the_four_gate_languages` — `set(WORDING_EDIT_OPS.values()) == set(GateLanguage) == set(WORDING_INPUT_KINDS.values())`; `set(LOCALE_LABELS) == set(GateLanguage)`.
- `test_every_callback_fits_telegrams_limit` — for every kind (`p3d`, `p3w`, `p3a`) and every op of §6.1 (including the four `ed_*`): `accounts.callbacks.encode(kind, "A" * 10, op)` does not raise, and `accounts.callbacks.parse(...)` returns the same kind and op.
- `test_the_callback_kinds_are_the_contract_kinds` — `(P3D_CARD, P3W_CARD, P3A_CARD) == ("p3d", "p3w", "p3a")`.

### 7.2 `tests/integration/test_settings_p3.py` (part A)

**Rows and keyboards (per builder)**
- `test_settings_rows_are_hidden_while_their_ports_are_unwired` — `SettingsP3(delegation_controls=None, automation_controls=None, autonomy=AUTONOMY_ON, enabled_flags=frozenset())`: `settings_rows(...)` returns `[]`, and `/settings` shows exactly the core rows (its last row's labels are `["Delete everything…"]`).
- `test_settings_rows_keyboard` — delegation fake wired, automation port `None`, two active delegations (Kamola, Bobur): `settings_rows(...)` returns one row of one button, label `"🤝 Delegations (2)…"`, callback kind `p3d`, op `ls`.
- `test_the_settings_rows_follow_the_core_settings_rows` — `panels.settings_screen(session, env.clock, account, row_builders=(p3.settings_rows,))`: the core rows are unchanged and `screen.extra_rows` equals the builder's rows (P2-T45: appended only).
- `test_panel_delegate_row_keyboard` — a time-bounded delegation of Kamola's chat ending Tue 2026-10-20 18:00 UTC: `panel_rows(...)` returns `[["🤝 Delegated until Tue 18:00", "End…"]]` with ops `["vw", "end"]` and kind `p3d`; a standing one returns the label `"🤝 Delegated (standing)"`; one ending Mon 2026-10-26 18:00 UTC (8 days away) returns `"🤝 Delegated until Mon 26 Oct, 18:00"`.
- `test_panel_delegate_row_is_hidden_without_a_covering_delegation_or_port` — each of these returns `[]`: no delegation; an ended delegation; a delegation of Bobur's chat on Kamola's panel; the delegation port `None`.
- `test_the_panel_row_follows_the_core_panel_rows` — `panels.panel_screen(..., row_builders=(p3.panel_rows,))`: the mode row (Observe/Ignore/Assist), the Delete row and the priority row are unchanged, and the Delegate row comes after them.

**Delegations list and details**
- `test_delegations_screen_lists_active_and_upcoming_delegations_with_the_true_total` — 12 active delegations on 12 chats (one standing, one starting Mon 2026-10-19 09:00, the rest ending Tue 18:00) plus one `ended`: pressing `ls` sends one message whose text has exactly 10 numbered lines, contains `Showing 10 of 12. Ask me "list my delegations" to see the rest.`, contains `from Mon 19 Oct, 09:00 until` for the upcoming one and `standing, until you end it` for the standing one, and never names the ended delegation's chat; 10 button rows labelled `"{i}. Details"`, `"{i}. End…"`.
- `test_delegations_screen_states_the_effective_mode` — parametrized over the four `mode_line` cases (configs as in §7.1): the screen's second line is that sentence.
- `test_an_invalid_stored_spec_is_neither_listed_nor_counted` (R89) — two delegations, then `UPDATE app.delegations SET spec = '{"schema_version": 99}'::jsonb WHERE id = :second`: the screen lists one line, counts `1`, has one button row; the `/settings` row reads `Delegations (1)…`. (The seam skips the unreadable row; it covers no chat, so nothing is sent there.)
- `test_a_delegation_that_starts_later_is_listed_through_list_current` (R89) — a delegation starting Mon 2026-10-19 09:00 is listed with `from … until …` and counted, and an ended one is not.
- `test_an_empty_delegations_list_says_whether_creating_one_is_on` — no delegation: with `enabled_flags={TOOL_CREATE_DELEGATION}` the `ls` message is `DELEGATIONS_EMPTY` and has no keyboard; with `enabled_flags=frozenset()` it is `DELEGATIONS_EMPTY_OFF`.
- `test_the_screens_say_when_automatic_replies_are_paused` — `account.autonomy_paused = True` (set in the test database) with an active delegation and `AUTONOMY_ON`: the list and the details both contain `PAUSED_LINE` on the line after the mode sentence; with it `False` neither does.
- `test_delegation_details_show_what_the_owner_confirmed` — topic "SYNTH-TOPIC price list", allowed ask and acknowledge, limits 5 and 30, urgent bypass on: pressing `vw` sends a text containing `Chats: Kamola`, `Topic: “SYNTH-TOPIC price list”`, `Allowed: ask for missing details, say you will follow up`, `NEVER_LINE`, `Limits: 5 per chat per hour, 30 in total.` and `Urgent decisions: alert you at once, also during quiet hours.`; rows `["AI notice wording…"]` (kind `p3w`, op `vw`) and `["End delegation…"]` (kind `p3d`, op `end`).
- `test_details_of_an_ended_delegation_offer_nothing` — `NOT_ACTIVE_TEXT`, no keyboard.

**End ("settings End" and its edges)**
- `test_settings_end_asks_first_and_ends_through_the_port` — press `end`: a new message `END_QUESTION` for Kamola with buttons `["End now", "Cancel"]` (ops `eok`, `ecx`) and **no** port call; press `eok`: `calls == [("end", <id>, None, None)]`, answer `Ended`, the question edited to `"✅ Delegation ended for Kamola. No more automatic replies there."` with no keyboard.
- `test_cancel_keeps_the_delegation` — `end` then `ecx`: no port call; the question edited to `END_KEPT_TEXT`.
- `test_end_outcomes_are_reported_truthfully` — parametrized `end_result`: `ALREADY` → `END_ALREADY_TEXT`, answer `Already ended`; `NOT_FOUND` → `END_GONE_TEXT`, `No longer exists`; `INVALID` → `NOT_AVAILABLE`, `Not possible`.
- `test_a_second_tap_on_end_now_changes_nothing` — `eok` twice on the same message: one port call; the second answer is `CALLBACK_EXPIRED_TEXT`.
- `test_end_works_with_automatic_handling_switched_off` — `replace(AUTONOMY_ON, run_enabled=False)`: the row shows and `eok` calls the port (ending is never gated).

**Wording**
- `test_wording_screen_shows_the_effective_line_per_language` — `disclosure={"ru": "SYNTH-RU line"}`: the text has `• Russian: {AI_MARKER} SYNTH-RU line — custom` and `• English: {AI_MARKER} {DISCLOSURE_DEFAULTS[GateLanguage.EN]} — default` (both escaped), in `GateLanguage` order; ops `["ed_ul", "ed_uc", "ed_ru", "ed_en"]`.
- `test_wording_edit_saves_through_the_port` — press `ed_ru`: a pending input with `kind = "deleg_wording_ru"` and `ref_id` = the delegation, and the `WORDING_PROMPT` for Russian is sent; the owner then sends `"SYNTH new notice line"`: `calls == [("wording", <id>, "ru", "SYNTH new notice line")]`, the reply is `WORDING_SAVED` for Russian with that line, and no pending input remains.
- `test_wording_that_cannot_be_used_is_refused_before_the_port` — parametrized owner messages: `"   "` → the `EMPTY` text; `"x" * 201` → `TOO_LONG`; `"line one\nline two"` → `MULTILINE`; `"<b>bold</b>"` → `MARKUP`; no port call in any case.
- `test_wording_outcomes_are_reported_truthfully` — `wording_result` `ALREADY` → `WORDING_ALREADY`; `INVALID` → `WORDING_INVALID`; `NOT_FOUND` → `END_GONE_TEXT`.
- `test_a_forwarded_message_is_never_taken_as_the_wording` — after `ed_en`, the owner forwards a message (a `forward_origin` hidden user, as `test_monitor_tools.FORWARD`): no port call.
- `test_a_command_cancels_the_wording_question` — after `ed_en`, the owner sends `/settings`: no pending input remains; no port call.
- `test_an_ended_delegations_wording_cannot_be_edited` — an ended delegation, press `ed_en`: `WORDING_ENDED_TEXT`; no pending input.

**Unwired ports, scoping, escaping, the real port**
- `test_a_press_for_an_unwired_port_changes_nothing` — routes from `SettingsP3(None, None, …)` and a `p3d` card made with `create_card`: pressing `ls`, `end` and `eok` each answers `NOT_AVAILABLE`, sends no message and writes nothing.
- `test_rg21_settings_p3_cards_of_another_account_are_no_longer_active` — OTHER_OWNER presses OWNER's `p3d` (`vw`, `end`, `eok`) and `p3w` (`ed_en`) tokens: each answer is `CALLBACK_EXPIRED_TEXT`; no port call; no pending input for either account.
- `test_rg21_settings_lists_show_only_the_owners_rows` — both owners have a delegation: OWNER's list names only OWNER's chat, and OWNER's row says `(1)`.
- `test_settings_p3_screens_escape_untrusted_text_once` (GW-ESC §3.3 rule 6) — Kamola's `display_name` and the topic both set to `HOSTILE = "A&B <b>x</b> &amp;"` (`HOSTILE_ESCAPED = "A&amp;B &lt;b&gt;x&lt;/b&gt; &amp;amp;"`, module constants of the test file); with `rendered = telegram.render.sanitize(text)` for the text the gateway receives: the list screen has `rendered.count(HOSTILE_ESCAPED) == 1` (the name; the topic is not shown there); the details screen `== 2` (the name in `Chats:` and the topic); the wording screen `== 1` (the name in its header); the end question `== 1`; and none of the four contains `"<b>x</b>"` while each still contains its own application `<b>` markup.
- `test_wire_surfaces_registers_the_settings_p3_routes` — build the `P3WireContext` with the helper F0-R's `tests/integration/test_p3_wiring.py` uses (import it; if it has none you can import, stop and ask, §10) with `ports=P3Ports(delegation_controls=<fake>)`; `wiring = _wire_surfaces(ctx)`: `wiring.routes.callbacks` has `"p3d"` and `"p3w"`, `wiring.routes.pending_inputs` has the four `deleg_wording_*` kinds, and `wiring.routes.settings_rows` and `panel_rows` each hold one builder; with `P3Ports()` (nothing wired) the same builders are registered but `settings_rows(...)` returns `[]` (hidden). If another David unit's block is already in the function on your base, the test asserts only your own entries.
- `test_settings_end_with_the_real_delegation_service` — `delegations = pytest.importorskip("assistant.automation.delegations")`; routes with `delegations.DelegationService(catch_up=lambda: None)`: `end` then `eok` leaves the delegation `status = "ended"`, `ended_reason = "owner_ended"`. (Skipped until M6-DELEG is on your base; it runs on `int/p3` once both are merged.)

### 7.3 `tests/integration/test_settings_p3_automations.py` (part B)

Seed (account timezone `UTC`): A = recurring reminder, `active`, `rrule="FREQ=DAILY"`, `spec={"schema_version": 1, "kind": "reminder", "max_lateness_minutes": 720, "text": "SYNTH water plants"}`, `next_run_at` Mon 2026-10-19 09:00; B = one-off reminder, `paused`, `paused_reason="owner"`, its task's title "SYNTH call the bank", `run_at` Mon 2026-10-19 10:00; C = `scheduled_summary`, `active`, `next_run_at` Mon 2026-10-19 18:00; D = `monitor`, `active`, a valid `MonitorRule`, `next_run_at` NULL; E = `follow_up`, `active`, on Kamola's chat, `next_run_at` Tue 2026-10-20 12:00. Not listed: a `digest`, a `delegation_end`, a `cancelled` reminder, and a `monitor` whose `expires_at` is NOW − 1 h.

- `test_settings_automations_row_keyboard` — both ports wired: the second row is `["🗓️ Automations (5)…"]` (kind `p3a`, op `ls`); automation port `None`: that row is absent.
- `test_automations_screen_lists_each_kind_with_its_buttons` — pressing `ls` sends lines 1–5 in the order A, B, C, E, D with labels `🔁 Reminder “SYNTH water plants” — next Mon 19 Oct, 09:00`, `⏰ Reminder “SYNTH call the bank” — Mon 19 Oct, 10:00 (paused)`, `🗓️ Scheduled summary — next Mon 19 Oct, 18:00`, `⏳ Follow-up on the chat with Kamola — due Tue 20 Oct, 12:00`, `🔔 Monitor: …`; button labels `["1. Pause", "1. Delete…", "2. Resume", "2. Delete…", "3. Pause", "3. Delete…", "4. Delete…", "5. Pause", "5. Delete…"]`; the digest, the delegation end, the cancelled reminder and the expired monitor are absent.
- `test_automations_screen_reports_true_totals_beyond_the_limit` — 12 active recurring reminders: 10 lines and `Showing 10 of 12. Ask me "list my automations" to see the rest.`
- `test_automations_screen_says_when_a_kind_is_switched_off` — `enabled_flags=frozenset()`: the three switched-off sentences appear; `enabled_flags={REMINDERS_RUN, FOLLOWUPS_RUN, SCHEDULED_SUMMARIES_RUN}`: none appears.
- `test_settings_automations_pause_and_delete` (FR-CTL-10; P3-interfaces §12.3: pause applies at once, delete asks first) — press `1. Pause`: `calls == [("pause", A)]`, answer `Paused`, the list message edited with the first line `⏸️ Paused: 🔁 Reminder “SYNTH water plants” — next Mon 19 Oct, 09:00.`; press `4. Delete…`: a new message `DELETE_QUESTION` with `["Delete", "Cancel"]` and no delete call; press `Delete`: `("delete", E)` recorded once, the question edited to `🗑️ Deleted: ⏳ Follow-up on the chat with Kamola — due Tue 20 Oct, 12:00.`
- `test_settings_automation_delete_needs_a_confirm_step` — `dl` then `dcx`: no delete call; the question edited to `DELETE_KEPT_TEXT`.
- `test_pause_resume_and_delete_outcomes_are_reported_truthfully` — parametrized per op and result (`ALREADY`, `INVALID`, `NOT_FOUND`) → the §6.7 notice or text and answer.
- `test_a_paused_invalid_rule_offers_only_delete` — parametrized over `paused_reason` `None` and `"invalid"`: a reminder `paused` with that value: its label ends `(paused: its rule is invalid)`; its only button is `"{i}. Delete…"`.
- `test_hidden_kinds_are_never_listed_counted_or_sent_to_the_port` (R51) — a `digest` and a `delegation_end` automation exist (plus the seed): the `Automations (n)…` row counts neither (still `(5)`), the list shows neither, and with cards made by `create_card(kind="p3a", ref_id=<the delegation_end id>)`: `dl` → `DELETE_INVALID_TEXT`, `pa` → the `INVALID` pause notice, `dok` → `DELETE_INVALID_TEXT`; a card on a `follow_up` row with `pa` → the same `INVALID` pause notice; no port call in any of these.
- `test_rg21_automation_cards_of_another_account_are_no_longer_active` — OTHER_OWNER presses OWNER's `p3a` tokens (`ls`, `pa`, `dl`, `dok`): `CALLBACK_EXPIRED_TEXT`; no port call.
- `test_automation_labels_escape_untrusted_text_once` — reminder text `"A&B <b>x</b> &amp;"`: `sanitize(text)` contains `"A&amp;B &lt;b&gt;x&lt;/b&gt; &amp;amp;"` once and never `"<b>x</b>"`.
- `test_the_settings_kinds_are_m7_autos_listing_rule_without_the_digest` (R51) — `set(SETTINGS_KINDS) == set(management.LISTED_KINDS) - {"digest"}` and `"delegation_end" not in SETTINGS_KINDS`.
- `test_settings_buttons_match_m7_auto_allowed_ops` — `management` is the real `assistant.automation.management` (part B's base has it); for every §7.1 row whose kind is in `SETTINGS_KINDS`: `set(automation_ops(...)) == {op for op, name in (("pa", "PAUSE"), ("rs", "RESUME"), ("dl", "DELETE")) if management.ManageOp[name] in management.allowed_ops(Automation(kind=k, status=s, paused_reason=r, rrule="FREQ=DAILY" if k == "reminder" else None, spec={}))}` (drift guard).
- `test_settings_pause_with_the_real_automation_manager` — routes with `management.AutomationManager()`: `1. Pause` leaves A with `status = "paused"`, `paused_reason = "owner"`.

### 7.4 `tests/integration/test_log_hygiene_set_p3.py`

- `test_settings_p3_logs_no_names_topics_or_wording` — inside `capture_logs()` (F0-E's `tests/log_hygiene.py`), with `CANARY` in Kamola's display name, a topic, an AI-notice line sent through `wording_input` and a reminder text: open both lists, the details and the wording screens, end a delegation, change a wording, pause and delete an automation; `assert_content_free(output, CANARY)`.

You may add tests; never fewer, never with changed expected values.

## 8. Truthfulness edge cases

- **Hidden, not broken.** A port that is `None` hides its row and button; an old card for it answers `Not available right now; nothing changed.` and nothing else happens. Never a row that leads to an error.
- **Done only when done.** "Ended", "Paused", "Resumed", "Deleted" and "AI notice … is now" appear only for `PortResult.DONE`. `ALREADY`, `INVALID` and `NOT_FOUND` each have their own sentence, and none of them says success.
- **Asking is not doing.** `end` and `dl` only send a question; the port is called only by `eok` / `dok`. A cancelled question says "Kept".
- **True totals.** A list capped at 10 states the real total of the readable delegations and how to see the rest. A delegation whose stored spec cannot be read is skipped by F0-S's `list_current` (R89), so this screen cannot count it; it covers no chat and nothing is sent there, and the seam logs its id. The earlier "N could not be read" line was dropped for that reason (reported to the TL).
- **"Off" is said.** The delegations list states the effective mode (switched off, shadow, no language, on), says when the kill switch is on (`PAUSED_LINE`), and says when creating a delegation is switched off. A listed reminder, follow-up or scheduled summary whose feature flag is off says it will not fire. Ending stays available whatever the flags.
- **Hidden kinds stay hidden (R51).** The digest and delegation-end automations are never listed, counted or sent to `AutomationControls` from here; the screen points to Alerts & digest and to Delegations instead.
- **A paused row says why.** `(paused)` is only the owner's pause; any other paused row reads `(paused: its rule is invalid)` and offers Delete only.
- **The AI marker cannot be removed.** The wording screen always shows the marker in front; the prompt says it is added automatically; an empty, long, multi-line or formatted line is refused with its reason before the port; the port re-validates (OD-4).
- **The new line applies from now on.** The saved message says so (replies already composed keep their text, M6-DELEG §3.5).
- **Upcoming is not active.** A delegation that starts later reads `from … until …`; the panel row appears only while a delegation covers the chat now (`active_covering`).
- **No text leaves the module** in logs or exception messages; outcome notes are fixed codes.

## 9. Acceptance checklist

- [ ] Every test in §7 exists, was seen failing, and passes (the two `importorskip` tests per part may show as skipped until their port's unit is on the base; the PR says so).
- [ ] Part A: `git diff --stat origin/int/p3...HEAD` lists only `settings_p3.py`, `bootstrap.py`, `docs/ARCHITECTURE.md`, `test_settings_p3_rules.py`, `test_settings_p3.py` and `test_log_hygiene_set_p3.py`. Part B: `git diff --stat origin/p3/set-p3...HEAD` lists only `settings_p3.py`, `bootstrap.py` and `test_settings_p3_automations.py`.
- [ ] The `bootstrap.py` diff touches only the body of `_wire_surfaces`; no top-level import is added (F0-R's frozen-import test passes).
- [ ] `ruff check`, `ruff format --check` and `mypy` (strict) clean on the changed files; `lint-imports` clean.
- [ ] No `assert`, no broad `except` (only `pydantic.ValidationError` and `NotInAccountError` are caught), no logging of names, topics, wording or reminder text; every Telegram call is a gateway call made after the commit.
- [ ] Every keyboard in §7 matches label for label and op for op (QD1 compares).
- [ ] The PR description names the contract gaps of §10 you relied on and the owner-decision tags (OD-4, OD-7, OD-19).

## 10. When to stop and ask

Ask on the SET-P3 issue, starting the comment with "Question for J3:" (automation questions in part B: "Question for E1:"), in plain text, never an @-mention. If nobody answers within 2 h, David raises it with the TL.

- Step 1's import check fails, `ControlRoutes` has no `settings_rows` / `panel_rows`, or `panels.settings_screen` / `panel_screen` take no `row_builders`.
- `_wire_surfaces` already has a shape you cannot add your block to without changing another unit's lines.
- A named test cannot pass without editing a file outside §3 (a frozen keyboard test, `panels.py`, a factory).
- The real `DelegationService` or `AutomationManager`, once on your base, returns results that contradict §6.5 or §6.7 (for example `INVALID` from `end`), or M7-AUTO's `allowed_ops` disagrees with `automation_ops` (the drift test fails): do not change the table; ask.
- The TL rules the per-language op and input-kind names differently from §6.1. (Contract gap: P3-interfaces §12.3 names one op `ed` and §12.4 one kind `deleg_wording`, but neither a card nor a pending input can carry the language.) Until a ruling, use §6.1's names.
- `_wire_surfaces` already registers `p3d`, `p3w`, `p3a` or a `deleg_wording_*` kind for another unit.
- `test_wire_surfaces_registers_the_settings_p3_routes` finds no helper to import from F0-R's `test_p3_wiring.py`.
- You want any text, label or op that differs from this spec.
- You have spent more than one engineer-day and are not close.

## 11. Coding rules (reviewers block on these)

- No broad `except` (no bare `except`, no `except Exception`); no `assert` in production code, neither for control flow nor for type narrowing (`if card.token is None: raise RuntimeError(...)`).
- Full type hints; `mypy --strict` clean; names exactly as §6.
- No `datetime.now()`: use the `clock` you are given; store and compare in UTC; the owner's timezone is for display only.
- Content-free logs: ids, counts and fixed codes only; never names, usernames, topics, wording or reminder text.
- No Telegram call except the gateway's `answer_callback`, `edit_control_text` and `send_text`, after the commit; no aiogram shortcuts; no Bot API import.
- Escape each untrusted field exactly once with `telegram.render.escape`, before adding markup; never escape twice, never unescape.
- Synthetic test data only (invented names "Kamola" and "Bobur", `SYNTH` texts).
- GitHub: every body through `--body-file <file>`; no bare `@names`; no recurring status comments. Commits: Conventional Commits, `git commit -F <file>`; never `--no-verify`.

## 12. Pull request

- **Titles:** part A `SET-P3: /settings delegations, AI-notice wording and the panel Delegate row`; part B `SET-P3: /settings automations (pause, resume, delete)`.
- **Size:** about 400 changed lines or fewer each (expected part A ≈ 300 code + ≈ 380 tests; part B ≈ 150 code + ≈ 250 tests). If part A grows past that, move the wording screen and input into a third stacked PR, `p3/set-p3-wording`, and tell David.
- **Labels:** `team:david`, `eng:d1` on each draft; when CI is green, mark it ready and add `needs-review:j3` (part B also `needs-review:e1`) and `needs-qa`. QA is **after merge**: the TL merges on mentor and TL review; QD1 checks on `int/p3` before R1, and a finding becomes one fix PR.
- **Template** (WORKFLOW §4.3):
  - *Unit:* `WU: SET-P3 · Spec: docs/engineering/p3/specs/SET-P3.md · Issue: #<n> · Base: int/p3` (part B: `Base: p3/set-p3`); Tier: other; QA lane: after merge.
  - *Safety invariants touched:* 10 (End stays available whatever the flags: `test_end_works_with_automatic_handling_switched_off`; the kill switch is shown: `test_the_screens_say_when_automatic_replies_are_paused`); 11 (honest status: `test_end_outcomes_are_reported_truthfully`, `test_pause_resume_and_delete_outcomes_are_reported_truthfully`, `test_wording_outcomes_are_reported_truthfully`, `test_hidden_kinds_are_never_listed_counted_or_sent_to_the_port`); 12 (tenant scoping: the `test_rg21_*` tests of each part); 13 (the AI notice cannot be emptied or lose its marker: `test_wording_that_cannot_be_used_is_refused_before_the_port`); 3b (a forwarded message is never the wording: `test_a_forwarded_message_is_never_taken_as_the_wording`).
  - *Tests:* the §7 names; commands and results, or "not run locally: <reason>; CI is the gate"; RG-21 extended; the skipped `importorskip` tests named.
  - *Shared lists updated:* none.
  - *Data:* no migration, no content column; logs: `test_log_hygiene_set_p3.py`.
  - *Prompts, flags and settings:* none (reads `reminders.run`, `followups.run`, `scheduled_summaries.run` and `AutonomyConfig`).
  - *Docs:* ARCHITECTURE §6.7 (part A).
  - *Interface changes:* none; contract gaps relied on: the `ed_*` ops and `deleg_wording_*` kinds; `automation_ops` mirrors M7-AUTO's `allowed_ops`.
  - *Open items for M06.md / M07.md:* anything non-blocking you found (for example a label that reads badly in practice).
