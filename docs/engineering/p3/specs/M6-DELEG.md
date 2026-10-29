# M6-DELEG — Delegations service and tools, standing behind its flag, disclosure wording, shareable designation

| | |
|---|---|
| **Unit** | M6-DELEG (S06.1, service and tools) |
| **Team / slot** | Josh / J3 |
| **Size** | M+ (≈ 1.5 eng-days) |
| **Depends on** | F0-S (`delegations` tables, `DelegationRules`, `delegations_repo` incl. `end_delegations`, `actions/disclosure.py`, flags, `AutonomyConfig`), F0-R (`agent/tools/delegations.py` pre-created, `ToolServices` fields, `core/p3_ports.py`, `_wire_autonomy`, fire registry) |
| **Branch → base** | `p3/m6-deleg` → `int/p3` |
| **Peer reviewer** | E1 (Codex), dispatched by the TL |
| **QA slot and lane** | QJ1 or QJ2, before merge |
| **Tier** | 1 (TL scope check) |
| **Normative inputs** | [P3-interfaces](../contracts/P3-interfaces.md) §3.3, §4.5, §5, §6.1, §6.2, §9.4, §9.5, §10, §12.5, §13, §15; [P3-schema](../contracts/P3-schema.md) §2.1, §2.2, §3.2, §7.1; ARCHITECTURE §7.3, §7.5, §8.1–§8.3, §9.4; PRD J5, J12, FR-AUT-01, FR-AUT-02, FR-MEM-08, §11.1–§11.4, SEC-08; IMPLEMENTATION_PLAN §8 S06.1; decisions OD-4, OD-6, OD-7, OD-8, OD-13, P3-T19, P3-T22 (ADR-17, ADR-18) |

## 1. Goal

The owner asks in plain words for the assistant to reply for them in named chats for a time (or, behind its own flag, without an end). The owner run turns the request into strict tool arguments; a confirmation card states exactly what will happen; only the owner's Confirm creates the delegation. The owner can end it at once from any run, change its AI-notice wording without ever emptying it, and save **shareable knowledge** that a delegation may pass on. Contact text never widens autonomy (SEC-08, invariant 3b).

## 2. Scope

