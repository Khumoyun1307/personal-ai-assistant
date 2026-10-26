# OPS-LV08 — LV-08 reopen half (and the LV-15 contact-edit add-on)

| | |
|---|---|
| **Owner** | Antony (probe change, runbook, evidence) + **the owner** (client steps, per-run go-ahead) · **Peer review** TL |
| **Size** | S (small probe change + two short live steps ≥ 24 h apart) |
| **Branch** | `ops/lv08-probe` from `main` → PR to `main` (probe change; data/docs-free of product code) |
| **Depends on** | The owner's go-ahead; an AGENTS §9 wording update agreed by the owner (see §3.2); not on any M04 unit |
| **Normative inputs** | TF §11 LV-08 and LV-15 protocol, TF §12 LV-08 (closed-window half, 2026-10-21), [M03-handoff.md §4](../../../milestones/M03-handoff.md) (moved to M04 by the owner, 2026-10-26), AGENTS §2 (only owner-executed tests mark TF items *Verified*), AGENTS §9 (Alviro token rules), `tools/probe/probe.py` |

## 1. Goal

Answer the open half of LV-08 on Alviro's own connection: **after a chat's 24-hour reply window has closed, does (a) a contact's edit of an old message or (b) a new message from the owner reopen it?** Until verified, the product keeps the conservative rule (only a new contact message reopens; `conversations/window.py` / `core/reply_window.py`). The result is recorded in TF §12; any product change is a separate owner decision.

## 2. Scope

**In:** a narrowly gated probe mode for this run; a content-free server timeline action; the run procedure; the TF §12 entry draft. **Out:** changing `window_anchor` or any product rule (follow-up, owner decision); `listen` with the production token (never); `edit`/`delete` attempts (the closed-window edit error is already recorded).

## 3. Design

### 3.1 No polling from the laptop, and the server keeps running (**TL decision (spec)**)

The attempts' outcomes are synchronous (`sendChatAction`/`sendMessage` answer at once), so the probe needs no `getUpdates` and no `listen`. The server's own ingestion records the timeline (the contact's edit and the owner's message arrive as business updates and are stored with Telegram's dates), which a content-free server action reads. So the production poller does **not** need to be stopped: a second process that never calls `getUpdates` cannot conflict with it (TF §3.1; AGENTS §9 "one poller per token"). The earlier note "with the server's poller briefly stopped" (M02/M03 docs) is unnecessary; stop it (`just prod <ip> app-stop` … `app-start`) only if the owner prefers.

### 3.2 Probe change (`tools/probe/probe.py`)

Today `--prod-token` refuses everything but `me` and `getfile` (`PROD_TOKEN_COMMANDS`). Add an **LV-08 mode**, `--prod-token --lv08 --armed-date YYYY-MM-DD <command>`, that allows exactly three commands — `connection` (`getBusinessConnection`, to confirm `is_enabled` and `can_reply`), `typing` (`sendChatAction typing`) and `send` — and only when **all** hold:
1. `--armed-date` equals today's date in UTC (the operator types it after the owner's go-ahead in the conversation; a stale command line refuses the next day).
2. The target chat is the **single** id in `PROBE_ALLOWED_CHAT_IDS` read from `var/prod.env` (set by the owner; parsed like `BOT_TOKEN`, never printed). Any other chat id, or zero or several ids, refuses.
3. `send` takes **no text argument**: the probe sends the fixed synthetic text `LV-08 reopen check <UTC HH:MM:SS>` (AGENTS §9 probe condition 3).
4. `listen`, `burst`, `edit`, `delete`, `dm`, `sendphoto` and any `getUpdates` call stay refused with the production token in every mode.
Console output stays content-free: command, UTC time, `ok` with the returned `message_id`, or the error class and Telegram's description code (e.g. `PEER_ID_INVALID`). Never the token, never the chat id.

**Rule change needed first:** AGENTS §9 currently says the `--prod-token` flag "refuses every command but `me` and `getfile`". The LV-08 mode contradicts that sentence, so the TL proposes the wording change to the owner (rule change process, AGENTS §2) and the probe PR merges only after the owner agrees. A send in this mode is an owner-voice message outside `actions.execute`; like the M00 LV runs it is owner-authorised throwaway tooling, into the owner's own second account's chat, with fixed synthetic text.

### 3.3 Server timeline action (`deploy/server.sh lv08-timeline`, `deploy/sql/lv08_timeline.sql`)

A psql script run exactly like `nfr04` (read-only transaction, `PGOPTIONS=-c default_transaction_read_only=on`), so this unit does not depend on OPS-TV (which lands on `int/m04`). Read-only and content-free: the account's connection (`status`, `is_enabled`, whether `can_reply` is in `rights`, `connection_id` — needed by the probe's `connection`/`send` commands; not secret); and, per conversation of the account, `conversations.id`, `last_contact_msg_at`, `last_owner_msg_at`, the newest contact `edited_at`, whether the product considers the window open now, and the count of contact messages in the last 72 h — no contact ids, names or text. The owner identifies the test chat by its times (production has few chats); the TL notes its internal id for the evidence.

