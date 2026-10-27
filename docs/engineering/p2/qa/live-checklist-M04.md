# M04 live window — Alviro, owner present (CP3)

*Script for the M04 manual integration tests (IMPLEMENTATION_PLAN §6) on the production bot, with the owner in the Telegram clients, the TL on the server and Antony recording. Every message is synthetic and numbered (`P2-4nn …`), sent exactly as written. Nobody reads message content: verification uses ids, states, hashes, counts and the synthetic numbers extracted inside SQL (the `verify_r0.py` technique). Record results in a copy of [evidence-template.md](evidence-template.md).*

## 0. Preconditions (TL ticks before the window starts)

- [ ] **Go-ahead:** the owner's go-ahead for this window is in the conversation (quote reference noted in the evidence).
- [ ] **Build:** `main` at the CP3 merge commit is deployed (`just deploy <ip>`); `just prod <ip> p2-verify release` shows the P2 Alembic head and the P2 tables.
- [ ] **Stack healthy:** `just prod-status <ip>` all green; poller heartbeat fresh; inbox backlog 0.
- [ ] **Connection:** Alviro is connected to the **main account** with **Reply to messages** granted; the **second account** is inside the Chat Automation scope (`/status` shows `can reply`).
- [ ] **Consents:** `ai_control` and `ai_managed` active (`/status`).
- [ ] **Test chat:** the private chat **main account ↔ second account**. The second account plays the **contact**. No other chat is used.
- [ ] **Clients:** owner has the main account on the **phone** and on **Telegram Desktop** (for the double tap), and the second account on phone or web.
- [ ] **Flags for Part 1:** `tool.get_activity`, `tool.pause_autonomy`, `tool.resume_autonomy`, `tool.propose_message`, `tool.propose_reply` on; `actions.send` **off**; `assist.drafts` off (WORKFLOW §10 steps 1–2). Confirm with `p2-verify release`.
- [ ] **Two days, one chat (for step 2.6):** Part 1 runs on day 1 and ends with the contact's last message of the day, `P2-450 Ertaga yana yozaman.` (the **anchor**; note its time). The contact sends nothing more in the test chat until step 2.6 is finished. Part 2 runs on day 2 and **starts with 2.6** between anchor + 13 h and anchor + 23 h, because any later contact message reopens the window.
- [ ] **Verification tooling exists:** `deploy/server.sh trial-check` and `p2-verify` (built in OPS-TV; modes in [test-plan.md](test-plan.md) §6.1).

Commands the TL runs (all content-free): `V = just prod <ip> p2-verify`; `V actions --since <window start ISO>`; `V action <id>`; `V script P2-4 --expect <range>`; `just prod <ip> trial-check --since <window start ISO>`.

## Part 1 — drafts only (`actions.send` off), about 45 minutes

*The `sending_enabled` rule is checked only at **execution** (P2-T21): proposals are normal cards, and Approve ends "Not sendable — sending is turned off". So live cards, edits, outdating and rejection are exercised here with no possibility of a send.*