**In:**
1. `agent/delegation_schemas.py` (new): `CreateDelegationArgs`, `EndDelegationArgs`, `DisclosureOverrideArgs`.
2. `automation/delegations.py` (new): create-on-confirm with supersede, owner end, disclosure-wording change; `DelegationService` implementing `core.p3_ports.DelegationControls`.
3. `agent/tools/delegations.py`: `create_delegation` (configure, `always_confirm`) and `end_delegation` (control, taint-exempt) appended to its `TOOLS`.
4. `agent/tools/memory.py`: one branch, `remember(kind="shareable")`.
5. `memory/items.py`: the shareable write path (`owner_confirmed_exact`, delegation-scope notes).
6. `_wire_autonomy` lines: `DelegationControls` into `P3Ports`.
7. Tests (S06.1 #1, #2, #4, #5; RG-11 and RG-21 extensions; supersede; shareable), ARCHITECTURE §7.5 and §8.2.

**Out:**
- The `delegation_end` fire handler, the overdue reconciler and the ends on consent revoke or connection disabled (M6-LIFE).
- `list_delegations` (RD-DEL), `/settings` screens and the panel Delegate row (SET-P3, which calls `DelegationControls`), the catch-up content (M6-CATCH), the delegated draft (DELEG-DRAFT), the planner and the autonomy rules (M6-PLAN, M6-POL).
- The `owner_request.v10` prompt text (PR-OR10). This PR's description carries the rule text for the three tool changes.
- `interpret_instruction` (OD-13, ADR-18: strict arguments plus a card instead).

## 3. Design

### 3.1 Arguments (`agent/delegation_schemas.py`)

Strict schema (`strict_schema`): every field present, nullable where shown; constraints in validators (no `format` or `pattern`, M03 D52); our rules told to the model as `ArgumentRuleError` text. Field names are short on purpose: the confirmation card shows the arguments as JSON and allows 600 characters (`agent/confirmations.py::CARD_ARGS_LIMIT`; contract gap G7).

```python
GateLocale = Literal["uz-Latn", "uz-Cyrl", "ru", "en"]  # = core.autonomy_texts.GateLanguage values
BehaviourName = Literal[
    "answer_from_knowledge", "ask_for_details", "acknowledge_and_defer", "decline"
]


class DisclosureOverrideArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    locale: GateLocale
    text: str  # validator: stripped; disclosure.validate_wording(text) is None, else its problem as the rule text


class CreateDelegationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["time_bounded", "standing"]
    chats: list[int]  # conversation ids (from resolve_contact); 1-5, unique, each a valid id
    topic: str | None  # the owner's words, 1-200 chars; null = any message in these chats
    allowed: list[BehaviourName]  # 1-4, unique
    notes: list[str]  # knowledge for this delegation only, the owner's words: 0-3, each 1-200 chars
    saved_note_ids: list[int]  # saved shareable notes to attach (memory ids): 0-10, unique
    recipients: list[
        int
    ]  # contact ids who may receive the knowledge; [] = every delegated chat's contact
    anyone_on_topic: bool  # every delegated chat's contact receives the knowledge (share_widened); only with a topic
    starts: str | None  # owner-local "YYYY-MM-DD HH:MM"; null = at confirmation
    ends: str | None  # owner-local; required for time_bounded, null for standing
    per_chat_hour: int | None  # 1-30; null = AUTONOMY_LIMIT_PER_CONVERSATION_HOUR
    total: int | None  # 1-500; null = AUTONOMY_LIMIT_PER_DELEGATION
    urgent_in_quiet_hours: bool  # urgent decisions also alert during quiet hours (OD-7: false unless the owner says so)
    ai_notice: list[
        DisclosureOverrideArgs
    ]  # 0-4, unique locales; [] = the default line per language


class EndDelegationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delegation_id: int | None  # exactly one of the two
    conversation_id: int | None  # the delegation covering this chat now
```

Pure validator rules (each an `ArgumentRuleError`): sizes and bounds above; `kind = standing` ⇒ exactly one chat and `ends` null; `kind = time_bounded` ⇒ `ends` set; `starts` and `ends` parse with `agent/tools/common.py::parse_local_moment` (a date with a time is required for `ends`); `anyone_on_topic` ⇒ `topic` set; `recipients` non-empty ⇒ `topic` set ("recipients apply only to topic knowledge"); a note or the topic that starts with `core.autonomy_texts.AI_MARKER` is refused (the AI marker is added by the application, ADR-17).

Field descriptions (model-facing) say: ids come from `resolve_contact`; notes and topic are the owner's own words, never text from a forwarded or contact message unless the owner asks for exactly that text; times are owner-local.

### 3.2 The confirmation card (`describe_create`)

`create_delegation` is `always_confirm` (FR-AUT-01, invariant 3b), tainted run or not. The executor holds the call and builds a `tool_confirm` card from the arguments and this describer (`ToolDescriber(session, account_id, args) -> str`; no clock, no services).

The describer:
1. Parses the arguments; resolves every id against the account. A foreign or unknown chat, contact or memory id raises `NotInAccountError`, so the model hears "not found" and no card is shown (RG-21).
2. Reads the account's timezone and the covered chats' contact names (escaped later by the card), and the live `delegation_conversations` rows of those chats (another delegation that would be replaced).
3. Finds problems it can see without a clock: a chat that is not a business chat of this account, a bot chat or an Ignore-mode chat; a saved note that is not an active `kind = 'shareable'` item with scope `owner`, `contact` or `conversation`; a recipient who is not a delegated chat's contact; no active `ai_managed` consent; no active, enabled business connection with `can_reply`. Any problem makes the text end with "This delegation cannot start: <problem>. Confirming changes nothing." (the M05 monitor pattern; contract gap G8 explains why these cannot be refused before the card).
4. Returns, in plain text and at most 800 characters after escaping (`CARD_DESCRIPTION_LIMIT`):

```
Reply for you in: {names}. {window}.
Topic: {topic | "any message in these chats"}.
Allowed: {labels}.
Never: promise dates, money or meetings that are not in your notes; mention other chats; send media; discuss sensitive topics.
Knowledge: {n} note(s) for this delegation{, m saved note(s)}; goes to {recipients | "the contacts of these chats" | "anyone in these chats who raises the topic"}.
Limits: {per_chat_hour}/chat/hour, {total} in total | your default limits (shown after you confirm).
Urgent decisions: alert you at once{, also during quiet hours}.
AI notice: {"the default line in each language" | the custom lines}, always starting with {AI_MARKER}.
{"It replaces the current delegation in " + names + " when you confirm." if any}
Replies are sent on their own only when every check passes; otherwise they come to you as drafts or alerts.
```

`{window}`: "From {starts local | "your confirmation"} until {ends local}" or "Standing: until you end it (needs standing delegations switched on)". Labels: answer from your notes / ask for missing details / say you will follow up / politely decline. The forbidden line is fixed text (PRD §11.3; the forbidden behaviours are code and prompt rules, not stored).

When the arguments or the description exceed the card limits, the executor refuses with "too large to show on a confirmation card" (unchanged). The tool description tells the model: save longer knowledge first with `remember(kind="shareable")`, then attach it with `saved_note_ids`.

### 3.3 Create on confirm (`automation/delegations.py::confirm`)

The handler runs only with `ctx.held` (the owner tapped Confirm; the `agent/tools/monitors.py` pattern). Without `held` it returns `ToolOutcome("not_executed", {"reason": "a delegation starts only after the owner taps Confirm on its card"}, log_code="not_confirmed")`.

`ToolServices.autonomy_config` is `None` → `ToolOutcome("error", {"reason": "delegations are not available right now"}, log_code="not_wired")`, nothing written (a missing service is a factual "not available", P3-interfaces §9.4). Otherwise the tool handler maps the arguments to a domain value and calls the service in one transaction (`db.uow.transaction`):

```python
@dataclass(frozen=True, slots=True)
class DelegationRequest:
    kind: str  # "time_bounded" | "standing"
    conversation_ids: tuple[int, ...]
    topic: str | None
    allowed: tuple[Behaviour, ...]
    notes: tuple[str, ...]
    knowledge_ids: tuple[int, ...]
    recipients: tuple[int, ...]  # () = every delegated chat's contact
    share_widened: bool
    starts_at: datetime | None  # UTC; None = at confirmation
    ends_at: datetime | None  # UTC
    per_chat_hour: int | None
    total: int | None
    urgent_bypass_quiet_hours: bool
    disclosure: Mapping[str, str]  # GateLanguage value -> validated wording


@dataclass(frozen=True, slots=True)
class ConfirmOutcome:
    delegation_id: int
    created: bool  # False: this (run_id, tool_call_id) already created it
    starts_at: datetime
    ends_at: datetime | None
    replaced_chat_ids: tuple[int, ...]
    superseded_ids: tuple[int, ...]


class DelegationProblem(Exception):
    """A factual reason the delegation cannot start; `code` is an enum for logs."""

    code: str


async def confirm(
    session: AsyncSession,
    *,
    account_id: int,
    request: DelegationRequest,
    run_id: int | None,
    tool_call_id: str | None,
    tainted: bool,
    origin_refs: Mapping[str, list[int]]
    | None,  # run_origin(...).refs as JSON, for the notes' provenance
    now: datetime,
    config: AutonomyConfig,
    catch_up: CatchUpRequester | None,
) -> ConfirmOutcome: ...
```

`automation` is a domain package: it imports no `agent` type (import-linter contract 1), so the tool handler converts `run_origin(...)` (an agent value) to `tainted` and plain JSON refs before calling.

Steps, in the caller's transaction, lock order account → conversation → delegation → action (ARCHITECTURE §9.4; P3-schema §2.2):
1. Account row `FOR SHARE`.
2. Covered conversations `FOR UPDATE`, ordered by id.
3. Idempotency, **under those locks**: a delegation with this `(run_id, tool_call_id)` exists → return it with `created=False` (no other write). Checking before the locks would let a racing duplicate of the same call wait at step 2, then treat the first one's fresh delegation as an older one and supersede it.
4. Re-check each covered chat and the arguments (problems as in §3.2 step 3). Problem codes: `chat_not_delegable`, `no_managed_consent`, `no_reply_rights`, `note_not_shareable`, `recipient_not_covered`.
5. Times: `starts_at = max(now, request.starts_at or now)` (an earlier time means "now"); `starts_at ≤ now + 30 days` else `start_too_far`; time-bounded: `ends_at > starts_at` and `ends_at > now` else `end_passed`, `ends_at − starts_at ≤ 30 days` else `too_long` (**TL decision (spec)**: a guard against typos; a longer need is a standing delegation). Standing: `config.standing_enabled` else `standing_off`; exactly one chat (also the DB CHECK).
6. Rules: `DelegationRules(conversation_ids=chats, topic, allowed, knowledge_ids, knowledge_contact_ids = recipients or every delegated chat's contact, share_widened, limits=DelegationLimits(per_conversation_hour = per_chat_hour or config.limit_per_conversation_hour, per_delegation = total or config.limit_per_delegation), escalation=DelegationEscalation(urgent_bypass_quiet_hours=…), disclosure=dict(request.disclosure))`. A limit above the setting is allowed only because the owner saw it on the card (OD-6).
7. **Supersede** (OD-7; P3-schema §2.2; R11). Find the live `delegation_conversations` rows of the covered chats that belong to other delegations; lock those delegations `FOR UPDATE` ordered by id. For each older delegation: set `ended_at = now` on its live rows in the shared chats; `lifecycle.cancel_pending_for_delegations(session, account_id=…, delegation_ids=[old], reason=StateReason.DELEGATION_ENDED, now=now, conversation_ids=shared)`; if it has no live row left, `delegations_repo.end_delegations(session, account_id=…, delegation_ids=[old], reason="superseded", now=now)`. An older delegation that keeps other chats stays active for them. An `executing` autonomous action of the older delegation is never touched (its send already started; its outcome is recorded).
8. Insert the `delegations` row: `kind`, `spec = rules.model_dump(mode="json")`, `refs = {"conversation_ids": chats, "contact_ids": delegated chats' contacts ∪ recipients}` (sorted, ids only), `status = 'active'`, `starts_at`, `ends_at`, `confirmed_at = now`, `share_widened` (same value as in the rules, same statement), `run_id`, `tool_call_id`. A unique violation on `uq_delegations_run_id_tool_call_id` cannot happen under step 2's locks; if it does, let it raise so the whole transaction rolls back (never a half-applied supersede).
9. Insert one `delegation_conversations` row per chat (the partial unique index proves one live delegation per chat).
10. Notes: `memory/items.py::save_delegation_knowledge(...)` (§3.6) writes one `scope = 'delegation'`, `kind = 'shareable'`, `source = 'owner_stated'` item per note.
11. Time-bounded only: the `delegation_end` automation through `automation/schedules.py::create_scheduled(session, account_id=…, kind="delegation_end", rule=DelegationEndRule(delegation_id=id), spec=ScheduleSpec(timezone=account.timezone, tz_policy="fixed", run_at=ends_at), clock=…, refs=<the delegation's refs>, confirmed_at=now)`. The same `refs` let deletion find it (P3-schema §7.2). A standing delegation gets none.
12. Audit `delegation_created` (`subject_type = delegation`, actor `owner`): `kind`, `conversations` (count), `share_widened`, `starts_at`, `ends_at` (epochs or null).
13. For each id returned by step 7's `end_delegations`: `catch_up.request(session, account_id=…, delegation_id=old, now=now)`; when `catch_up` is `None`, log `catch_up_not_wired` (delegation id only) and continue.

A `DelegationProblem` rolls the transaction back; the handler returns `ToolOutcome("error", {"reason": <the problem in plain words>}, log_code=<code>)` and the applier tells the owner it failed (invariant 11). Success returns `ToolOutcome("ok", {"delegation_id", "kind", "starts": local, "ends": local or null, "chats": n, "replaced_chats": n})`, no names; a repeat returns `{"changed": False, "note": "already created"}` (the applier then says "nothing to change").

After commit, and only after commit, the handler sends one notice through `ToolServices.channel` (the `monitors.queue_notice` pattern: `db.uow.after_commit`; a rollback sends nothing; a failed or uncertain send is not resent; plain HTML, names escaped once with `html.escape`, the escape-once rule). It carries the fixed Pause button. An agent tool may not import `assistant.telegram` (import-linter contract 2), so the button comes from F0-S's pure helper `core/autonomy_texts.py::pause_button()` (TL ruling R45; on the base from CP1). The notice appends it from the start and its test is a normal test.

```
🤝 <b>{"Delegation on" | "Delegation scheduled"}</b> for {names}, {window}.
Limits: {per_chat_hour}/chat/hour, {total} in total.
{mode line}
End it any time: /settings → Delegations, or tell me "end the delegation". /pause stops every automatic reply.
```

"Delegation scheduled" when `starts_at > now` (nothing is covered yet; invariant 11). The limits are the effective ones stored in `spec` (the card may only have said "your default limits", contract gap G7), so the owner sees the real numbers at once and can end the delegation.

Mode line, from `ToolServices.autonomy_config` at that moment (truthful, invariant 11): `run_enabled` false → "Automatic handling is switched off, so nothing changes in these chats yet."; `send_enabled` false → "Shadow mode: replies that pass every check come to you as drafts; nothing is sent on its own."; no enabled language → "No language is switched on for automatic replies yet, so replies come to you as drafts."; otherwise → "Replies that pass every check are sent on their own with the AI notice, in: {languages}. Everything else comes to you as a draft or an alert." (`autonomy_config` is never `None` here: the handler refused above.)

### 3.4 Owner end and `end_delegation`

```python
async def end_owner(
    session: AsyncSession,
    *,
    account_id: int,
    delegation_id: int,
    now: datetime,
    catch_up: CatchUpRequester | None,
) -> PortResult: ...
```

Account `FOR SHARE` → `delegations_repo.get` (`None`, including a scrubbed spec, → `NOT_FOUND`) → status not `active` → `ALREADY` → `end_delegations(…, reason="owner_ended")` (it locks the delegation row and then the actions, P3-schema §7.1); an empty result (a racing end) → `ALREADY`; otherwise the catch-up request (as §3.3 step 13) → `DONE`. Never flag-gated: it works with `autonomy.run` off (P3-interfaces §4.5).

`end_delegation` tool (class `control`, flag `tool.end_delegation`, already in `TAINT_EXEMPT_TOOLS`, so it runs in a tainted run; no card):
- Exactly one of `delegation_id` and `conversation_id` (validator, `common.ONE_TARGET_RULE` wording).
- `conversation_id`: `common.require_conversations` (foreign → "not found"); `delegations_repo.active_covering(…, now)`; none → `ToolOutcome("ok", {"ended": False, "note": "no active delegation covers that chat"})`.
- Calls `end_owner`; `NOT_FOUND` → raises `NotInAccountError` ("not found"); `ALREADY` → `{"delegation_id": id, "ended": False, "note": "already ended"}`; `DONE` → `{"delegation_id": id, "ended": True}`. Idempotent by construction (an ended delegation stays ended), so a retried call cannot double anything.

### 3.5 Disclosure wording (`DelegationControls.set_disclosure_wording`)

Called by SET-P3's `deleg_wording` pending input, with the owner's own next message only (a forwarded or quoted message is never an answer, M03 D49; SET-P3 enforces it). The wording goes into messages to contacts, so it is never taken from contact text or a model call. In the caller's transaction:
1. `locale` not a `GateLanguage` value → `INVALID`. `text.strip()`; `disclosure.validate_wording(text)` not `None` → `INVALID` (empty, too long, several lines, markup). The wording can never be emptied, and the AI marker is not part of it: `compose` always prepends it (OD-4, ADR-17).
2. `SELECT … FROM delegations WHERE id = :id AND account_id = :a FOR UPDATE`; none, or `spec` NULL → `NOT_FOUND`; status not `active` → `INVALID`; the stored spec fails `DelegationRules.model_validate` → `INVALID` and log `delegation_spec_invalid` (id only).
3. The same text as stored for that locale → `ALREADY`.
4. Otherwise rewrite `spec` with `disclosure[locale] = text` (validated through `DelegationRules`), `updated_at = now`; audit `delegation_wording_changed` (`locale` only) → `DONE`.

Actions already proposed keep their stored text; the change applies to replies composed afterwards. Nothing here is flag-gated.

`DelegationService(catch_up: Callable[[], CatchUpRequester | None])` implements `DelegationControls`: `end` calls `end_owner(…, catch_up=self._catch_up())`; `set_disclosure_wording` calls §3.5. It holds no session and no client (AGENTS §5).

### 3.6 Shareable knowledge (`memory/items.py`, `agent/tools/memory.py`)

`memory/items.py` (M6-DELEG owns the shareable write path):
- `provenance(..., owner_confirmed_exact: bool = False)` adds `"owner_confirmed_exact": true` only when true (a backward-compatible jsonb key, P3-schema §3.2).
- `save(..., owner_confirmed_exact: bool = False)` passes it through; for `kind = "shareable"` the `memory_saved` audit gains `kind: "shareable"` and `owner_confirmed_exact` (P3-interfaces §13). Every other call behaves exactly as before.
- New `save_delegation_knowledge(session, *, account_id: int, delegation_id: int, texts: Sequence[str], tainted: bool, refs: Mapping[str, list[int]] | None, now: datetime) -> list[int]`: one item per text, `scope = 'delegation'`, `kind = 'shareable'`, `source = 'owner_stated'`, `delegation_id`, provenance `tainted`/`managed` = `tainted`, `refs` when tainted, and `owner_confirmed_exact = true` (the owner saw these exact words on the card), with one `memory_saved` audit row each (actor `owner`). Texts are stripped, 1–200 characters.

`remember` gains one branch (`MemoryKind` gains `"shareable"`; the `kind` field description says "shareable: the owner's own words that a delegation may pass on to contacts"):
1. `tool.create_delegation` not in `ToolServices.enabled_flags` → `ToolOutcome("error", {"reason": "shareable knowledge is not available"}, log_code="shareable_off")`.
2. `origin = run_origin(...)` (unchanged). `origin.tainted and not ctx.held` → `ToolOutcome("error", {"reason": SHAREABLE_NEEDS_CARD}, log_code="shareable_untrusted")`, where the text says this run saw other people's messages, so the owner should send the exact words to share in a new message. (A tainted run never gets here: the gate holds every `configure` call in a tainted run for a card.)
3. `ctx.held` (the owner confirmed a card that showed the exact text) → `save(..., kind="shareable", tainted=True, managed=True, refs=origin.refs, owner_confirmed_exact=True)`.
4. A clean run → `save(..., kind="shareable")` and the usual echo with Undo ("📝 Saved shareable knowledge {about}: <i>…</i>").

Scope comes from the target, as for every note: `contact_id` → that contact, `conversation_id` → that chat, neither → `owner` (saved for anyone the owner designates on a delegation card, OD-8). `describe_remember` adds "shareable: a delegation may pass these exact words on to contacts" for this kind. `shareable_for` (F0-S) then returns an item to a delegation only under its rules (P3-interfaces §6.2).

`set_conversation_mode` keeps rejecting `delegate` (its `mode` Literal has no such value; Delegate is never a stored mode, PRD §11.1): no code change, a pinning test.

### 3.7 Wiring (`bootstrap.py`, inside `_wire_autonomy` only)

- `ports.delegation_controls = DelegationService(catch_up=lambda: ctx.final_ports().catch_up_requester)`.
- The tools need no wiring: they are in `agent/tools/delegations.py::TOOLS` and read `ToolServices`.

Both the service and the tool handlers need the catch-up requester, which `_wire_surfaces` provides after `_wire_autonomy` has run. F0-R's spec (§3.3, §3.4; contract gaps G5 and G6) adds the two seams this uses: `P3WireContext.final_ports() -> P3Ports`, a late-bound accessor valid once all three wire functions ran (it raises if called during wiring, so capture it and call it only at run time), and `ToolServices.p3_ports: P3Ports | None`, the final ports, since bootstrap builds the single `ToolServices` after the wire functions. The create handler passes `services.p3_ports.catch_up_requester` (or `None` when `p3_ports` is `None`); `end_delegation` calls `end_owner` with the same.

### 3.8 Edge cases (each has a test in §5)

Confirm tapped after the end time passed; a start in the past; a start more than 30 days ahead; a chat switched to Ignore between the card and the tap; consent revoked between the card and the tap; two Confirms on the same card (the card is single use), an applier retry, and a duplicate apply of the same call racing the first; a rollback after the notice was queued; a newer delegation for one of two chats of an older one; a newer delegation for the only chat of an older one, with a scheduled autonomous reply there; a standing request while `delegations.standing` is off; a note that starts with the AI marker; a saved note id that is a `pending_review` suggestion; a foreign id in every id field; `end_delegation` on an ended delegation and on a chat with no delegation; a wording with a newline; ending while `autonomy.run` is off.

## 4. Files

**Owned (P3-interfaces §15):**
- `automation/delegations.py` (new), `agent/delegation_schemas.py` (new).
- `agent/tools/delegations.py` (F0-R pre-created it with `TOOLS = ()`; M6-DELEG is its only editor).
- `memory/items.py`: the shareable write path only (`provenance`, `save` keyword, `save_delegation_knowledge`).
- `agent/tools/memory.py`: the `remember` shareable branch, its `MemoryKind`, field description and `describe_remember` line only.
- Test files: `tests/integration/test_delegations.py`, `tests/integration/test_delegation_tools.py`, `tests/integration/test_shareable_memory.py`, `tests/unit/test_delegation_schemas.py`, `tests/integration/test_log_hygiene_m6_deleg.py`.

**Named touch points:** `bootstrap.py` (`_wire_autonomy` lines only); `docs/ARCHITECTURE.md` §7.5 and §8.2 (§7).

**Consumed, never edited:** `automation/{delegation_models,delegation_rules,delegations_repo}.py`, `memory/shareable.py`, `actions/{disclosure,lifecycle,config}.py`, `core/{autonomy_texts,p3_ports,flags}.py`, `automation/{schedules,rules,repository}.py`, `agent/{confirmations,untrusted}.py`, `agent/tools/{registry,common,echo}.py`.

**Never edit without an ICR** (§15.2): `policy/*`, the send-path `actions/` files (this unit only calls `lifecycle.cancel_pending_for_delegations`), `agent/{runtime,context,schemas}.py`, `agent/tools/executor.py`, migrations, `privacy/*`, `config.py`, `core/*`, any prompt file.

## 5. Tests (red first)

Real PostgreSQL; synthetic text; `FakeClock`, `FakeLLM` where a run is needed (`tests/integration/agent_env.py`), recording `FakeChannel`, a recording fake `CatchUpRequester`. Delegations for setup come from `p3_factories.seed_delegation`.

**IMPLEMENTATION_PLAN §8 S06.1** (`tests/integration/test_delegations.py`):
- `test_delegation_spec_validation` (#1, "spec validation"; parametrised; each case: no delegation row, a factual error): no chats; six chats; a repeated chat; a bot chat; an Ignore-mode chat; empty `allowed`; standing with two chats; standing with `ends`; time-bounded without `ends`; `ends` before `starts`; `ends` already past at the tap; more than 30 days; a start more than 30 days ahead; `anyone_on_topic` without a topic; a recipient who is not a delegated chat's contact; a saved note that is not shareable or is `pending_review`; an AI-notice wording that is empty, too long, multi-line or has markup; a repeated locale; four notes; a 201-character note; a note starting with the AI marker; `per_chat_hour = 31`.
- `test_a_delegation_starts_only_after_the_owner_confirms_its_card` (#2, "confirmation required"): a clean run's call → `awaiting_confirmation`, a card, no row; Reject → no row; a second call and Confirm → one active delegation, its chat rows, its `delegation_end` automation, audit `delegation_created`, and the "Delegation on" notice.
- `test_a_standing_delegation_requires_confirmation_and_its_flag` (#4): with `delegations.standing` off the card shows but Confirm fails with "standing delegations are not switched on"; with it on, Confirm creates `kind = standing`, `ends_at` NULL, one chat, no `delegation_end` automation.
- `test_set_conversation_mode_rejects_delegate` (#5; pins): `mode = "delegate"` is a validation error; nothing changes.

**RG-11 and RG-21 extensions** (`tests/integration/test_delegation_tools.py`):
- `test_rg11_create_delegation_in_a_tainted_run_offers_only_a_card`: tainted and clean runs both get `awaiting_confirmation`; nothing is created until the tap.
- `test_rg11_end_delegation_runs_in_a_tainted_run`: ends at once (taint-exempt).
- `test_rg11_shareable_text_from_a_tainted_run_is_saved_only_from_its_card` ("shareable from a tainted run only verbatim on a card"): the call is held; the card shows the exact text; Confirm saves it with `owner_confirmed_exact = true`, tainted and managed.
- `test_rg21_delegation_tools_and_controls_refuse_another_accounts_ids`: a foreign chat, recipient or saved note in `create_delegation` → "not found" before any card; a foreign `delegation_id` in `end_delegation` → "not found"; `DelegationControls.end` and `set_disclosure_wording` with a foreign id → `NOT_FOUND`, nothing changed.

**Service and tools:**
- `test_the_delegation_card_states_everything_the_owner_confirms`: the description names the chats, window, topic, allowed behaviours, the fixed never-list, recipients, limits, urgent rule and AI notice; a replaced delegation is named.
- `test_a_card_that_would_not_fit_is_refused_as_too_large`.
- `test_confirming_a_newer_delegation_supersedes_the_older_in_shared_chats` (OD-7): the older keeps its other chat and stays active; its live row in the shared chat is ended.
- `test_supersede_cancels_the_older_delegations_scheduled_reply_in_the_shared_chat` (R11): `cancelled/delegation_ended`; its reply in its other chat is untouched.
- `test_an_older_delegation_left_without_chats_ends_superseded_with_a_catch_up`: `ended_reason = superseded`, one catch-up request.
- `test_a_time_bounded_delegation_gets_one_delegation_end_automation_and_a_standing_one_none`: `kind = delegation_end`, `run_at = ends_at`, `refs` equal to the delegation's.
- `test_delegation_refs_name_every_chat_contact_and_recipient`.
- `test_delegation_notes_are_saved_as_delegation_scope_shareable_items_confirmed_exact`; and `shareable_for` returns them for that delegation and chat only.
- `test_limits_default_to_the_settings_and_a_raised_limit_is_kept`.
- `test_create_delegation_is_idempotent_on_its_call`: an applier retry creates nothing new.
- `test_a_duplicate_apply_racing_the_first_never_supersedes_it`: two transactions apply the same `(run_id, tool_call_id)`; barrier-controlled so the second waits on the conversation lock; the second returns `created=False`, the first delegation stays active with its live chat rows, one `delegation_created` audit row, no `superseded` end, no catch-up.
- `test_create_delegation_needs_managed_ai_consent_and_reply_rights`.
- `test_a_start_in_the_past_means_now`.
- `test_end_delegation_ends_at_once_and_requests_a_catch_up`: `owner_ended`, scheduled replies cancelled `delegation_ended`, delegation notes deleted, one catch-up request; by `conversation_id` too.
- `test_end_delegation_is_idempotent_and_reports_already_ended`.
- `test_ending_works_with_autonomy_switched_off`.
- `test_a_missing_catch_up_port_never_blocks_an_end` (`catch_up=None`: ended, logged, no error).
- `test_the_disclosure_wording_can_be_changed_but_never_emptied`: `DONE`, `ALREADY`, `INVALID` for empty, multi-line, markup and an unknown locale, `INVALID` for an ended delegation; one `delegation_wording_changed` audit row.
- `test_the_delegation_notice_states_the_effective_mode_and_limits` (parametrised over the four mode lines; "Delegation scheduled" for a future start; the stored limits, including defaults from settings).
- `test_the_delegation_notice_is_sent_only_after_commit`: a confirm whose transaction rolls back after the notice was queued sends nothing; a committed confirm sends exactly one notice.
- `test_the_delegation_notice_carries_the_pause_button`: one button equal to `core.autonomy_texts.pause_button()`, data `PAUSE_CALLBACK_DATA` (`pz:pause:p`; R45).
- `test_delegation_tools_are_hidden_while_their_flags_are_off` (registry filtering).
- `test_create_delegation_without_autonomy_config_is_not_available`: `ToolServices.autonomy_config = None` → a factual error, no row, no notice.

**Shareable memory** (`tests/integration/test_shareable_memory.py`):
- `test_remember_shareable_is_refused_while_delegations_are_off`.
- `test_remember_shareable_in_a_clean_run_is_saved_and_echoed_with_undo` (no `owner_confirmed_exact`, untainted).
- `test_a_shareable_note_from_an_untrusted_run_that_was_not_held_is_refused` (a forwarded message in the history window; nothing saved).
- `test_other_memory_kinds_behave_as_before` (pins: `fact` saved with the same provenance and audit as in P2).

**Schemas** (`tests/unit/test_delegation_schemas.py`): `test_delegation_args_have_a_strict_schema_without_format_or_pattern`; `test_end_delegation_needs_exactly_one_target`; `test_the_gate_locales_equal_gate_language`.

**Log hygiene** (`tests/integration/test_log_hygiene_m6_deleg.py`): `test_delegation_paths_log_ids_only` — canary text in the topic, a note, the wording and a contact name; create, supersede, end and the wording change log and audit no canary.

**Scenarios:** QA-S6 (QJ1) owns `tests/scenarios/m06/`. When a strict-xfail marked `awaiting M6-DELEG` turns green, tell QJ1, who removes the marker in a commit on this PR's branch (P3-interfaces §15.1).

Run locally: these files plus `tests/integration/test_memory_tools.py`, `tests/integration/test_tool_confirmations.py`, `tests/unit/test_tool_registry.py`; ruff, format, mypy on `src/assistant/{automation,agent,memory}`; `lint-imports`.

## 6. Acceptance criteria

1. A delegation exists only after the owner's Confirm on a card that states chats, window, topic, allowed and forbidden behaviours, knowledge and its recipients, limits, urgent rule and AI notice; tainted or not.
2. One live delegation per chat; a newer delegation supersedes in shared chats and cancels the older one's pending autonomous replies there in the same transaction.
3. `end_delegation` and `DelegationControls.end` end at once, in any run and with any flag state, and request one catch-up.
4. The AI-notice wording can be changed per locale and never emptied; the AI marker is never part of it.
5. Shareable knowledge from other people's text is stored only from a card that showed the exact words (`owner_confirmed_exact`); nothing shareable is saved while `tool.create_delegation` is off.
6. Every named test in §5 passes in CI; RG-11 and RG-21 extended; no RG test weakened.
7. Logs and audit rows carry ids, enums, counts and epochs only.
8. ARCHITECTURE §7.5 and §8.2 updated; the PR description carries the `owner_request` rule text for PR-OR10.

## 7. Docs updated in the same PR

- ARCHITECTURE **§7.5**: `DelegationRules` as built (fields, the mapping from `CreateDelegationArgs`, the 30-day guard, the defaults from settings), replacing the `DelegationSpec`/`interpret_instruction` text that F0-C part 2 marks as superseded (ADR-18).
- ARCHITECTURE **§8.2**: rows for `create_delegation` (configure, always a card) and `end_delegation` (control, immediate, taint-exempt); the `remember` row gains `kind = shareable` (refused while `tool.create_delegation` is off; from other people's text only through a card).
- PR description: the rule text for `owner_request.v10` (when to create a delegation, ids from `resolve_contact`, notes and topic in the owner's words, never invent knowledge, end on request, `remember(kind="shareable")` only for words the owner wants contacts to receive).

## 8. Owner decisions

- **OD-4:** the AI notice is application text with a per-locale override that can never be emptied. A different answer (for example no override) removes `ai_notice` and §3.5.
- **OD-6:** limit defaults come from settings; the card may lower or raise them. A different answer (raising forbidden) adds a validator rule `per_chat_hour ≤ setting`.
- **OD-7:** explicit chats only, one live delegation per chat with supersede, widening off unless ticked, standing behind its flag, urgent bypass off by default. A topic-only scope would need new coverage logic in `delegations_repo` (an ICR).
- **OD-8:** both per-delegation notes and saved shareable knowledge. "Per delegation only" removes the `remember` branch and `saved_note_ids`.
- **OD-13 (ADR-18):** strict arguments plus a card. An `interpret_instruction` call would replace §3.1's mapping, not the service.

## 9. Risks and notes

- **Contract gap G1** (Pause button for non-telegram code), ruled R45: the notice uses F0-S's core helper `pause_button()`; no port and no option involving `P3Ports` exists.
- **Contract gaps G5 and G6** (late-bound catch-up requester; `ToolServices.p3_ports`): F0-R's spec adds both; the contract still has to name them. Without them a delegation ended by the tool or by supersede gets no catch-up; this spec then logs `catch_up_not_wired` and never fails an end because of it.
- **Contract gap G7** (card limits 600/800): the compact field names, ≤ 5 chats and ≤ 3 notes of ≤ 200 characters keep a typical card inside the limits; longer knowledge goes through saved shareable notes. If F0-R adds per-tool card limits or gives describers the services (so the card can show effective limits, the standing flag and enabled languages), the note limits and the "your default limits" line can change without touching the service.
- **Contract gap G8:** the describer can refuse only foreign ids before the card; Ignore and bot chats show a card that says it cannot start, and Confirm changes nothing.
- **`remember`'s schema changes** (one more `kind` value) for every `owner_request` run. PR-OR10's retrieval agent-mode re-run covers it; until `tool.create_delegation` is on, the value is refused.
- **Tainted provenance on delegation notes:** a held call always counts as tainted (`run_origin`, M03 N1), so every delegation note is stored tainted and managed with `owner_confirmed_exact`. Managed-data deletion ends active delegations anyway (P3-schema §7.2), so nothing is lost that should stay.
- **Lock order:** create takes account, conversations, delegations, actions in that order; Tx A and the release job take the same order, so a claim either finishes before the supersede or sees the cancelled action.