### 3.4 Procedure

Chat: the owner's main account (Alviro's connection, Premium) with the second account as the contact. No Assist mode and no drafts in that chat during the test.

| Step | When | Who | Action | Record |
|---|---|---|---|---|
| P1 | Day 0 | Owner | Second account sends one message `LV-08 T0` in the chat; afterwards **neither account writes there** | T0 (Telegram date) from `lv08-timeline` |
| P2 | Day 0 | Owner | Set `PROBE_ALLOWED_CHAT_IDS=<second account id>` in `var/prod.env` (alone) | — |
| R1.1 | ≥ T0 + 24 h 15 min, owner present, go-ahead given | TL | `lv08-timeline`: window closed by the product rule, connection active, `can_reply` | timeline line |
| R1.2 | same | Antony/TL | `probe --prod-token --lv08 --armed-date <today> connection <conn id>` then `… typing <chat>` | expect `PEER_ID_INVALID` (closed baseline, matches 2026-10-21) |
| R1.3 | same | Owner | Second account **edits** `LV-08 T0` (no new message); note the client and version | edit time from `lv08-timeline` (`edited_at` set, `last_contact_msg_at` unchanged) |
| R1.4 | +1 min | Antony/TL | `typing`, then `send` | **Result A** (contact edit): `ok` = reopened; `PEER_ID_INVALID` = not reopened |
| R2.0 | if A = reopened | — | Wait until ≥ edit time + 24 h 15 min (the edit may have opened a new 24 h) and repeat R1.1–R1.2 | — |
| R2.1 | window closed | Owner | Main account sends `LV-08 owner check` in the chat; note the client and version | `last_owner_msg_at` |
| R2.2 | +1 min | Antony/TL | `typing`, then `send` | **Result B** (owner message): `ok` = reopened; `PEER_ID_INVALID` = not reopened |
| Clean-up | after | Owner | Remove `PROBE_ALLOWED_CHAT_IDS` from `var/prod.env` | — |

Abort if the timeline shows any new contact message in the chat after T0 (the test would be invalid); restart from P1.

### 3.5 Recording

- The same day, Antony drafts the TF §12 LV-08 row extension ("reopen half", plus the LV-15 add-on) with: date, UTC times, client versions, the connection's rights, the exact responses for each attempt, and a synthetic payload summary (no text beyond the fixed probe strings). The owner confirms it in the conversation; only then does the TL mark it *Verified* (AGENTS §2). Evidence uses [qa/evidence-template.md](../qa/evidence-template.md) and goes into `docs/milestones/M04.md`.
- **Follow-up, not this unit:** if A or B reopened the window, the TL takes the product question to the owner (should `window_anchor` count edits or owner messages? it would let drafts and sends go later than today). Until the owner decides, the conservative rule stays. If neither reopened, the "unverified" notes in `conversations/window.py`/`core/reply_window.py` and ARCHITECTURE §6.6 are updated to "verified" in a later Neryn PR (the TL files it).

## 4. Interfaces

Changes only `tools/probe/probe.py`, `deploy/sql/lv08_timeline.sql` and `deploy/server.sh` (+ its usage line). No dependency on other P2 units.

## 5. Tests (red first; `tests/unit/test_probe_prod_token.py`, extend)

`test_lv08_mode_allows_only_connection_typing_and_send`, `test_lv08_mode_requires_todays_armed_date`, `test_lv08_mode_refuses_any_chat_but_the_single_allow_listed_one`, `test_lv08_send_text_is_fixed_and_synthetic`, `test_the_production_token_never_listens_or_polls_in_any_mode`, `test_lv08_console_output_has_no_token_or_chat_id`; `tests/integration/test_lv08_timeline_query.py::test_the_lv08_timeline_query_is_content_free_and_read_only` (in the style of `test_nfr04_query.py`: a canary in every content column never appears in the output).

## 6. Acceptance criteria

1. The AGENTS §9 wording change is agreed by the owner before the probe PR merges.
2. Results A and B recorded with exact responses, dates and client versions; owner-confirmed TF §12 entry.
3. No `getUpdates` call was made with the production token; no text other than the fixed probe strings was sent.

## 7. Docs

TF §12 (owner-confirmed entry), TF §13 PQ-02 ("Still open" removed or updated), `tools/probe/README.md` (LV-08 mode), `deploy/runbooks/` one line for `lv08-timeline`; M04.md evidence.

## 8. Risks

- **A stray message in the chat** invalidates the closed window — the abort rule and the timeline check catch it.
- **A successful `send` reaches the second account** — expected and harmless (fixed synthetic text, the owner's own account).
- **The probe gate is too wide** — the five refusal tests are blocking for merge; the TL reviews the diff line by line.