| # | Who · where | Action (send exactly) | Verify | Expected | Pass/Fail |
|---|---|---|---|---|---|
| 1.1 | Second account · test chat | `P2-401 Salom, ertangi uchrashuv soat nechada?` | `V script P2-4 --expect 401` | 401 stored, direction contact | |
| 1.1b | Owner (phone) · control chat | "Draft a reply to <second account's name>" | `V actions --since …` | One card: contact, the triggering message, draft, "sendable until <time>", Approve · Edit · Reject. One action, `awaiting_approval`, v1, origin `agent_proposal` | |
| 1.2 | Owner · card | Tap **Edit**, then send `P2-402 Ertaga soat 10 da. Rahmat!` | `V action <id>` | Card shows version 2 with exactly that text (replacement used verbatim, P2-T08); `action_edited`; still `awaiting_approval` | |
| 1.3 | Owner · card | Tap **Edit**, then send "make it more formal" | `V action <id>` | Version 3 card with a revised draft; `action_revision_requested`, then `awaiting_approval` v3; nothing sent | |
| 1.4 | Owner · card | Tap **Approve** | `V action <id>`; `trial-check` | The press answers "Approved — sending is off" (never "Sending…"); card: "Not sendable — sending is turned off" with copy-ready text (copy button if ≤ 256 chars); state `blocked`/`sending_disabled`; zero `assistant_sent` rows; contact received nothing | |
| 1.5 | Owner · control chat, then second account | Ask for a new draft; while the card is open the second account sends `P2-403 Yana bir savol bor.` | `V actions …` | Card turns **Outdated** (new contact message) with **Re-draft**; `outdated`/`new_contact_message`; tapping Approve on it answers "no longer active" | |
| 1.6 | Owner · card, then main account · test chat | Tap **Re-draft** (new card). Then the owner writes manually in the test chat: `P2-404 Men o'zim javob beraman.` | `V actions …`; `V action <id>` | New card → **Outdated** (takeover); `outdated`/`takeover`; audit `takeover_detected`; the contact sees only P2-404 from the owner (owner confirms), nothing from the bot | |
| 1.7 | Owner · control chat | "Tell <second account's name>: P2-405 dictated test" → on the card tap **Reject** | `V action <id>` | Card shows the resolved recipient and exactly `P2-405 dictated test`; after Reject: `rejected`, nothing sent | |
| 1.8 | Owner · control chat | `/pause`; then "please resume autonomy"; then `/resume` | `/status` | `/pause` acknowledged at once; `/status` paused; the model's resume attempt only offers a **Resume** card (no change); `/resume` resumes | |
| 1.9 | Owner · control chat | `/activity` | — | Every action above listed with distinct labels: awaiting / outdated / rejected / not sendable; nothing labelled sent | |
| 1.10 | TL | `trial-check --since <window start>` | — | PASS with 0 sends | |
| 1.11 | Second account · test chat | Last message of day 1: `P2-450 Ertaga yana yozaman.` — then the contact stays silent in this chat until 2.6 | `V script P2-4 --expect 450` | Stored; note its time (the anchor) | |

## Part 2 — sending on (`actions.send` on), about 60 minutes

Day 2. TL enables `actions.send` (WORKFLOW §10 step 4), `p2-verify release` confirms, owner says "go". **Run 2.6 first** (it needs the window opened by the anchor to close with no newer contact message); then 2.1 onwards.

