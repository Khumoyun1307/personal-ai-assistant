# M7-MRV — Memory review cards: approve, edit, discard (junior unit)

| | |
|---|---|
| **Unit** | M7-MRV — the `memory_review` renderer (one card per inferred suggestion), the `mem` card presses Approve / Edit / Discard, the `mem_edit` text input that follows Edit, and the domain functions that change a `pending_review` item with one content-free audit row each. **Card-only:** no agent tool can approve, edit or discard a suggestion |
| **Owner slot** | D2 (David's team, junior) |
| **Mentor** | E2 (Codex; the M7-MEX owner, who writes the suggestions and enqueues the cards; peer review dispatched by the TL) |
| **QA** | QD1, **after merge** (on `int/p3` before R1; depth: the three outcomes, isolation after approval, the card-only guard, scoping) |
| **Size** | S (≈ 0.5 engineer-day, one fix round included) |
| **Depends on** | F0-S: `memory_items` with `status = 'pending_review'`, `source = 'agent_inferred'`, `category` and their CHECKs, `NotificationKind.MEMORY_REVIEW` and its delivery class, `NotificationRefs.memory_ids` (R75: the review card's memory item id travels in `notifications.refs`, never in the dedupe key), `memory/models.py` constants (`SOURCE_AGENT_INFERRED`, `STATUS_PENDING_REVIEW`, `STATUS_ACTIVE`, `STATUS_ARCHIVED`, `MEMORY_CATEGORIES`), `privacy/retention.py::PENDING_REVIEW_RETENTION`, `p3_factories.seed_memory_item`. F0-R: `ControlRoutes.pending_inputs`, `_wire_surfaces`, `P3WireContext.notification_delivery`. **GW-ESC** landed in P2 #133 (verification only; no P3 renderer merges before that verification is closed, WORKFLOW §7). Not M7-MEX: tests seed suggestions and notifications with factories; with M7-MEX merged the cards arrive by themselves |
| **Branch → base** | `p3/m7-mrv` from `origin/int/p3` → PR to `int/p3` |
| **Normative inputs** | [P3-interfaces](../contracts/P3-interfaces.md) §1.1 (`memory/review.py`, `telegram/control/memory_review.py`), §11.1 (`NotificationRefs.memory_ids`, R75), §11.2–§11.3 (`memory_review`: held_in_quiet, high; dedupe `memreview:{local_date}:{memory_id}`; renderer rules), §12.3 (`mem`: `ok`, `ed`, `rm`), §12.4 (`mem_edit`), §12.5 (Memory review card: card-only), §13 (`memory_suggestion_approved`, `_edited`, `_discarded`), §15.1, §15.3; [P3-schema](../contracts/P3-schema.md) §3.2 (inferred suggestions, CHECKs), §6 (14-day retention); decisions OD-15, OD-16, P3-T52, P3-T56; [specs/GW-ESC.md](GW-ESC.md) §3.3 (escape-once rule, test rule 6); AGENTS §6 (invariants 3b, 4, 11, 12); ARCHITECTURE §7.3 (`contact_facing`); PRD FR-MEM-05, FR-MEM-06, §11.2 ("inferred memory needs a confirmation card"); IMPLEMENTATION_PLAN S07.4 |

## 1. Goal

When the assistant infers a note about a contact from a chat (M7-MEX), the note is stored as a suggestion (`pending_review`) that no prompt ever sees. The owner gets a review card: "💡 Suggested note about Kamola: “Prefers voice messages after 18:00” — Approve / Edit / Discard". **Approve** makes it an active note, used only for the chat it came from. **Edit** asks for the owner's own words and saves those instead. **Discard** deletes it. Only the owner's taps do this: no agent tool can approve a suggestion (invariant 3b; a test enforces it). Every outcome is reported truthfully, and every change writes one audit row with ids only.

## 2. Read first

1. [handoffs/junior-engineers.md](../handoffs/junior-engineers.md), all of it.
2. [WORKFLOW](../WORKFLOW.md) §2, §3, §4 (PR template), §5, §6.2, §6.5 ("Escape once", "Wire functions").
3. `src/assistant/automation/monitors.py::MonitorMatchRenderer` (a renderer that creates its own card in the delivery's claim transaction and returns `RenderedAlert(..., extra_buttons=…)`) and `src/assistant/telegram/control/monitors.py::handle_mon_press` (the press pattern you copy).
4. `src/assistant/inbox/alert_texts.py`: `RenderedAlert`, `Renderer`, and its private `_ids(refs, key)` (the pattern for reading ids out of `notification.refs`: write your own small helper, never import the private one); `src/assistant/inbox/notifications.py`: `_refs_json` (how refs are stored), `register_renderer` and `_card` (why the renderer returns `conversation_id=None`).
5. `src/assistant/accounts/service.py`: `create_card`, `claim_card`, `set_pending_input`, `clear_pending_input`; `src/assistant/telegram/control/cards.py::card_keyboard`.
6. `src/assistant/memory/items.py`: `MAX_CONTENT`, `contact_facing` (the isolation rule an approved note follows), `_audit` (the audit-row shape).
7. `src/assistant/telegram/control/dispatcher.py`: precedence rules 1–3 (a command cancels a pending question; a stop word pauses; a forwarded or quoted message is never an answer).
8. This spec, to the end.

## 3. Allowed files

| Path | New or existing | Exactly what may change |
|---|---|---|
| `src/assistant/memory/review.py` | new | The whole module (§6.1) |
| `src/assistant/telegram/control/memory_review.py` | new | The whole module (§6.2–§6.5) |
| `src/assistant/bootstrap.py` | existing | Inside `_wire_surfaces` only: the M7-MRV block of §6.6 (and the two shared opening lines of §6.6 if no David unit added them yet). Function-local imports only; the module import block is frozen |
| `docs/ARCHITECTURE.md` | existing | §6.7 only: one new bullet "**Memory review cards (as built, M7-MRV).**", added as the last bullet of §6.7 (just before the §6.8 heading, whatever the last bullet is on your base). Your mentor checks the wording |
| `tests/unit/test_memory_review_card_only.py` | new | §7.1 |
| `tests/integration/test_memory_review_cards.py` | new | §7.2 (not `test_memory_review.py`: that name belongs to an M03 test) |
| `tests/integration/test_log_hygiene_m7_mrv.py` | new | §7.3, copied from F0-E's template `tests/unit/test_log_hygiene_f0e.py` |

## 4. Forbidden

- `src/assistant/policy/`, `src/assistant/actions/`, `src/assistant/telegram/gateway.py`, `src/assistant/telegram/render.py`, `src/assistant/telegram/ingress/`, `migrations/`, `src/assistant/agent/` (all of it: no tool, no prompt, no `prompt_registry.py`, no `agent/tools/memory.py`), `src/assistant/privacy/`, consent code (`src/assistant/llm/consent.py`), `src/assistant/config.py`, `src/assistant/core/`, `src/assistant/jobs/`, `AGENTS.md`.
- `src/assistant/memory/{items,models,inferred,sensitive_filter,undo,shareable}.py` (you **import** `items.MAX_CONTENT` and `models`, never edit them; `inferred.py` is M7-MEX's, `sensitive_filter.py` SENS's).
- `src/assistant/inbox/*` (including `notification_types.py`, `notifications.py`, `alert_texts.py`), `src/assistant/accounts/*`, `src/assistant/conversations/*`, `src/assistant/telegram/control/{dispatcher,routes,handlers,panels,cards}.py`.
- `tests/fakes/*`, `tests/integration/{p2_factories,p3_factories,business_env,agent_env}.py`, `tests/integration/test_memory_review.py` (M03's), `tests/integration/test_p3_wiring.py` (F0-R's: you import its helper, never edit it), `tests/scenarios/**` (QA's).
- Every other unit's files, `pyproject.toml`, `uv.lock` (no new dependency), `.env*`, `var/`.
- **Every file not in §3.**

## 5. Shared lists you must update

None. The `mem` callback kind, the `mem_edit` pending-input kind and the `memory_review` renderer are registered only in your `_wire_surfaces` block; F0-R's merge raises at startup on a duplicate. The three audit-event names are constants in your own module (P3-interfaces §13: "module-level constants in its own module"). The delivery class of `memory_review` is already in F0-S's table (held_in_quiet, high; P3-T52); M7-MEX sets the priority.

## 6. Step-by-step

### 6.1 Domain functions (`src/assistant/memory/review.py`)

```python
"""Owner review of inferred memory suggestions (P3 M7-MRV; FR-MEM-05, PRD §11.2, OD-16).

A suggestion is a `memory_items` row with `source = 'agent_inferred'` and
`status = 'pending_review'` (written by M7-MEX; never shown to any prompt).
Only the owner's taps on its review card, and the `mem_edit` text that follows
Edit, change it: approve makes it an active note; edit replaces its words with
the owner's and makes it active; discard deletes it. No agent tool calls this
module (card-only; tests/unit/test_memory_review_card_only.py). Every function
runs in the caller's transaction, filters on the account, never commits, and
writes one content-free audit row for a change."""

EVENT_SUGGESTION_APPROVED: Final = "memory_suggestion_approved"
EVENT_SUGGESTION_EDITED: Final = "memory_suggestion_edited"
EVENT_SUGGESTION_DISCARDED: Final = "memory_suggestion_discarded"


@dataclass(frozen=True, slots=True)
class Suggestion:
    memory_id: int
    scope: str  # "contact" | "conversation"
    contact_id: int | None
    conversation_id: int | None
    category: str | None
    content: str
    status: str  # "pending_review" | "active" | "archived"


async def get_suggestion(
    session: AsyncSession, *, account_id: int, memory_id: int
) -> Suggestion | None: ...
async def approve(
    session: AsyncSession, *, account_id: int, memory_id: int, now: datetime
) -> PortResult: ...
async def edit(
    session: AsyncSession, *, account_id: int, memory_id: int, text: str, now: datetime
) -> PortResult: ...
async def discard(
    session: AsyncSession, *, account_id: int, memory_id: int, now: datetime
) -> PortResult: ...
```

`PortResult` is `core.p3_ports.PortResult` (one result vocabulary for every P3 owner action; `memory` may import `core`).

Use the constants of `memory/models.py` (`SOURCE_AGENT_INFERRED`, `STATUS_PENDING_REVIEW`, `STATUS_ACTIVE`, `STATUS_ARCHIVED`) wherever the text below names a source or status, never string literals.

- **`get_suggestion`**: `SELECT memory_items WHERE account_id = :a AND id = :m AND source = 'agent_inferred'` (no lock). `None` when nothing matches (unknown, another account's, or not inferred).
- **Locking read** (private `_lock`): the same `SELECT … FOR UPDATE`. Every mutating function starts with it; `None` → `PortResult.NOT_FOUND`.
- **`approve`**: status `pending_review` → `UPDATE memory_items SET status = 'active', updated_at = :now WHERE account_id = :a AND id = :m`; audit `EVENT_SUGGESTION_APPROVED` → `DONE`. Status `active` → `ALREADY`. Status `archived` → `INVALID`.
- **`edit`**: first `text = text.strip()`; empty or longer than `memory.items.MAX_CONTENT` (400) → `INVALID` (before any query). Then lock; status `pending_review` → `SET content = :text, status = 'active', updated_at = :now`; audit `EVENT_SUGGESTION_EDITED` → `DONE`. Any other status → `INVALID`. `scope`, `contact_id`, `conversation_id`, `category`, `source` and `provenance` stay as they are: the note stays scoped to its contact or chat (P3-interfaces §12.4) and keeps its managed provenance and refs, so per-chat and managed-data deletion still find it (the TL confirmed, P3-T162: an owner-edited suggestion keeps `source = 'agent_inferred'`, and the edited text does not go through the sensitive-category filter, because it is the owner's own words).
- **`discard`**: status `pending_review` → `DELETE FROM memory_items WHERE account_id = :a AND id = :m`; audit `EVENT_SUGGESTION_DISCARDED` → `DONE` (the words are gone at once; nothing to restore). Any other status → `INVALID` (an approved note is removed with the existing `forget`, not here).
- **Audit row** (private `_audit`): `AuditLogEntry(account_id=…, at=now, actor="owner", event=…, subject_type="memory_item", subject_id=memory_id, details={})`, added to the session and flushed. `details` is empty (P3-interfaces §13: "—"): never the text, the category or a name.

### 6.2 Card constants and texts (`src/assistant/telegram/control/memory_review.py`)

```python
"""Memory review cards (P3 M7-MRV; PRD §11.2, FR-MEM-05, OD-16).

The `memory_review` renderer shows one inferred suggestion with Approve / Edit /
Discard (a `mem` card, `ref_id` = the memory item). Presses and the `mem_edit`
text change the suggestion only through `memory/review.py`. Card-only: no tool.
Database work runs in the press or message transaction; every Telegram call
runs after the commit through the gateway. The suggestion's words and the
contact's name are escaped exactly once with `telegram.render.escape`."""

MEM_CARD: Final = "mem"
MEM_EDIT_INPUT: Final = "mem_edit"
OP_APPROVE: Final = "ok"
OP_EDIT: Final = "ed"
OP_DISCARD: Final = "rm"
REVIEW_CARD_TTL: Final = PENDING_REVIEW_RETENTION  # 14 days (OD-16); a test pins the equality
CATEGORY_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "work": "work",
        "project": "a project",
        "schedule": "schedule",
        "logistics": "logistics",
        "communication_preference": "how they like to communicate",
        "relationship_role": "who they are to you",
        "other_safe": "other",
    }
)
BUTTONS: Final = (("Approve", OP_APPROVE), ("Edit", OP_EDIT), ("Discard", OP_DISCARD))


def memory_id_from_refs(refs: Mapping[str, Any]) -> int | None: ...
async def review_text(session: AsyncSession, *, account_id: int, suggestion: Suggestion) -> str: ...
```

- **`memory_id_from_refs`**: `value = refs.get("memory_ids")`; not a `list`, or not exactly one element, or the element is not an `int` (a `bool` is not) or is below 1 → `None`; otherwise that id. (R75: M7-MEX stores the item id in `NotificationRefs.memory_ids`, which `inbox/notifications.py::_refs_json` writes as the jsonb key `memory_ids`. The renderer never parses the dedupe key `memreview:{local_date}:{memory_id}`: that key only dedupes.) Exactly one id keeps a card tied to one suggestion; anything else is suppressed (fail closed).
- **`review_text`** returns escaped text, exactly these three lines joined with `\n`:
  1. `💡 <b>Suggested note</b> about {name}:`
  2. `“{escape(suggestion.content)}”`
  3. `Category: {label}. Suggested by the assistant from your chat with {name}; it is not used anywhere until you approve it. Once approved, it is used only for that chat.`
  
  `name`: for `scope == "contact"` the contact `suggestion.contact_id`, for `scope == "conversation"` the contact of chat `suggestion.conversation_id`; both through one account-scoped query and `memory.summaries.preferred_name`; no row or no usable name → `a contact`; then `escape(...)`. `label` = `CATEGORY_LABELS.get(category or "", "other")`.

Outcome texts (module constants; `{name}` and `{note}` are escaped when put in):

| Constant | Text |
|---|---|
| `APPROVED_TEXT` | `✅ Note saved about {name}: “{note}”. It is used only for the chat it came from. You can ask me to forget it later.` |
| `ALREADY_SAVED_TEXT` | `This note was already saved; nothing changed.` |
| `DISCARDED_TEXT` | `🗑️ Suggestion discarded and deleted. It will not be used.` |
| `ALREADY_REVIEWED_TEXT` | `This suggestion was already reviewed; nothing changed.` |
| `GONE_TEXT` | `This suggestion no longer exists: it was discarded, expired after 14 days, was withdrawn because the message it came from was edited or deleted, or its chat's data was deleted. Nothing changed.` |
| `EDIT_WAITING` | `✏️ Send me the note as you want it saved, in one message (up to 400 characters). Until then it stays unreviewed.` |
| `EDIT_SAVED_TEXT` | `✅ Note saved about {name}: “{note}”. It is used only for the chat it came from.` |
| `EDIT_CARD_CLOSED_TEXT` | `✏️ Saved in your words: “{note}”.` |
| `EDIT_INVALID_TEXT` | `A note can be 1 to 400 characters; nothing was saved. Tap Edit on the suggestion to try again.` |
| `EDIT_REVIEWED_TEXT` | `This suggestion was already reviewed; nothing was saved.` |
| `EDIT_GONE_TEXT` | `This suggestion no longer exists (discarded, expired, withdrawn or deleted with its chat); nothing was saved.` |

### 6.3 The renderer (`MemoryReviewRenderer`)

```python
class MemoryReviewRenderer:
    def __init__(self, clock: Clock) -> None: ...
    async def __call__(
        self, session: AsyncSession, notification: Notification, account: Account
    ) -> RenderedAlert | None: ...
```

It runs in the delivery's claim transaction (P3-interfaces §11.3), never calls Telegram, reads content at delivery:
1. `memory_id = memory_id_from_refs(notification.refs)`; `None` → return `None` (suppressed).
2. `suggestion = await review.get_suggestion(session, account_id=account.id, memory_id=memory_id)`; `None`, or `suggestion.status != "pending_review"` → return `None` ("deleted source → suppressed"; an already reviewed suggestion needs no card).
3. `text = await review_text(...)`.
4. `card = await create_card(session, account_id=account.id, kind=MEM_CARD, clock=self._clock, ref_id=memory_id, ttl=REVIEW_CARD_TTL)`; `card.token is None` → `raise RuntimeError("card without a token")`.
5. Return `RenderedAlert(text, conversation_id=None, extra_buttons=tuple(Button(label, encode(MEM_CARD, card.token, op)) for label, op in BUTTONS))`.

`conversation_id=None` on purpose: with a chat id, delivery would add Open chat / Useful / Not useful, and alert feedback must never mute a contact because of a memory card (the M7-REM precedent; and F0-S's `feedback_buttons` is false for `memory_review` (R82), so even a renderer that returned a chat id would get Open chat only, never Useful or Not useful). The delivery class (held_in_quiet, high) and the "≤ 5 cards per owner-local day" cap are M7-MEX's and F0-S's; the renderer neither gates nor counts. The renderer needs no consent check: it only shows the owner text the application already holds and calls no model (invariant 9 concerns AI providers).

### 6.4 Card presses (`handle_mem_press`)

```python
async def handle_mem_press(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    scheduler: JobScheduler | None,
    account: Account,
    press: CardPress,
) -> HandlerResult: ...
```

The `CallbackHandler` shape (F0-R). `target = ControlChat(account_id=account.id, chat_id=press.chat_id)`; `now = clock.now()`. All edits go through a private `_after(session, gateway, press, target, *, answer: str, text: str | None = None, keyboard: InlineKeyboardMarkup | None = None)`: it registers `db.uow.after_commit(session, run)` where `run` is `async def run(factory: async_sessionmaker[AsyncSession]) -> None` (the hook passes the session factory; copy `telegram/control/monitors.py`'s helper of the same job), which does, in order, `gateway.answer_callback(press.callback_id, answer)`, then (when `text` is given) `gateway.edit_control_text(target, press.message_id, text, keyboard=keyboard)`, or `gateway.send_text(target, text, keyboard=keyboard)` when `press.message_id is None`. A second private helper, `_drop_edit_question(session, *, account_id, memory_id)`, deletes this account's text question of kind `MEM_EDIT_INPUT` with `ref_id = memory_id` and `token IS NULL` (no Telegram call). `ok` and `rm` call it (steps 3 and 4), so after the owner taps Approve or Discard the next plain message is not swallowed as an edit text.
1. `card = await claim_card(session, account_id=account.id, token=press.token, clock=clock)`; `None`, `card.kind != MEM_CARD`, `card.ref_id is None` or `press.op not in (OP_APPROVE, OP_EDIT, OP_DISCARD)` → `_after(answer=CALLBACK_EXPIRED_TEXT)`; `HandlerResult(False, "mem", None, note="no_longer_active")`.
2. `before = await review.get_suggestion(session, account_id=account.id, memory_id=card.ref_id)` (for the name and words in the answer, read before the change).
3. **`ok`** → `result = await review.approve(...)`, then `_drop_edit_question(...)` whatever the result:

   | Result | Answer | New card text (no buttons) | Note |
   |---|---|---|---|
   | `DONE` | `Saved` | `APPROVED_TEXT` with `before`'s name and words | `approved` |
   | `ALREADY` | `Already saved` | `ALREADY_SAVED_TEXT` | `already` |
   | `INVALID` | `Already reviewed` | `ALREADY_REVIEWED_TEXT` | `invalid` |
   | `NOT_FOUND` | `No longer exists` | `GONE_TEXT` | `not_found` |
4. **`rm`** → `result = await review.discard(...)`, then `_drop_edit_question(...)` whatever the result: `DONE` → `Discarded` / `DISCARDED_TEXT` / note `discarded`; `INVALID` → `Already reviewed` / `ALREADY_REVIEWED_TEXT`; `NOT_FOUND` → `No longer exists` / `GONE_TEXT`.
5. **`ed`**:
   - `before is None` → `No longer exists` / `GONE_TEXT`, note `not_found`; `before.status != "pending_review"` → `Already reviewed` / `ALREADY_REVIEWED_TEXT`, note `invalid`.
   - Otherwise: `await set_pending_input(session, account_id=account.id, kind=MEM_EDIT_INPUT, clock=clock, ref_id=card.ref_id)`; a **fresh** card for the same suggestion so Approve and Discard stay usable: `fresh = await create_card(session, account_id=account.id, kind=MEM_CARD, clock=clock, ref_id=card.ref_id, prompt_message_id=press.message_id, ttl=REVIEW_CARD_TTL)`; `_after(answer="Send the new wording", text=await review_text(...) + "\n\n" + EDIT_WAITING, keyboard=card_keyboard(MEM_CARD, fresh.token, list(BUTTONS)))`; note `asked`. (`fresh.token is None` → `RuntimeError`.)
6. Return `HandlerResult(True, "mem", None, note=…)`.

### 6.5 The edit text (`handle_mem_edit_input`)

```python
async def handle_mem_edit_input(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    scheduler: JobScheduler | None,
    account: Account,
    message: IncomingMessage,
    pending: PendingInput,
) -> HandlerResult: ...
```

The `PendingInputHandler` shape. The dispatcher calls it only for the owner's own plain message (never a command, a stop word, a forwarded or a quoted message). `target = ControlChat(account_id=account.id, chat_id=message.chat_id)`; replies go through a private `_reply(session, gateway, target, text)` that sends after the commit.
1. `await clear_pending_input(session, account_id=account.id)` (the question is answered whatever happens next).
2. `memory_id = pending.ref_id`; `None` → `_reply(EDIT_GONE_TEXT)`, `HandlerResult(False, "mem_edit", None, note="not_found")`.
3. `text = message.text.strip()`; empty or longer than `MAX_CONTENT` → `_reply(EDIT_INVALID_TEXT)`, note `invalid` (`review.edit` would refuse it too; this gives the reason).
4. `before = await review.get_suggestion(...)`; `result = await review.edit(session, account_id=account.id, memory_id=memory_id, text=text, now=clock.now())`:
   - `DONE` → `_reply(EDIT_SAVED_TEXT.format(name=…, note=escape(text)))` (`name` from `before`; `before is None` cannot happen after `DONE`, but use `a contact` if it does); then **close this suggestion's open review cards**: `DELETE FROM pending_inputs WHERE account_id = :a AND kind = 'mem' AND ref_id = :m AND token IS NOT NULL RETURNING prompt_message_id`, and after the commit edit each returned non-null message to `EDIT_CARD_CLOSED_TEXT.format(note=escape(text))` with no keyboard (`gateway.edit_control_text`). Note `edited`.
   - `INVALID` → `_reply(EDIT_REVIEWED_TEXT)`, note `invalid`.
   - `NOT_FOUND` → `_reply(EDIT_GONE_TEXT)`, note `not_found`.
5. Return `HandlerResult(True, "mem_edit", None, note=…)`. Never log the text.

### 6.6 Wiring (`_wire_surfaces` in `src/assistant/bootstrap.py`)

F0-R leaves `_wire_surfaces` returning a bare `P3Wiring()`. Several David units add blocks to it (M6-CATCH, STAT-ACT, SET-P3, OPS-RATE, COMMIT-DUE, OPS-AV, and you), so every unit uses **one convention** (the one in STAT-ACT's and SET-P3's specs): a local `wiring` value that each block extends with `dataclasses.replace`, returned at the end. Look at the function on your base first:

- It still ends with `return P3Wiring()`: change that to `wiring = P3Wiring()` at the top of the body (after the docstring), put your block after it, and end the function with `return wiring`.
- Another David unit already did that: add only your block just before `return wiring`.
- It is built any other way: keep every keyword and statement already there, merge your entries into it in the same style, and ask (§10) if unsure.

Use `replace` as the module already imports it; if it does not, add `from dataclasses import replace` as a function-local import. Never touch another unit's block.

```python
def _wire_surfaces(ctx: P3WireContext) -> P3Wiring:
    """David's units. Function-local imports only; one block per unit."""
    wiring = P3Wiring()

    # ... other David units' blocks ...

    # --- M7-MRV (D2): memory review cards
    from assistant.inbox.notification_types import NotificationKind
    from assistant.telegram.control.memory_review import (
        MEM_CARD,
        MEM_EDIT_INPUT,
        MemoryReviewRenderer,
        handle_mem_edit_input,
        handle_mem_press,
    )

    ctx.notification_delivery.register_renderer(
        NotificationKind.MEMORY_REVIEW, MemoryReviewRenderer(clock=ctx.clock)
    )
    routes = wiring.routes
    wiring = replace(
        wiring,
        routes=replace(
            routes,
            callbacks={**routes.callbacks, MEM_CARD: handle_mem_press},
            pending_inputs={**routes.pending_inputs, MEM_EDIT_INPUT: handle_mem_edit_input},
        ),
    )

    return wiring
```

The renderer and the routes are wired whatever the flags: with `memory.extract` off, M7-MEX enqueues no cards, and a card already queued still renders (the owner can still decide). The renderer and its routes go in together, never one without the other: a card whose press has no handler would be a button that does nothing. F0-R's `test_empty_wire_functions_change_nothing` checks a process with all P3 flags off against the append-only `ALWAYS_WIRED` expected set (F0-R §5.4, P3-T157): append the `memory_review` renderer, the `mem` callback route and the `mem_edit` pending-input kind to that set and change nothing else in the file (WORKFLOW §6.5, append only), in a separate last commit named in the PR. If any other exact-set test fails, stop and ask (§10).

### 6.7 ARCHITECTURE §6.7 bullet (exact content; your mentor checks it)

As the last bullet of §6.7 (just before the §6.8 heading): **Memory review cards (as built, M7-MRV).** An inferred suggestion (`memory_items`, `source = agent_inferred`, `status = pending_review`) reaches the owner as a `memory_review` notification (held in quiet hours, high; at most 5 per owner-local day, enqueued by M7-MEX with the item id in `refs.memory_ids`). Its renderer reads the item at delivery (gone, already reviewed or without exactly one item id → suppressed) and adds one `mem` card (14 days) with Approve, Edit and Discard; no Open chat or feedback buttons. Approve sets the item active (used only through `contact_facing`, for its own chat); Edit opens the `mem_edit` question and saves the owner's words, keeping the item's scope, category and provenance; Discard deletes the row; Approve and Discard also close an open `mem_edit` question. One content-free audit row per change (`memory_suggestion_approved` / `_edited` / `_discarded`). Card-only: no agent tool reaches `memory/review.py`.

### 6.8 Steps (each ends with a check)

All commands run in your directory with `$env:COMPOSE_PROJECT_NAME = 'd2'` set (they run inside the container). If your session cannot run Docker, write the tests, push, let CI run them, and say so in the PR.

1. **Branch** from `origin/int/p3`. **Check:** `docker compose -f compose.dev.yml run --rm app uv run python -c "from assistant.memory.models import STATUS_PENDING_REVIEW, SOURCE_AGENT_INFERRED, MEMORY_CATEGORIES; from assistant.inbox.notification_types import NotificationKind, NotificationRefs; from assistant.privacy.retention import PENDING_REVIEW_RETENTION; from assistant.core.p3_ports import PortResult; print(STATUS_PENDING_REVIEW, SOURCE_AGENT_INFERRED, NotificationKind.MEMORY_REVIEW.value, len(MEMORY_CATEGORIES), NotificationRefs(memory_ids=(1,)).memory_ids, PENDING_REVIEW_RETENTION.days)"` prints `pending_review agent_inferred memory_review 7 (1,) 14`. Anything else (including a `TypeError` about `memory_ids`): F0-S is not on your base; stop and ask (§10).
2. **Red tests.** Write §7.1–§7.2; create both modules with docstrings, constants and texts, and every function raising `NotImplementedError`. **Check:** `docker compose -f compose.dev.yml run --rm app uv run pytest tests/unit/test_memory_review_card_only.py tests/integration/test_memory_review_cards.py -q` → failures are assertion errors or `NotImplementedError`, none `ImportError` (the card-only test passes already; that is expected).
3. **`memory/review.py`** (§6.1). **Check:** `… uv run pytest tests/integration/test_memory_review_cards.py -q -k "domain or rg21_review or replace or forget"` → pass (a red `forget` test: see §10).
4. **Renderer** (§6.2–§6.3). **Check:** `… -k "renderer or conversation_scoped or delivered or escapes"` → pass.
5. **Presses and the edit text** (§6.4–§6.5). **Check:** the whole of `tests/integration/test_memory_review_cards.py` passes.
6. **Wiring** (§6.6) and the hygiene test (§7.3). **Check:** `… uv run pytest tests/integration/test_log_hygiene_m7_mrv.py -q` → pass, then `… uv run pytest tests/integration/test_memory_review_cards.py tests/integration/test_p3_wiring.py -q` → pass (F0-R's duplicate-key and frozen-import tests still hold with your kinds; `test_wire_surfaces_registers_the_review_renderer_and_routes` is in your own file).
7. **Docs** (§6.7). **Check:** `git diff origin/int/p3 -- docs/ARCHITECTURE.md` shows one added bullet at the end of §6.7 and nothing else.
8. **Static checks.** `… uv run ruff check src/assistant/memory/review.py src/assistant/telegram/control/memory_review.py src/assistant/bootstrap.py tests/unit/test_memory_review_card_only.py tests/integration/test_memory_review_cards.py tests/integration/test_log_hygiene_m7_mrv.py`; the same with `uv run ruff format --check`; `… uv run mypy src/assistant/memory/review.py src/assistant/telegram/control/memory_review.py src/assistant/bootstrap.py`; `… uv run lint-imports` (`memory` must not import `telegram`, `agent` or `aiogram`). **Check:** all clean.
9. **Diff.** `git diff --stat origin/int/p3...HEAD` → only §3 files. Commit (red tests, then green code), push, open the draft PR (§12).

## 7. Named tests (red first)

Synthetic data only; real PostgreSQL 18; the `business_env` clock (`NOW` = Sun 2026-10-18 12:00 UTC); no sleeps. Setup:

- **Env:** `environment(migrated_url, owner_ids=frozenset({OWNER, OTHER_OWNER}), routes=ControlRoutes(callbacks={MEM_CARD: handle_mem_press}, pending_inputs={MEM_EDIT_INPUT: handle_mem_edit_input}))`, `OTHER_OWNER = 7000000002`; `await connect(env)`; Kamola's chat from `deliver(env, business_message(message_id=1, first_name="Kamola"))`; Bobur's from `business_message(from_id=7000000009, chat_id=7000000009, message_id=2, first_name="Bobur")`.
- **A suggestion:** `p3_factories.seed_memory_item(session, account_id=…, scope="contact", contact_id=<Kamola>, kind="preference", content="SYNTH prefers voice messages after 18:00", source="agent_inferred", status="pending_review", category="communication_preference", provenance={"tainted": True, "managed": True, "refs": {"conversation_ids": [<Kamola's chat>], "contact_ids": [<Kamola>]}, "source_message_ids": [<message id>]})`.
- **Its notification:** `p2_factories.seed_notification(session, account_id=…, kind="memory_review", priority="high", dedupe_key=f"memreview:20261018:{memory_id}", refs={"memory_ids": [memory_id], "conversation_ids": [<chat>], "contact_ids": [<Kamola>], "message_ids": [<message id>]})`, with `contact_id` left NULL and `conversation_id` NULL unless a test says otherwise (the shape M7-MEX enqueues; `memory_ids` is R75's key: `NotificationRefs.memory_ids`, written to the jsonb key `memory_ids` by `inbox/notifications.py::_refs_json`).
- **The card:** call the renderer in a transaction (`await MemoryReviewRenderer(env.clock)(session, notification, account)`), commit, then press with `deliver(env, callback(f"mem:{token}:{op}", message_id=…))` (the token from the returned buttons' callback data, or `test_conversation_modes.card(env, "mem")`; if that import fails on your base, copy the six-line helper into your test file and say so in the PR).
- **Hostile text** (GW-ESC §3.3 rule 6), a module constant of the test file: `HOSTILE = 'A&B <b>x</b> <a href="https://example.invalid/">y</a> &amp;'` and `HOSTILE_ESCAPED = 'A&amp;B &lt;b&gt;x&lt;/b&gt; &lt;a href="https://example.invalid/"&gt;y&lt;/a&gt; &amp;amp;'` (`telegram.render.escape` leaves the double quotes).

### 7.1 `tests/unit/test_memory_review_card_only.py`

- `test_no_agent_module_imports_the_review_functions` — parse every `src/assistant/agent/**/*.py` with `ast`; no `Import` or `ImportFrom` names `assistant.memory.review`, and no `from assistant.memory import review` (card-only, P3-interfaces §12.5).
- `test_no_tool_handler_lives_in_the_review_modules` — for every tool in `default_registry().all()`: `tool.handler.__module__` is neither `assistant.memory.review` nor `assistant.telegram.control.memory_review`.
- `test_memory_id_from_refs` — `{"memory_ids": [42]}` → `42`; each of these → `None`: `{}`, `{"memory_ids": []}`, `{"memory_ids": [1, 2]}`, `{"memory_ids": [True]}`, `{"memory_ids": [0]}`, `{"memory_ids": [-3]}`, `{"memory_ids": ["42"]}`, `{"memory_ids": 42}`, `{"memory_ids": None}`, `{"conversation_ids": [42]}` (R75: only the one `memory_ids` key counts).
- `test_the_renderer_module_never_parses_the_dedupe_key` — `ast`-parse `src/assistant/telegram/control/memory_review.py`: no attribute access `.dedupe_key` and no `re` import (the item id comes from `refs.memory_ids` only).
- `test_category_labels_cover_every_allowed_category` — `set(CATEGORY_LABELS) == set(MEMORY_CATEGORIES)`.
- `test_the_card_and_input_kinds_are_the_contract_kinds` — `(MEM_CARD, MEM_EDIT_INPUT, OP_APPROVE, OP_EDIT, OP_DISCARD) == ("mem", "mem_edit", "ok", "ed", "rm")`; `accounts.callbacks.encode(MEM_CARD, "A" * 10, op)` does not raise for each op.
- `test_the_review_card_lives_as_long_as_the_suggestion` — `REVIEW_CARD_TTL == PENDING_REVIEW_RETENTION == timedelta(days=14)` (OD-16: a changed retention must change the card's life with it).

### 7.2 `tests/integration/test_memory_review_cards.py`

**Domain**
- `test_domain_approve_edit_and_discard_follow_the_status` — fresh pending suggestions each: `approve` → `DONE`, the row `status == "active"`, content unchanged, one `memory_suggestion_approved` audit row with `actor = "owner"`, `subject_type = "memory_item"`, `subject_id` = the id and `details == {}`; a second `approve` → `ALREADY`; `edit(text="  SYNTH owner words  ")` → `DONE`, `content == "SYNTH owner words"`, `status == "active"`, `scope`, `contact_id`, `category`, `source` and `provenance` unchanged, one `memory_suggestion_edited` row; `discard` → `DONE`, the row is gone, one `memory_suggestion_discarded` row; `discard` of an `active` item → `INVALID` and the item stays; `approve` of an `archived` item → `INVALID`.
- `test_domain_edit_refuses_empty_or_long_text` — `""`, `"   "` and `"x" * 401` → `INVALID`; the row unchanged; no audit row.
- `test_domain_ignores_items_that_are_not_inferred` — an `owner_stated` active item: `get_suggestion` → `None`; `approve`, `edit` and `discard` → `NOT_FOUND`; the item unchanged.
- `test_rg21_review_functions_refuse_another_accounts_suggestion` — OTHER_OWNER's pending suggestion: called with OWNER's account id, `get_suggestion` → `None` and each function → `NOT_FOUND`; the row unchanged.

**Renderer and delivery**
- `test_the_renderer_shows_the_suggestion_with_three_buttons` — the returned `RenderedAlert` has `conversation_id is None`, text lines exactly `💡 <b>Suggested note</b> about Kamola:`, `“SYNTH prefers voice messages after 18:00”` and `Category: how they like to communicate. Suggested by the assistant from your chat with Kamola; it is not used anywhere until you approve it. Once approved, it is used only for that chat.`; `extra_buttons` labels `["Approve", "Edit", "Discard"]` with ops `["ok", "ed", "rm"]` sharing one token; one `mem` card exists with `ref_id` = the memory id and `expires_at == NOW + 14 days`.
- `test_a_conversation_scoped_suggestion_names_the_chats_contact` — `scope="conversation"`, `conversation_id=<Bobur's chat>`: the first line says `about Bobur:`.
- `test_the_renderer_suppresses_a_gone_or_reviewed_suggestion` — each returns `None` and creates no card: the item deleted; the item `active`; the item `archived`; refs without `memory_ids`; refs with two ids; refs naming OTHER_OWNER's suggestion (rendered for OWNER); an `owner_stated` active item named by the refs.
- `test_the_renderer_reads_the_refs_not_the_dedupe_key` — a notification whose dedupe key is `memreview:x:99999999` but whose refs name the real pending item renders the card; one whose dedupe key names the real item but whose refs name none returns `None`.
- `test_a_delivered_review_card_has_only_its_own_buttons` — parametrized over the notification's `conversation_id` column (NULL; Kamola's chat id — M7-MEX may set it, the renderer must not care): `channel = FakeChannel()`; `NotificationDelivery(session_factory=env.factory, channel=channel, clock=env.clock, scheduler=env.scheduler, enabled=True)` with the renderer registered through `register_renderer`; `deliver(<notification id>)` → status `sent`; `channel.buttons` has one entry whose labels are exactly `["Approve", "Edit", "Discard"]` (no Open chat, Useful or Not useful; R82). (`NOW` is 12:00 in the owner's timezone, outside quiet hours, so the card is not held. If your base holds it anyway, do not change the clock; stop and ask, §10.)
- `test_review_card_escapes_untrusted_text_once` (GW-ESC §3.3 rule 6) — the suggestion's content and Kamola's `display_name` both set to `HOSTILE`: with `text = telegram.render.sanitize(<the rendered text>)`, `text.count(HOSTILE_ESCAPED) == 3` (the name twice, the content once), `"<b>x</b>" not in text`, `"<a " not in text`, and the application markup `"<b>Suggested note</b>"` is still in `text`.

**Presses and the edit text**
- `test_review_card_approve_edit_discard` (IMPLEMENTATION_PLAN S07.4, PRD §11.2) — three suggestions, three cards: `ok` → answer `Saved`, the card edited to `APPROVED_TEXT` for Kamola with no keyboard, the item `active`; `rm` → answer `Discarded`, `DISCARDED_TEXT`, the item gone; `ed` → answer `Send the new wording`, a pending input `kind = "mem_edit"` with `ref_id` = the item, the card edited to the review text plus `EDIT_WAITING` with the three buttons on a new token; the owner sends `"SYNTH my own words"` → the reply is `EDIT_SAVED_TEXT` with those words, the item `active` with `content == "SYNTH my own words"`, no `mem` card is left for it, no `mem_edit` pending input remains, and the review message is edited to `EDIT_CARD_CLOSED_TEXT` with those words and no keyboard.
- `test_press_outcomes_are_reported_truthfully` — `ok` on an item that is already `active` (made active in the test before the press) → `Already saved` / `ALREADY_SAVED_TEXT`; `rm` on an `active` item → `Already reviewed` / `ALREADY_REVIEWED_TEXT`; `ok` after the item was deleted → `No longer exists` / `GONE_TEXT`; `ed` on a deleted item → `GONE_TEXT` and no pending input.
- `test_a_second_tap_on_the_same_card_changes_nothing` — `ok` twice on one card: one audit row; the second answer is `CALLBACK_EXPIRED_TEXT`.
- `test_after_edit_approve_and_discard_still_work_from_the_rearmed_card` — `ed`, then `ok` on the re-armed card (no text sent): the item is `active` (`APPROVED_TEXT`); a second item, `ed` then `rm` on its re-armed card: the item is deleted (`DISCARDED_TEXT`).
- `test_approve_and_discard_close_an_open_edit_question` — after each of the two sequences above no `mem_edit` pending input remains, so the owner's next plain message is not taken as a note (the dispatcher no longer routes it to `handle_mem_edit_input`: no `EDIT_*` reply is sent and no row changes).
- `test_edit_text_for_a_suggestion_deleted_meanwhile_is_not_saved` — `ed`, then the item is deleted directly (as the 14-day sweep would), then the owner sends text: `EDIT_GONE_TEXT`, no `memory_items` row exists, no audit row, no pending input remains.
- `test_edit_text_that_cannot_be_saved_is_refused` — `"x" * 401` → `EDIT_INVALID_TEXT`; the item stays `pending_review`; no pending input remains.
- `test_a_forwarded_message_is_never_taken_as_the_edited_note` — after `ed`, the owner forwards a message (a `forward_origin` hidden user): the item stays `pending_review` with its original content.
- `test_a_command_cancels_the_edit_question` — after `ed`, the owner sends `/status`: no pending input remains; the item stays `pending_review`.
- `test_rg21_a_review_card_of_another_account_is_no_longer_active` — OTHER_OWNER presses OWNER's card (`ok`, `ed`, `rm`): `CALLBACK_EXPIRED_TEXT` each time; OWNER's item unchanged; no pending input for OTHER_OWNER.

**What an approved note reaches (invariant 4)**
- `test_approved_inferred_item_reaches_only_its_contacts_drafts` (FR-MEM-06; ARCHITECTURE §7.3) — before approval, `memory.items.contact_facing(session, account_id, <Kamola's chat>, include_owner_preferences=True)` (the exact call `agent/drafting.py` makes) does not contain the item; after `ok`, it contains it for Kamola's chat and does **not** for Bobur's chat.
- `test_an_approved_suggestion_is_never_shareable_knowledge` — after `ok`, with `p3_factories.seed_delegation` covering Kamola's chat, `memory.shareable.shareable_for(session, account_id=…, delegation=<its view from delegations_repo.get>, conversation_id=<Kamola's chat>)` does not contain the item (it is not `kind = 'shareable'`, so no autonomous reply can pass it on).
- `test_forget_and_its_undo_cannot_activate_a_pending_suggestion` (invariant 3b: only the owner's taps on the review card approve) — on a pending suggestion, `memory.items.forget(session, account_id=…, memory_id=<pending id>, actor="agent", now=NOW)` either raises `NotInAccountError` or leaves `status == "pending_review"`; then `memory.items.set_status(session, account_id=…, memory_id=<pending id>, active=True, now=NOW)` returns `False`; the row is never `active` afterwards. (If this is red on your base, `memory/items.py` — M6-DELEG's file — lacks a `pending_review` guard in `forget`: do not edit it; stop and ask, §10. The TL rules the guard as an interface change.)
- `test_remember_cannot_replace_a_pending_suggestion` — `memory.items.save(session, account_id=…, content="SYNTH x", kind="fact", replaces_id=<pending id>, actor="owner", now=NOW)` raises `NotInAccountError`; the suggestion stays `pending_review`.

**Wiring**
- `test_wire_surfaces_registers_the_review_renderer_and_routes` — build the `P3WireContext` with the helper F0-R's `tests/integration/test_p3_wiring.py` uses (import it; if it has none you can import, stop and ask, §10), with `notification_delivery` replaced (`dataclasses.replace`) by a small spy class recording `register_renderer(kind, renderer)` calls; `wiring = _wire_surfaces(ctx)`: the spy recorded `NotificationKind.MEMORY_REVIEW` with a `MemoryReviewRenderer`, `wiring.routes.callbacks["mem"] is handle_mem_press`, `wiring.routes.pending_inputs["mem_edit"] is handle_mem_edit_input`, and the same holds with every feature flag off (wired whatever the flags). If another David unit's block is already in the function on your base, the test asserts only your own entries.

### 7.3 `tests/integration/test_log_hygiene_m7_mrv.py`

- `test_rg10_review_cards_log_no_note_text_or_names` — inside `capture_logs()`, with `CANARY` in the suggestion's content, in Kamola's display name and in the owner's edit text: render, press `ok` on one card, `rm` on another, `ed` on a third and send the edit text; `assert_content_free(output, CANARY)`; and no `audit_log.details` row contains `CANARY`.

You may add tests; never fewer, never with changed expected values.

## 8. Truthfulness edge cases

- **Saved only when saved.** "Note saved" appears only for `DONE`; `ALREADY`, `INVALID` and `NOT_FOUND` each have their own sentence.
- **Discard means deleted.** The text says the suggestion was deleted, and it is (the row is gone; nothing can restore it).
- **Unreviewed means unused.** The card says the suggestion is not used anywhere until approved; that is true because prompt loaders read only `status = 'active'` (M7-MEX's "never in prompts" test). The renderer suppresses a card whose suggestion is gone or already reviewed instead of asking twice.
- **Approved means one chat.** The card and the saved text say the note is used only for the chat it came from; `contact_facing` excludes it from every other chat (its refs name that chat), and it is never shareable.
- **The owner's words are the owner's.** Only the owner's own plain message answers `mem_edit`; a forwarded or quoted message never does; a command or a stop word cancels the question.
- **Expired is said.** A press after the 14-day sweep, a deletion or a discard says the suggestion no longer exists and why it might be gone.
- **No text leaves the modules** in logs, audit details or exception messages.

## 9. Acceptance checklist

- [ ] Every test in §7 exists, was seen failing, and passes (the card-only test passes from the start: that is expected).
- [ ] `git diff --stat origin/int/p3...HEAD` lists only the §3 files.
- [ ] The `bootstrap.py` diff touches only the body of `_wire_surfaces`; no top-level import is added (F0-R's frozen-import test passes).
- [ ] `memory/review.py` imports no `telegram`, `agent`, `aiogram`, `jobs` or `llm` module; `lint-imports` clean.
- [ ] `ruff check`, `ruff format --check` and `mypy` (strict) clean on the changed files.
- [ ] No `assert`, no broad `except`, no logging of note text or names; every Telegram call is a gateway call made after the commit.
- [ ] The PR description names the contract gaps relied on (the memory id read from `refs.memory_ids`, R75; edit keeps `source = agent_inferred`; `forget` of a pending item) and tells E2 the exact key format and refs M7-MEX must use (§12).

## 10. When to stop and ask

Ask on the M7-MRV issue, starting the comment with "Question for E2:" (plain text, never an @-mention). If nobody answers within 2 h, David raises it with the TL.

- Step 1's check fails, or `p3_factories.seed_memory_item` cannot write a `pending_review` / `agent_inferred` row.
- M7-MEX (once on your base) enqueues `memory_review` without `memory_ids` in `refs` (for example it still relies on the dedupe key's last segment, as its first draft did), with a dedupe key other than `memreview:{YYYYMMDD}:{memory_id}`, or sets the notification's `contact_id` column (a low-priority contact would then demote the card; feedback buttons are off for this kind, R82, so no mark could apply to it). A set `conversation_id` column is fine: the renderer ignores it.
- `test_forget_and_its_undo_cannot_activate_a_pending_suggestion` is red on your base (see its text).
- (Settled by P3-T162: an edited suggestion keeps `source = 'agent_inferred'` and its text is not passed through the sensitive-category filter; §6.1 stands as written.)
- `_wire_surfaces` has a shape you cannot add your block to without changing another unit's lines.
- A named test cannot pass without editing a file outside §3.
- You think a tool, a prompt or any `agent/` file needs to change: it does not (card-only); ask.
- You have spent more than half an engineer-day and are not close.

## 11. Coding rules (reviewers block on these)

- No broad `except`; no `assert` in production code (`if card.token is None: raise RuntimeError(...)`).
- Full type hints; `mypy --strict` clean; names exactly as §6.
- Time from the `clock` / `now` you are given; UTC only.
- Every query has `account_id = :account_id`; the item's `source` is checked too (`agent_inferred`).
- Content-free logs and audit details: ids and fixed codes only.
- No Telegram call except the gateway's `answer_callback`, `edit_control_text` and `send_text`, after the commit; no aiogram shortcuts.
- Escape the suggestion's words and the contact's name exactly once with `telegram.render.escape`; never twice; application markup (`<b>`) raw.
- Synthetic test data only (invented names "Kamola", "Bobur", `SYNTH` texts).
- GitHub: every body through `--body-file <file>`; no bare `@names`; no recurring status comments. Commits: Conventional Commits, `git commit -F <file>`; never `--no-verify`.

## 12. Pull request

- **Title:** `M7-MRV: memory review cards (approve, edit, discard)`.
- **Size:** about 400 changed lines or fewer (expected ≈ 220 code + ≈ 330 tests).
- **Labels:** `team:david`, `eng:d2` on the draft; when CI is green, mark it ready and add `needs-review:e2` and `needs-qa`. QA is **after merge**: the TL merges on mentor and TL review; QD1 checks on `int/p3` before R1, and a finding becomes one fix PR. If a strict-xfail QA-S7 scenario about review cards passes with your merge, tell QE1 on the unit's issue.
- **Template** (WORKFLOW §4.3):
  - *Unit:* `WU: M7-MRV · Spec: docs/engineering/p3/specs/M7-MRV.md · Issue: #<n> · Base: int/p3`; Tier: other; QA lane: after merge.
  - *Safety invariants touched:* 3b (card-only: `test_no_agent_module_imports_the_review_functions`, `test_no_tool_handler_lives_in_the_review_modules`, `test_a_forwarded_message_is_never_taken_as_the_edited_note`, `test_forget_and_its_undo_cannot_activate_a_pending_suggestion`); 4 (`test_approved_inferred_item_reaches_only_its_contacts_drafts`, `test_an_approved_suggestion_is_never_shareable_knowledge`); 11 (`test_press_outcomes_are_reported_truthfully`); 12 (`test_rg21_review_functions_refuse_another_accounts_suggestion`, `test_rg21_a_review_card_of_another_account_is_no_longer_active`).
  - *Tests:* the §7 names; commands and results, or "not run locally: <reason>; CI is the gate"; RG-10 and RG-21 extended.
  - *Shared lists updated:* none.
  - *Data:* no migration, no content column (`memory_items.content` is already registered; discard deletes the row); logs: `test_log_hygiene_m7_mrv.py`.
  - *Prompts, flags and settings:* none (cards arrive only while M7-MEX's `memory.extract` is on).
  - *Docs:* ARCHITECTURE §6.7.
  - *Interface changes:* none. For E2 (M7-MEX): enqueue each card with dedupe key `memreview:{YYYYMMDD}:{memory_id}`, `NotificationRefs(memory_ids=(memory_id,), conversation_ids=…, contact_ids=…, message_ids=…)` (exactly one id in `memory_ids`: the renderer suppresses anything else), the `contact_id` column NULL (the `conversation_id` column may be set), priority `high`.
  - *Open items for M07.md:* anything non-blocking (for example card wording the owner may want changed).