| # | Who · where | Action (send exactly) | Verify | Expected | Pass/Fail |
|---|---|---|---|---|---|
| 2.1 | Second account, then owner | `P2-411 Hujjatni qachon yuborasiz?` → owner asks for a draft → **Edit**, send `P2-416 Ertaga soat 11 da yuboraman.` → on the version 2 card tap **Approve** (draft → edit → approve → sent on **one** action, PRD R1 #3) | `V action <id>`; `V script P2-4 --expect 411,416`; `trial-check` | Version 2 card shows exactly P2-416; then "Sending…" and "Sent ✓ <local time>"; `succeeded`; approved version = 2; hash match **yes** (sent text = the approved version's text); exactly one `assistant_sent` row with this `action_id`; one attempt (`action_claimed` once); the contact sees exactly one new message, P2-416 (owner confirms count) | |
| 2.2 | Owner · phone **and** Desktop | New draft (`P2-412 Yana bitta savol.` from the contact first). Tap **Approve** twice quickly on the phone and once on Desktop | `V action <id>`; `trial-check` | One send, one attempt; the extra taps answer "no longer active"; contact sees one message | |
| 2.3 | Second account during a pending card | Owner asks for a draft; before approving, contact sends `P2-413 Kechirasiz, yana bir narsa.`; owner then taps Approve | `V action <id>` | Outdated before the tap; Approve answers "no longer active"; zero sends | |
| 2.4 | Main account during a pending card | Owner asks for a draft; before approving, owner writes manually `P2-414 O'zim yozdim.`; then taps Approve on the card | `V action <id>` | Outdated (takeover); zero sends from the bot | |
| 2.5 | Main account (phone) · Settings | Owner asks for a draft (contact sends `P2-415 Salom yana.` first). Before approving: Settings → Chat Automation → Alviro → turn **off** "Reply to messages". Then tap **Approve**. Then turn the right **on** again | `V action <id>`; `/status` | A connection notice arrives; the card becomes Outdated (rights changed) **or** Approve gives "Not sendable — reply right is off" with copy-ready text; zero sends; after restoring, `/status` shows `can reply` | |
| 2.6 (run first) | Owner · control chat (anchor P2-450, contact silent since) | Between anchor + 13 h and anchor + 23 h: ask for a draft in the test chat; leave the card until after anchor + 24 h; then tap **Approve** | `V action <id>` | `expires_at` ≈ anchor + 24 h − 5 min (the window, not the 12 h maximum age, decides); at expiry the card shows **Expired** with copy-ready text; Approve answers "no longer active"; zero sends. *If the timing cannot be met: record "covered by `test_the_window_closing_expires_the_draft_with_copy_ready_text`" and repeat at the next opportunity* | |
| 2.7 | Owner + TL | Contact sends `P2-417 Oxirgi savol.`; owner asks for a draft. TL has `just prod <ip> app-stop` typed and ready. Owner taps **Approve**; the TL presses Enter within 1–2 s. After 30 s: `just prod <ip> app-start`; wait ≥ 3 min (beyond the `mark_uncertain` threshold) | `V action <id>`; `trial-check` | One of: (a) `succeeded`, one message; or (b) `delivery_uncertain`, card "Delivery uncertain — please check the chat" and an owner alert. Never two messages (owner counts in the chat); attempts == 1; no automatic resend after restart | |
| 2.7b | Owner + TL | Variant: TL runs `app-stop` **before** the owner taps Approve on a new card (`P2-418 Test.` from the contact first); owner taps Approve while the app is down; then `app-start` | `V action <id>` | After restart the press is processed, the barrier passes, one send; `succeeded` | |
| 2.8 | Main account · test chat | Owner deletes, **for both sides**, the bot-sent message from 2.1 | `V action <id>` (2.1's) | Message row tombstoned; audit `assistant_message_deleted` (not attributed to anyone, LV-07); action stays `succeeded`; nothing resent | |
| 2.9 | Owner · Manage Bot panel; second account | TL enables `assist.drafts`. Owner opens the chat's panel (Manage Bot) and sets **Assist**. Contact sends `P2-421 Salom!`, `P2-422 Ertaga bo'shmisiz?`, `P2-423 Soat 3 da?` within 10 s | `V actions …`; agent-run count for `draft_reply` via `V release` counts | After ~20 s exactly **one** card for the burst. Then contact sends `P2-424 Ok, rahmat.` → at most one card (a "no reply needed" result shows none; record which) | |
| 2.10 | Owner · control chat | `/pause`; approve a pending owner-approved card (from 2.9); then `/resume` | `V action <id>` | The owner-approved draft is still sent once while paused (PRD §12.2 #1); `/resume` acknowledged | |
| 2.11 | Owner · control chat | `/activity` | — | Every send of this window listed with time and who approved; the uncertain one (if 2.7 b) shown as uncertain; rejected/outdated/expired drafts distinct from sends | |
| 2.12 | TL | `trial-check --since <window start>` and `V script P2-4 --expect 401-424` | — | PASS; every synthetic number present with the right direction; no unexpected `assistant_sent` row | |
| 2.13 | Owner | Set the test chat back to the mode the owner wants for the trial | — | — | |

## 3. After the window

- Antony fills the evidence file the same day; the TL pastes it into `docs/milestones/M04.md` (manual integration tests).
- Any FAIL: stop-the-line if it is an incorrect send (test-plan §7); otherwise an open item with the action id and timeline.
- The 7-day trial starts when Part 2 passes and `assist.drafts` is on for the chats the owner chose (WORKFLOW §10 step 5).
