# Telegram Feasibility & Platform Constraints

**Status:** Verified research baseline, 2026-10-17. Re-verify before each milestone that depends on a specific item.
**Audience:** Everyone building or reviewing this project. This document is the single source of truth for *what Telegram, our libraries, and our providers actually allow*. The PRD, architecture, and implementation plan cite it by section.

**How this was produced.** Nine independent research tracks (Bot API business surface, connection lifecycle, event semantics, Python libraries, jobs/deployment, channels/MTProto, policies/privacy/monetization, bot UX, competitors) plus two locale tracks (provider availability and Uzbek law; Uzbek language handling and hosting). Every critical claim was re-checked by a separate adversarial verifier against primary sources. Confidence labels:

| Label | Meaning |
|---|---|
| **Verified** | Stated in a primary source (core.telegram.org, telegram.org, official repos/PyPI, provider legal pages), re-checked by a verifier. |
| **Corroborated** | Not stated in Telegram's reference text, but supported by Telegram's open-source server/TDLib code and/or multiple independent field reports. |
| **Unverified → LV-nn** | Plausible but must be confirmed by a live test (see §11, Live Validation Protocol) before we build on it. |

**Observed versions (2026-10-17):** Telegram Bot API **10.3** (2026-08-24). MTProto docs Layer 225 (TDLib schema at 230). aiogram **3.31.0** (2026-08-26, supports Bot API 10.3). python-telegram-bot 22.8 (Bot API 10.0). PostgreSQL **18** (18.6). SQLAlchemy **2.1.5**. procrastinate **3.10.0**. Anthropic Python SDK `anthropic` **1.15.0**. Python **3.14** is the newest interpreter the whole stack supports (3.15.0 released 2026-10-09 is *not* yet supported by aiogram/pydantic-core).

---

## 1. Summary of what changes versus `PROJECT_CONTEXT.md`

The established integration approach — **one Telegram bot acting as both the owner's private control chat and the connected business ("Secretary Mode") bot** — is confirmed as viable, policy-compliant, and the right foundation. Several earlier findings are outdated or incomplete:

| # | Earlier finding (PROJECT_CONTEXT §6) | Current verified reality | Practical implication |
|---|---|---|---|
| C-01 | Users may connect a business bot without Premium (uncertain). | **Confirmed, since Bot API 10.0 (2026-05-08):** "Allowed Business Bots to manage user accounts without a Telegram Premium subscription." Before May 2026 Premium was required. Other Telegram Business features (away/greeting messages etc.) remain Premium-only. Telegram's ToS §5.4 and Privacy Policy still say "Telegram Business subscribers" (stale wording). | No Premium dependency for the owner or future users. The future addressable market is all Telegram users. |
| C-02 | "Business bot", "connected business bot". | Names changed: developer enables **Secretary Mode** in @BotFather (formerly "Business Mode"); users connect via **Settings → Chat Automation** (older clients: Settings → Telegram Business → Chatbots). The API still says "business". | Setup docs and onboarding must mention both UI paths. |
| C-03 | 24-hour restriction on account-level replies. | The limit is **per chat**, built into the `can_reply` right: the bot may "send **and edit** messages in the private chats that had **incoming** messages in the last 24 hours." It also covers typing indicators (`sendChatAction`). By the `can_reply` wording only *incoming* (contact) messages count; owner messages are expected not to reopen the window (unverified → LV-08). Violations return MTProto `BUSINESS_PEER_USAGE_MISSING`. | We track the last contact message time per chat (`conversations.last_contact_msg_at`), check the window before every send/edit, and degrade to "copy text for manual sending". The bot can never start a conversation or send late follow-ups as the owner. |
| C-04 | Bot receives "permitted business-message events". | Confirmed, and **bidirectional**: the contact's messages *and* the owner's manually typed messages arrive as `business_message` (Corroborated by a documented live test, Telegram's open-source server code, and field logs → LV-02). Messages the bot itself sends are **probably not echoed** (the only live test saw no echo; the send method's return value is the authoritative record → LV-03). | Every business update must be **classified by sender** before any logic runs. Owner messages are the human-takeover signal. Bot-sent messages are recorded from the send result. |
| C-05 | Connection can be paused or disconnected. | Three levels: per-chat **pause** and per-chat **disable** (from an action bar in each managed chat) and global disconnect. The bot gets **no per-chat pause signal** — it only sees updates stop and sends fail. Global changes arrive as `business_connection` updates; `business_connection_id` **may change** when settings change. | Key connections by the owner's Telegram user id, treat the connection id as mutable, and treat send failures/silence as possible pauses. |
| C-06 | "No unrestricted access to history." | Stronger: there is **no history API at all** for business chats. The bot sees only messages created/edited after connection, in chats the owner included, and delivered within 24 h of the message date. | Our own message store is the only history. Summaries must state their coverage. Downtime longer than ~24 h loses data permanently. |
| C-07 | Proposed stack: aiogram 3, FastAPI, PostgreSQL, SQLAlchemy, Alembic, scheduler TBD. | aiogram 3.31 is current and complete. APScheduler 4 is still pre-release and APScheduler 3 cannot share a job store across processes → use **procrastinate** (Postgres-only job queue). FastAPI is **not needed for long polling**; justified only when webhooks/Mini App/OAuth arrive. SQLAlchemy is now 2.1 (Python ≥ 3.11). | See ARCHITECTURE.md §3 for the final stack and rationale. |
| C-08 | Channel intelligence constrained by API access. | Constrained by **API access and Telegram policy**. Bots read channels only where they are **administrators**; no history; Telegram terms prohibit scraping/aggregating channel content and using Telegram data for AI development. MTProto user clients carry high account and compliance risk. | Channel features limited to owned/admin channels and user-forwarded posts; MTProto excluded unless separately decided. |
| C-09 | (Not covered) | Telegram **Bot Developer Terms §5.4** bind business bots: state retention; use data only for the service; **no disclosure of message contents to third parties including third-party APIs without the user's authorization**; never conceal bot activity from the owner; notify of scope changes. §4.4(a): data **encrypted at rest, key stored separately**. §4.3: no data collection for datasets/ML; minimisation. Violations can ban the bot **and the owner's account**. | Consent record naming the LLM provider before any business content reaches it; owner-visible activity log; retention policy; encryption at rest; no training/eval on real chats. |
| C-10 | (Not covered) | Anthropic's Usage Policy requires AI agents that **communicate directly with external users** to **disclose they are AI** (at least at the start of each chat session). | Autonomous replies must carry an AI disclosure; the owner chooses wording/placement, not whether. Owner-approved drafts are treated as the owner's own messages (inferred; see §7.2 for both policy versions). |

---

## 2. Connection & permissions

| Capability | Supported | Mechanism & constraints | Confidence |
|---|---|---|---|
| Bot can be connected to a personal account | Yes | Developer enables Secretary Mode in @BotFather → `getMe` returns `can_connect_to_business = true` (MTProto `bot_business`; without it connecting fails with `BOT_BUSINESS_MISSING`). User connects via Settings → Chat Automation. | Verified |
| Premium required | **No** (since Bot API 10.0) | See C-01. | Verified |
| One bot serves both control chat and business role | Yes | No policy forbids it; every reference implementation does it. One privacy policy must cover both roles. | Verified |
| Number of business bots per account | **One** ("Currently just one business bot may be connected to a user account") | Our bot occupies the owner's only slot; a user with another secretary/CRM bot must disconnect it. Dev and prod bots cannot both be connected to the same account — see ARCHITECTURE.md §15.3. | Verified |
| Chat scope selection | Owner-controlled | `businessBotRecipients`: `existing_chats`, `new_chats`, `contacts`, `non_contacts`, explicit `users`, `exclude_selected` (inverts), `exclude_users`. **Private, non-secret 1:1 chats only.** The bot is **never told** the selection. | Verified |
| Rights | Owner-controlled, per connection (not per chat) | `BusinessBotRights` (14 optional fields): `can_reply`, `can_read_messages` (= *mark as read*, not read access), `can_delete_sent_messages`, `can_delete_all_messages`, `can_edit_name`, `can_edit_bio`, `can_edit_profile_photo`, `can_edit_username`, `can_change_gift_settings`, `can_view_gifts_and_stars`, `can_convert_gifts_to_stars`, `can_transfer_and_upgrade_gifts`, `can_transfer_stars`, `can_manage_stories`. **No right is needed to receive messages.** Field `can_delete_outgoing_messages` was renamed `can_delete_sent_messages` (~9.1). The docs for `setBusinessAccountName/Username/Bio` mention `can_change_*` but the real fields are `can_edit_*`. | Verified |
| Observe-only connection (no reply right) | **Yes, and common** | Default rights depend on the owner's client: **Telegram Desktop connects view-only by default (no `can_reply`)**; Android defaults to reply + mark-read + both delete rights. Onboarding must read the rights actually granted and show the resulting capability tier. Observe-only is a fully valid product configuration (summaries, memory, monitoring, drafts sent manually). | Corroborated (official client source) / LV-19 (defaults per client), LV-12 (zero rights) |
| `BusinessConnection` fields | — | `id`, `user`, `user_chat_id` (owner↔bot private chat; int64), `date`, `rights` (optional; may be null when disabled), `is_enabled`. **No recipients/scope field.** Treat missing rights or `is_enabled=false` as *no permissions*. | Verified |
| Connection lifecycle updates | Yes | Connect, disconnect, and edits (rights or recipients) emit `business_connection`. Disconnect → `is_enabled=false`. | Verified |
| `business_connection_id` stability | **Not stable** | MTProto docs: connection id can change when connection parameters change; old id → `BUSINESS_CONNECTION_INVALID`. Unknown id on an incoming message → call `getBusinessConnection` (documented purpose: fetch a not-yet-cached connection). A documented live test saw the **initial `business_connection` update go missing** while `business_message` updates arrived; rights changes on a live connection were delivered reliably. → Bootstrap connections from any update carrying an unknown id. | Verified (MTProto) / Corroborated (live test) / LV-05 |
| Per-chat pause / disable | Owner-side only | Action bar in each managed chat: Pause/Resume (`account.toggleConnectedBotPaused`, no documented duration or auto-resume), "Manage Bot" (opens our bot with `/start bizChat<id>`), "Disconnect from this chat" (`account.disablePeerConnectedBot`, permanent until the bot is reconnected). Both add the peer to `exclude_users`. **No documented bot-side signal identifies the chat.** | Verified (mechanism) / LV-04 (what the bot observes) |
| Connection confirmation from another device | Possibly | TDLib layer 227+ models connected bots like sessions with an "unconfirmed" state (`confirmBusinessConnectedBot`). Effect on the Bot API is undocumented → LV-13. | Corroborated (schema) |
| Anyone can connect our bot | Yes | No documented allowlist for business connections. Application must **ignore connections from non-allowlisted users** (fail closed). `BotAccessSettings.is_access_restricted` may add a layer → LV-14. | Verified |

---

## 3. Inbound events

### 3.1 Update types and delivery

| Item | Reality | Confidence |
|---|---|---|
| Business update types | `business_connection`, `business_message`, `edited_business_message`, `deleted_business_messages`. | Verified |
| Delivered by default? | Yes. An empty `allowed_updates` list means all types **except** `chat_member`, `message_reaction`, `message_reaction_count`. But `allowed_updates` is **sticky** ("If not specified, the previous setting will be used"), and aiogram's `start_polling` derives the list from registered handlers. **Always pass an explicit list.** | Verified |
| Retention of undelivered updates | At most **24 hours**. For message updates the clock runs from the **message (or edit) date**; `business_connection`/`deleted_business_messages` get 86 400 s from queueing. Messages already > 24 h old when processed server-side are never emitted. | Verified + Corroborated (server source) |
| Outage impact | An outage shorter than the remaining window loses nothing (updates queue and are delivered later). Longer outages lose business messages **permanently** — there is no backfill. | Verified |
| Duplication | At-least-once. Webhook: non-2XX/timeouts are retried (open-source server: immediate, then 2, 4, 8 s… capped at 60–120 s; assumed identical on api.telegram.org). Polling: an update is confirmed only when `getUpdates` is called with a higher offset. | Verified + Corroborated |
| Ordering | `getUpdates` returns updates in `update_id` order. Webhooks: FIFO per chat queue, concurrent across chats; `business_connection` updates travel on a separate queue, so a rights change can arrive after messages that depend on it. A poison update stuck at the head blocks its chat's webhook queue until expiry. | Verified + Corroborated |
| `update_id` | Sequential, but becomes **random after ≥ 1 week without updates**. Use as an idempotency key only, never as a watermark. | Verified |
| aiogram defaults are unsafe for durability | Polling advances the offset before handlers finish and runs handlers as concurrent tasks; the aiohttp webhook handler answers 200 before processing. | Verified (source) |

### 3.2 What arrives in a `business_message`

| Sender case | How to recognise it | Confidence |
|---|---|---|
| Contact's incoming message | `from.id != BusinessConnection.user.id`, `chat.type == "private"`, `chat.id == contact's user id`. | Verified |
| Owner's **manual** message (typed in their own Telegram apps) | `from.id == BusinessConnection.user.id`, no `sender_business_bot`, no `is_from_offline`. | **Corroborated** (grammY docs, Telegram server source applies no outgoing filter, TDLib, ≥ 3 field reports) → LV-02 |
| Owner's **automated** message (away/greeting/scheduled) | `is_from_offline == true`. Owner-authored, but *not* evidence the owner is present. | Verified |
| Message sent by **our bot** via `business_connection_id` | The send method returns the `Message` synchronously, with `sender_business_bot` set (note: `message_id` may be 0 if the server schedules it). **An echo update probably does not arrive**: the only documented live test saw none, and Telegram's server/TDLib code returns the single resulting update as the send result. Earlier "echo" claims came from code reading, not payloads. Design: persist the send result as authoritative; upsert idempotently so an echo, if ever delivered, is harmless. | Verified (field semantics) / Corroborated (live test) / **LV-03** |
| Mirror of the owner's chat **with our bot** (or other bots) | One detailed field report: the owner↔our-bot DM can appear as `business_message` with `chat.id == OUR_BOT_ID` and a different `message_id` space; chats with third-party bots carry that bot's id. | Corroborated (single report) → LV-06 |
| Replied-to message | `reply_to_message` is included even if it predates the connection — limited extra context, not a history API. | Corroborated |
| Edits | `edited_business_message` carries the full new message with `edit_date`, for both parties. Edits of messages older than 24 h (even pre-connection) can arrive, referring to messages we never saw. The server does not apply its "content unchanged" filter to business edits → expect no-op edits. | Verified + Corroborated |
| Deletions | `deleted_business_messages` carries **only** `business_connection_id`, `chat`, `message_ids` — no content, sender, or timestamp. Whether one-sided ("delete for me") deletions arrive → LV-07. | Verified / LV-07 |

**Never received:** read receipts, typing indicators, online status, reactions in business chats, history before connection, groups/supergroups/channels, secret chats, view-once/self-destructing media, calls and many service messages, imported messages, and anything in excluded or paused chats. The bot **cannot distinguish a paused chat from a quiet one.**

### 3.3 Human takeover — what is and is not observable

- **Observable after the fact:** owner's manual messages, owner edits, owner deletions (including deletion of a message our bot sent — a strong "owner rejected the bot's reply" signal), `business_connection` changes, owner tapping "Manage Bot" (`/start bizChat<id>` arrives in the control chat).
- **Not observable:** the owner *about to* reply (no typing/read signals); anything in paused/excluded chats.
- **Consequence:** conflicting responses can be **reduced** (owner-activity cooldown, send delay/debounce, re-check immediately before sending, cancellation on owner activity) but **not fully prevented**. The PRD must not promise more.

---

## 4. Outbound actions

| Action | Supported | Constraints | Confidence |
|---|---|---|---|
| Send as the owner (text, media, etc.) | Yes | Requires `rights.can_reply`; only in private chats with an incoming message in the last 24 h; cannot start chats; bots cannot schedule messages (`SCHEDULE_BOT_NOT_ALLOWED`) — scheduling is ours. Error mapping: window → `BUSINESS_PEER_USAGE_MISSING`; unmanaged chat / no right / likely paused → `BUSINESS_PEER_INVALID`; rotated id → `BUSINESS_CONNECTION_INVALID`; disallowed method → 403 `BOT_ACCESS_FORBIDDEN`. Bot API surfaces these as "Bad Request: <CODE>" (inferred) → LV-08. | Verified (rights/MTProto errors) / LV-08 |
| Methods accepting `business_connection_id` | 51 in Bot API 10.3 | 30 optional (all common `send*`, `sendChatAction`, `edit*`, `stopPoll`, `pin/unpinChatMessage`, `sendRichMessage`, `createInvoiceLink`…) and 21 required (`getBusinessConnection`, `readBusinessMessage`, `deleteBusinessMessages`, `sendChecklist`, profile/gift/Stars/story methods). | Verified |
| Edit as the owner | Yes, within the same window | Bot-sent messages under `can_reply`; **owner-typed** messages without inline keyboard are editable within 48 h (implies the bot *could* edit the owner's messages — treat as a forbidden capability). | Verified (rights wording); out-of-window edit error undocumented → LV-08 |
| Delete | Yes | `deleteBusinessMessages` (1–100 ids, one chat, < 48 h old): own messages need `can_delete_sent_messages`, any message needs `can_delete_all_messages`. Business bots cannot delete "for me only" (`SELF_DELETE_RESTRICTED`). | Verified |
| Mark as read | Yes | `readBusinessMessage` needs `can_read_messages` and a chat active in the last 24 h. Hides the unread signal from the owner → **off by default**. | Verified |
| Typing indicator as owner | Yes | `sendChatAction` with `business_connection_id`, same 24 h window; the contact sees the owner "typing". Not used by the product (§4.2). | Verified |
| Stream a draft into a managed chat | **No** | `sendMessageDraft` / `sendRichMessageDraft` have no `business_connection_id`. Streaming works only in the owner↔bot control chat. | Verified |
| Forward/copy business messages into the control chat | **No** | `forwardMessage(s)`/`copyMessage(s)` have no business context ("not possible to forward a message between different accounts"). Content must be re-rendered from our store. | Verified |
| React as the owner | **No** | `setMessageReaction` has no `business_connection_id`. | Verified |
| Interactive markup on owner-sent messages | Limited | Reply keyboards, `ReplyKeyboardRemove`, `ForceReply`, `web_app` and `switch_inline_query*` buttons are unsupported; callback/url/copy_text inline buttons work **but the contact sees them**. → All owner controls live in the control chat. | Verified |
| Native checklists | Business-only | `sendChecklist` requires `business_connection_id` → cannot be used in the control chat. | Verified |
| Rich messages as the owner | Conditional | Only if the owner "can send rich messages" (likely Premium-gated). Plain `sendMessage` is the default for owner-voice replies. | Verified / Inferred |
| Idempotency key on sends | **None** in the Bot API | → at-most-once state machine with a `delivery_uncertain` outcome (ARCHITECTURE.md §9.4). | Verified |

### 4.1 aiogram hazards (must be enforced in code review)

aiogram 3.31's `Message` shortcuts (`answer`, `reply`, `edit_*`, `delete`, `pin`, `react`, `answer_*`, `reply_*` — 49 in total) **automatically inject `business_connection_id`**. Inside a business handler, `message.answer()` sends a real message **as the owner to the contact**. `Message.react()`/`delete()` pass a parameter that `setMessageReaction`/`deleteMessage` do not support, and `forward()`/`copy_to()` resolve against the bot's *own* chat with the same numeric id — wrong-target hazards. aiogram's FSM `DefaultKeyBuilder` omits `business_connection_id` by default. → **Rule:** business updates never pass through aiogram handlers or shortcuts; all owner-voice sends go through one gateway (AGENTS.md §7).

A real-world precedent: Hermes Agent issue #127430 (2026-09) processed `business_message` updates as owner commands and sent agent replies to the owner's contacts. Owner-chat traffic and business traffic require a **hard boundary**.

### 4.2 Identity pitfalls

- A business `chat.id` lives in a **namespace separate from the bot's own chats** even when the number is identical → key business conversations by (owner account, chat id), never chat id alone.
- `user_chat_id` (owner↔bot chat) is reachable for notifications, but one live test showed the owner must **/start the bot first** → onboarding begins with /start; a connection from a user who never started the bot stays inert; LV-09.
- The "Manage Bot" deep link is documented as `/start bizChat<user_chat_id>`; grammY documents the id as the **managed chat's** identifier, but Telegram's wording is ambiguous → parse defensively and verify the id belongs to a chat seen under the owner's connection; LV-10.
- Sending to a business contact's chat id **without** `business_connection_id` goes out **as the bot** into the bot's own chat with that user (delivered if the contact ever started the bot). Both directions of mis-routing are real; the outbound gateway takes an explicit target type (`ControlChat` vs `BusinessChat`).
- **Outbound allow-list:** the product needs only `sendMessage`, `sendChatAction` (control chat only; a business-chat typing indicator is an owner-voice action and stays unused until a PRD requirement and policy rule exist — AGENTS.md §7), `editMessageText` (in business chats, assistant-sent messages only), `deleteBusinessMessages` (assistant-sent messages only, for "undo"), `readBusinessMessage` (opt-in, R3), `getBusinessConnection`, and `sendDocument` (control chat only, /export), plus control-chat UI methods. Every other business method (profile, gifts, Stars, stories) is disallowed in code to keep behaviour predictable under ToS §5.2.

---

## 5. Control-chat (owner ↔ bot) platform features

| Feature | Facts | Use |
|---|---|---|
| Inline keyboards & callbacks | `callback_data` 1–64 bytes; every callback must be answered (`answerCallbackQuery`, text ≤ 200 chars). Button `style` (success/danger/primary, 9.4), disabled buttons (10.3), `copy_text` (1–256 chars), `force_reply` on inline keyboards (10.3). | Approval cards with server-side state behind short tokens. |
| Editing | Only messages with no markup or an inline keyboard can be edited; `deleteMessage` < 48 h. | Cards are edited to show outcome/expiry. |
| Commands & deep links | `setMyCommands` ≤ 100 commands, 7 scope types; deep-link payload ≤ 64 chars `[A-Za-z0-9_-]`. Updates carry no scope info → authorise in code. | Owner command menu via chat scope; `/start bizChat<id>` per-chat panel. |
| Text limits & formatting | Text 1–4096 chars (captions ≤ 1024); MarkdownV2 (strict escaping), HTML, legacy Markdown; `date_time` entity (9.5). **Rich Messages** (10.1+): up to 32 768 chars, GFM-like markdown/html/blocks; older clients show a placeholder. | HTML with a sanitizer and block-boundary splitting by default; rich messages later for digests. |
| Streaming | `sendMessageDraft` open to all bots since 9.5; empty text shows "Thinking…" (10.0); `can_stop` + `stopped_message_generation` (10.3); a draft is a 30-second preview finalised with `sendMessage`. Private chat with the bot only. | Progress indicator for long owner requests (optional polish). |
| Private-chat topics | Since 9.3/9.4; enabled in @BotFather; **enabling topics adds a non-refundable 15 % fee on Stars purchases** (Bot Developer Terms §6.2.6; the clause also mentions streaming of message drafts — streaming via `sendMessageDraft` without topics is inferred not to trigger it). | Not in the initial release. |
| Rate limits | ~1 msg/s per chat (short bursts tolerated), ~30 msg/s overall, 20 msg/min per group; 429 carries `retry_after`. Whether business sends share these limits is undocumented → apply conservatively. | Central outbound throttle. |
| Reactions | Bot can set ≤ 1 reaction per message; delivery of the owner's reactions in the bot chat undocumented. | Lightweight acknowledgements only, never approvals. |
| Mini Apps | `initData` HMAC validation; cross-origin calls blocked by default since 2026-07-20; `web_app` buttons only in the bot's private chat. Telegram explicitly suggests Mini Apps for "configuration, approvals and dashboards". | Deferred (M08+). |
| Guest Mode (10.0) | Bot can be @mentioned in any private/group chat; sees only the tagged message and its reply target; answers once, visibly. | Possible future group feature; not a monitoring path. |
| Managed Bots (9.6) | A manager bot can create per-user bots. Secretary Mode does not appear to be API-toggleable for managed bots. | Alternative multi-user model, not planned. |
| Privacy mode | Affects groups only; bots receive all private-chat messages. | No handling needed. |

---

## 6. Media

| Item | Facts | Confidence |
|---|---|---|
| Download limit | `getFile` ≤ **20 MB** on the cloud Bot API; link valid ≥ 1 h. Uploads ≤ 50 MB. A local Bot API server lifts limits but needs api_id/api_hash, a C++ build, and a `logOut` from the cloud server — deferred. | Verified |
| `getFile` for business-message media | Worked in one documented live test (voice note, animation). Long-term file_id validity unknown (TDLib has no file-reference repair source for business messages) → download promptly at ingestion if media is needed. Protected-content chats untested. | Corroborated / **LV-11** |
| Re-sending business media into the control chat by file_id | Inferred possible; untested. | LV-11 |
| View-once media | Never delivered. | Verified |

---

## 7. Policy, legal, and provider constraints

### 7.1 Telegram (binding on the bot and potentially the owner's account)

| Source | Requirement | Design response |
|---|---|---|
| Bot Developer Terms §5.4 (business bots) | (i) truthful scope; (ii) state retained data, duration, purpose; (iii) use data only to provide the service; (iv) never disclose message contents/files/data to third parties **including third-party APIs** without the user's authorization; (v) never conceal bot activity from the owner; (vi) notify significant changes with opt-out. Breach → "immediate and permanent ban of your TPA **and your account**". | Versioned consent naming each processor before any business content reaches an LLM; owner-visible activity log; published retention; capability-scope versioning with change notices. |
| §5.2 | Behave predictably; do not use business privileges beyond the purpose they were granted for; no unsolicited messages/spam. | No outreach, no bulk sends; automation scoped by explicit owner rules. |
| §4.2 | Delete data on request, when no longer needed, when the bot ceases operating. | Retention sweeper, per-chat purge, account deletion. |
| §4.3 | No collection beyond what is essential; no data collection aimed at datasets, ML models, AI products; user-submitted data usable with explicit, revocable consent. | Data minimisation; per-class retention; **no training, fine-tuning, evaluation or benchmarking on real chats** — eval data must be synthetic or hand-written. |
| §4.4(a) | User data **encrypted at rest and stored separately from its encryption key**; breach alerts as law requires. | Encrypted storage volume + encrypted off-site backups; secrets outside the DB (ARCHITECTURE.md §14.2). |
| §4.5 | Credentials secret; actions taken with your credentials are deemed yours. | Secret handling rules; token never logged (Bot API URLs contain the token). |
| §8.1 (branding) | Bot name/username/description must not include Telegram trademarks or impersonate Telegram. | **The product needs a neutral name** before the prod bot's @BotFather setup (open decision Q-01, needed by M02 S02.5). The M00 probe/dev bot uses a neutral throwaway username without Telegram trademarks. |
| Privacy policy | Mandatory. Telegram's Standard Bot Privacy Policy applies only if none is registered; it does not fit an AI secretary (no AI/retention language). @BotNews 96: serve via `/privacy`. | Custom privacy policy registered in @BotFather, versioned in the repo, served by `/privacy`. |
| Content Licensing & AI Scraping Terms; API ToS §1.5 | Content Licensing & AI Scraping Terms: prohibit scraping/aggregating content and using platform data to "train, fine-tune, validate or otherwise engage in the development, enhancement, benchmarking or **deployment** of artificial intelligence", except with explicit consent of "all relevant users" for a specific context. API ToS §1.5: "train, fine-tune or otherwise engage in the development, enhancement or deployment of artificial intelligence". Read literally, either could even reach LLM processing of private chats; Telegram's own docs promote "a business secretary that processes incoming business messages" and AI agents via business bots. | **Product-wide compliance risk (IMPLEMENTATION_PLAN §1.3 A11; §13 PQ-04).** Personal use: low risk when processing is strictly for the owner's service. Never train/eval on real chats; disable provider feedback programs; no cross-counterparty disclosure. Seek clarification (@BotSupport or counsel) before public launch. |
| Payments (future) | Digital goods/services sold inside Telegram must be paid **exclusively in Stars (XTR)**: no third-party payment processors or external checkout links in the bot, and anything also sold outside Telegram must also be offered for Stars (Bot Developer Terms §6.2; @BotNews 90). Subscriptions only via `createInvoiceLink` with `subscription_period = 2592000` (30 days), ≤ 10 000 Stars; `/paysupport` mandatory, `/terms` recommended; developer receives ≈ $0.013/Star after up to 21 days. | Out of scope until the owner decides to monetize. Local payment options only for sales made outside Telegram, never linked from the bot (PRD §18 R4). |

### 7.2 AI provider (Anthropic Claude, the recommended provider)

| Item | Fact | Source class |
|---|---|---|
| Availability | **Uzbekistan is a supported country** for the Claude API. The policy depends on where people are *physically located* and where an entity is incorporated, not where the server runs. **Russia is unsupported** — relevant for any future public product with users in Russia, and for the owner while travelling there. | Verified |
| Training | Commercial terms: Anthropic may not train on API customer content. Do not submit feedback (feedback can be retained up to 5 years). | Verified |
| Retention | API inputs/outputs deleted within 30 days by default; flagged content up to 2 years. ZDR only by arrangement with sales. Batch API (29 days) and Files API (until deleted) have their own retention — do not use them for message content. | Verified |
| DPA | Data Processing Addendum (with EU SCCs) incorporated into the Commercial Terms. | Verified |
| Usage Policy — disclosure | AI agents that communicate directly with external users must disclose they are AI (current text: "at a minimum at the beginning of each chat session"; text effective 2026-11-12 also allows disclosure "in the product interface"). Impersonating a human is prohibited. | Verified |
| Usage Policy — surveillance/profiling | Prohibits tracking a person's communications without consent, scoring individuals' trustworthiness without notice, and (from 2026-11-12) emotion-recognition systems. | Verified |
| Billing from Uzbekistan | Whether an Uzbek-issued card works on the Console is unverified → test in M00. Claude Platform on AWS (AWS Marketplace billing) is an alternative route. | Unverified |
| Alternatives | OpenAI API and paid Gemini API also support Uzbekistan (Gemini's unpaid tier trains on data — never use it). | Verified |
| Uzbek quality | Anthropic publishes no Uzbek benchmarks. On UzLiB (only current Uzbek benchmark, Latin script, spelling-heavy multiple choice), Gemini models lead; Claude Sonnet 5.5 ranks 7th (0.781); Opus 5.5 untested. TUMLU (2025, older models) found Claude best on Uzbek and negligible Latin/Cyrillic comprehension gap, but weaker *generation* in Turkic languages. | Verified (benchmarks); quality for our tasks **must be measured** |

### 7.3 Privacy law (practical baseline — not legal advice)

| Regime | Personal single-user phase | Public multi-user product |
|---|---|---|
| **Uzbekistan** — Law "On Personal Data" (ZRU-547) as amended by **ZRU-1125 (in force 2026-03-27)** | The 2021 blanket localization rule **no longer applies**: mandatory in-country storage now covers only biometric data, genetic data, and telecom-operator subscriber data. Art. 3 exempts "processing of personal data by a natural person for personal and household purposes" → hosting abroad with a foreign LLM API is low risk. (Caveat: business correspondence weakens the "household" reading.) The AI Ethics Rules (Order 284-mh, reg. No. 3787, in force 2026-06-17) apply without a household exemption (no penalties; human control, final decision by a human): keep autonomy opt-in, narrowly scoped, logged and reviewable. | Becomes an owner/operator. "Private life" information is special data (written/electronic consent). Delegating processing to a third party (LLM provider) needs consent, contract, or legal basis. Cross-border transfers: Cabinet Resolution No. 415 (in force 2026-08-03) lists 49 adequate states (EU, UK, Switzerland, Russia, …; USA only for EU-US DPF companies). **Anthropic is not DPF-listed** → first-party API is a non-listed destination whose transfer conditions are not yet published; Claude via AWS Bedrock or Google Cloud in EU regions is the fallback route. AI law ZRU-1115 (fines for unlawful AI processing of personal data) and AI Ethics Rules (human control, published user guide). Raw voice/face media may itself be biometric data (Art. 26) subject to localization: do not store or send raw voice notes or photos abroad in a public product without legal review; no voiceprint/face features. **Lawyer required.** |
| **GDPR** (strictest common baseline; applies if EU users/contacts) | Household exemption *may* apply; lost if work/business chats are processed. | Operator is a controller: lawful basis for counterparties (legitimate interests), no LI route for Art. 9 special-category data, Art. 14 transparency, DPAs, transfer safeguards, DPIA, possibly an EU representative. **Lawyer required.** |
| **EU AI Act Art. 50(1)** (since 2026-08-02) | Natural persons acting purely personally are exempt (grey area). | Disclosure duty for AI systems interacting with people. |

**Design consequence:** build to "privacy by design" now (minimisation, retention, consent, deletion, export, audit, encryption), so the public path does not require a rewrite — but do **not** implement public-launch legal machinery before the owner decides to go public.

---

## 8. Library and stack verification

| Component | Verified state | Decision input |
|---|---|---|
| aiogram 3.31.0 | Supports Bot API 10.3, all business observers/types/methods; tracks Bot API releases within ~0–3 days (one yanked release, 3.29.0). Requires Python ≥ 3.10, < 3.15; caps pydantic < 2.14 (→ pydantic 2.13.5). No public test toolkit (copy a `MockedSession` pattern). No aiogram 4 in development. | Use aiogram 3.31.x as the Bot API client/types and control-chat router; pin exactly; upgrade deliberately. |
| python-telegram-bot 22.8 | Maintained, business support, but only Bot API 10.0 (> 4 months behind); no pydantic coupling; Python 3.15 ready. | Fallback only. |
| Python | 3.15.0 released 2026-10-09 but unusable (aiogram cap, no pydantic-core cp315 wheels). 3.14 supported by everything. SQLAlchemy 2.1, Starlette, pydantic-settings require ≥ 3.11. `uv` ignores dependency upper bounds on `requires-python`. | Pin `requires-python = ">=3.14,<3.15"` and `.python-version = 3.14`. |
| SQLAlchemy 2.1.5 | Python ≥ 3.11; greenlet only via `[asyncio]`; plain `postgresql://` now selects **psycopg 3**. | Always use explicit driver URLs. |
| PostgreSQL 18 | Current (19 RC). Snowball stemmers: no Uzbek (Russian, English, Turkish exist). pg_trgm ignores non-alphanumerics; whether `U+02BB` (oʻ) counts as a letter depends on collation provider. Stock `unaccent` wrongly maps Uzbek letters (ş→s, ё→е). | App-side normalisation for search keys; `simple` config + prefix queries + pg_trgm; no `unaccent` on Uzbek. |
| procrastinate 3.10.0 | Production/Stable, MIT, Postgres-only async queue: `schedule_at/in`, periodic (code-declared, skipped if > 10 min late), retries (`exponential_wait` is base^attempt — use small bases), `lock`/`queueing_lock`, cancel/abort, heartbeats (stalled jobs need an explicit periodic `retry_stalled_jobs`), `InMemoryConnector` for tests, plain-SQL migrations (no Alembic integration), **requires psycopg 3**; transactional defer only on a raw psycopg connection (SQLAlchemy async needs the raw driver connection → spike). LISTEN incompatible with PgBouncer transaction pooling. Semantics the design relies on: worker concurrency defaults to 1 (set it per queue); jobs sharing a `lock` run one at a time in priority/creation order, so a future-scheduled locked job blocks later jobs with the same lock; `queueing_lock` dedupes only while a job is in `todo` (not while running or after completion). | Use procrastinate behind a `JobScheduler` port; domain rows remain the source of truth; spike transactional defer. |
| APScheduler | 4.x still pre-release (4.0.0a6, "do NOT use in production"); 3.x cannot share a persistent job store across processes. | Rejected. |
| arq / Celery / dramatiq / taskiq / saq | arq: Redis, maintenance-only. Celery/dramatiq: need RabbitMQ/Redis. taskiq: weak Postgres support. saq: credible but thinner docs. | Rejected (saq is the second-choice alternative). |
| psycopg 3 on Windows | Async psycopg does not work on the default Windows Proactor loop; policy workaround deprecated in 3.14 (supported replacement: `asyncio.run(..., loop_factory=asyncio.SelectorEventLoop)`). | Develop inside Linux containers / WSL2 (ARCHITECTURE.md §15.1). |
| FastAPI 0.143 | Current; justified by future webhooks/Mini App/OAuth/admin API, not by polling. aiogram's webhook integration is aiohttp-only; FastAPI requires a custom route using `feed_update`. | Deferred until an HTTP surface is needed. |
| Anthropic SDK | `anthropic` 1.15.0 (Python ≥ 3.10), built on `httpx2`. | Use behind an internal LLM port; mock at the port, not at HTTP. |

---

## 9. Channel intelligence options

| Path | Feasible | Policy risk | Status |
|---|---|---|---|
| Bot added as **administrator** to channels the owner owns/administers (user needs add-admin rights; ownership not required) → `channel_post`/`edited_channel_post`; specific earlier posts retrievable by id but no listing/search | Yes | Low (owner controls content) | Later milestone (M08) |
| Owner **forwards** posts to the bot (`forward_origin: MessageOriginChannel`; fails for protected channels) | Yes | Low–medium (user-submitted with consent; channel authors arguably "relevant users") | Later milestone (M08) |
| `getUserPersonalChatMessages` (Bot API 10.0): last 1–20 messages of a user's profile-linked personal channel | Yes (narrow) | Low if used sparingly | Optional contact-context feature |
| Bot reading followed channels it does not administer | **No** | — | Not possible |
| MTProto **user client** (Telethon active; Pyrogram unmaintained; Kurigram active; TDLib official) reading all followed channels | Technically yes | **High**: API ToS duties (own api_id, "Unofficial" naming, sponsored messages, no ghost mode), unofficial-client observation and possible bans, full account takeover risk from session strings, a possible profile warning shown to contacts (`unofficial_security_risk`), AI/scraping clauses, bots may not ask for passwords/OTPs. Hosted multi-user MTProto strongly inadvisable. | **Excluded** unless the owner makes a separate decision after a dedicated review |
| Scraping `t.me/s/<channel>` web previews | Technically fragile | Prohibited by terms | Rejected |

---

## 10. Competitive landscape (summary)

| Reference | What it is | Takeaways |
|---|---|---|
| AliZakaee/SmarTel (MIT, Python, pyTelegramBotAPI, SQLite) | Customer-support autoresponder with approvals, knowledge base, owner-takeover pause | Adopt: atomic approval claim and `delivery_uncertain` handling, edit/delete invalidation. Avoid: fail-open rights checks, double transport. |
| sepehr071/tg-secretary (MIT, PTB 22, aiosqlite, Claude Haiku/OpenRouter) | Personal "reply as me" bot with personas, LLM memory (facts, promises, open threads), owner-active cooldowns, inner-circle draft gate | Adopt as references: owner-activity race re-check, burst coalescing, memory taxonomy, consent versioning. Improve: durable jobs, tests, deterministic policy, no auto read receipts. |
| NousResearch hermes-telegram-business (MIT; archived → hermes-official-plugins) | Approval-only drafting plugin | Its bugs (drafting replies to the owner's own messages; non-atomic send) become our explicit test cases. |
| AndyShaman/telegram-business-bridge (MIT, aiogram 3) | Durable full-text archive + MCP tools + approval drafts; documented live test (non-Premium, owner messages arrive, no bot echo, /start required) | Confirms "event log first" design; untrusted-content markers. |
| Mnemora / 22Warm-XD/natursavebot (MIT, aiogram 3) | Summaries, catch-up, to-dos/promises, reminders, digests | Validates demand; includes risky features (saving deleted messages, deleting contacts' messages) we do not adopt. |
| Commercial (Chat Data ~$19/mo; CRM bot builders) | Customer-service autoresponders | Not our segment. |
| Telegram native AI (Cocoon) | Summaries of long *channel* posts/Instant View, AI text editor, translation | Do not compete on rewrite/translate. No native private-chat digest, triage, memory, or commitment tracking. |

**Gap we fill:** no reference combines a durable event log, conversation understanding with per-contact memory, commitment/follow-up tracking that respects the 24 h window, monitoring and digests, a **deterministic auditable policy** for auto-reply vs approval vs notify-only, a strict owner-chat vs business boundary with injection defenses, and a multi-tenant-ready data model. All competitors store plaintext SQLite and keep scheduling state in memory.

---

## 11. Live Validation Protocol

Executed in **M00 (S00.2, Telegram probe)** with the owner's main account, the owner's second and third accounts, and a throwaway probe bot. Raw update JSON stays on the owner's machine (git-ignored). Structural fixtures committed to `tests/fixtures/telegram/` must have **all text, names, and media replaced with synthetic content** (Telegram §4.3 / Content Licensing — no real chat data in test or eval sets).

**Recommended setup.** The limit is one business bot per *account*, so one bot can serve two accounts.

- **Connection A:** probe bot → **owner's main account**, with Chat Automation restricted to the chats with the second and third accounts (`users` = [second account, third account]). The third-account chat is reserved for LV-08 so that no other test reopens its window. Used for LV-09 (first, before any account opens the probe bot), LV-02, LV-03, LV-04, LV-05, LV-07, LV-08 (third-account chat only), LV-10, LV-11, LV-15, LV-16, LV-17.
- **Connection B:** probe bot → **second account** (acting as owner; it must have no real contacts), with scope = existing + new chats, contacts and non-contacts. Used for the LV-01 recipient changes, LV-06, LV-12, LV-13, LV-18 and LV-19, with the main account and a third-party bot as counterparts.
- Run connection B after the connection-A tests, or separate payloads by `business_connection_id`. **Never widen the main account's scope during M00.**
- The probe writes full payloads only for allow-listed chat ids (the test accounts and the bot); for any other chat it logs only the update type, `update_id` and a hash of `chat.id`.
- Do not open or /start the probe bot from any account until LV-09 is recorded.

| ID | Question | Procedure | Pass criterion / what we record |
|---|---|---|---|
| LV-01 | Connection lifecycle | Connect bot; change rights; change recipients (on connection B only; never widen the main account's scope); disconnect; reconnect. | Record each `business_connection` payload; whether `id` changes (feeds LV-05). |
| LV-02 | Owner manual messages | Owner types in the managed chat from phone and desktop. | Arrive as `business_message` with `from.id == owner`, no `sender_business_bot`, no `is_from_offline`. |
| LV-03 | Bot echo | Probe sends via `business_connection_id`. | Does an echo update arrive? With `sender_business_bot`? What `from`? Correlate with the send response `message_id`. |
| LV-04 | Per-chat pause/disable | Pause from action bar; send from contact; probe attempts a send; resume; repeat with "disconnect from this chat". | Any `business_connection` update? Connection id change? Messages delivered while paused? Exact send error. |
| LV-05 | Connection id rotation | From LV-01/LV-04 data. | Whether the Bot API `business_connection_id` changes on settings edits. |
| LV-06 | Mirrored bot chats | On connection B: the second account (owner) writes to the probe bot directly, then to a third-party bot. | Whether owner↔bot DM appears as `business_message` (`chat.id == bot id`); whether the third-party bot chat appears with that bot's id. |
| LV-07 | Deletions | Contact deletes for both; owner deletes for both; owner "delete for me"; owner deletes a bot-sent message; clear chat. | Which produce `deleted_business_messages`; payload shape. |
| LV-08 | 24 h window and error | In the third-account chat only (connection A). At T0 the third account sends one message. At T0+1 min the probe sends message M1 into that chat (record `message_id`); the third account then sends nothing further. At T0+24 h+10 min the probe attempts `editMessageText(M1)`, `sendMessage` and `sendChatAction`, recording exact error strings. Then the owner sends a message in that chat and the probe retries `sendMessage` (does an owner message reopen the window?). | Exact Bot API error strings; window semantics. |
| LV-09 | Owner reachability | Before /start, try to DM the owner at `user_chat_id`. | Whether /start is required. |
| LV-10 | Manage Bot deep link | Owner taps "Manage Bot" in a managed chat. | Exact payload of `/start bizChat<id>`; which id it contains. |
| LV-11 | Media | Contact sends voice note, photo, document, video note; probe calls `getFile` immediately, again after 24 h and after 7 days; probe re-sends a photo into the control chat by file_id. | Download success, expiry behaviour, re-send success. The 7-day result may be recorded after M00 closes. |
| LV-12 | Zero-rights connection | Try to connect with no rights granted (if UI allows). | Whether updates still arrive. |
| LV-13 | Connection confirmation | Observe whether the client asks to confirm the bot connection from another device; what the bot receives before confirmation. | Record behaviour. |
| LV-14 | Access restriction | (Optional) Test whether `BotAccessSettings`/BotFather restriction prevents other accounts from connecting the bot. | Record behaviour; app-level owner filter remains mandatory regardless. |
| LV-15 | Edits | Contact and owner edit messages; edit a pre-connection message. In the LV-08 chat after the window has closed, the contact edits an old message (no new message), then the probe attempts a send. | Payloads; no-op edits; `edit_date` handling; whether a contact edit alone reopens the reply window. |
| LV-16 | Rate behaviour | Send 5 messages in quick succession through the business connection. | Any 429; `retry_after`. |
| LV-17 | Owner edits a bot-sent message | Owner edits, in their client, a message the probe sent. | Does `edited_business_message` keep `sender_business_bot`? (Needed to tell owner corrections from bot edits.) |
| LV-18 | Out-of-scope chat kinds | On connection B: the owner (second account) uses Saved Messages, receives a Telegram service notification (777000), and uses a secret chat. | Expect nothing delivered; record any surprises. |
| LV-19 | Default rights per client | On connection B: connect once from Telegram Desktop with defaults and once from Android with defaults (never on the main account). | Record `rights` in each `business_connection` payload. |

Results are recorded in §12 below and drive the classification rules and error mapping implemented in M02.

---

## 12. Live validation results

*To be filled in during M00 (the LV-11 7-day result may follow after M00 closes). Each entry: date, Telegram client versions, observed payload summary (synthetic), conclusion, and any change required in ARCHITECTURE.md.* Only owner-executed live tests are recorded here (AGENTS.md §2); elsewhere in this document the label *Verified* means checked against primary sources.

| ID | Date | Result | Conclusion / follow-up |
|---|---|---|---|
| **LV-09** Owner reachability | 2026-10-18 | **`/start` is required.** Owner-executed, both arms run. (a) Account that had already opened the bot (tapped the `t.me/<bot>` link from @BotFather, which starts it): `sendMessage` with no `business_connection_id` **succeeded**, `message_id: 3`. (b) Account that had never opened the bot: **failed** with `TelegramBadRequest` — `Bad Request: chat not found`. Client versions not recorded (server-side API behaviour; no client action beyond pressing Start). | **A bot cannot open a conversation.** The error is a **400 `chat not found`, not the widely-reported 403 "bot can't initiate conversation with a user"** — classification must key on the description, not the status (TF §4). ARCHITECTURE §6.8 gains a `chat_unreachable` class: *not* a block, so it must not trigger the §6.3 automatic pause. Onboarding (M01 S01.3) must handle "the owner has not opened the bot yet": the control chat is unusable until the owner presses Start, and any alert raised before that is undeliverable. |
| **LV-16** Rate behaviour | 2026-10-18 | Five `sendMessage` calls through the business connection, back to back: **all five succeeded**, no 429, no `retry_after`. Message ids consecutive (…367…371), all within ~1.3 s (`date` 1792288725–1792288726). | No burst limit observed at this volume. **This does not prove limits are absent** — it proves 5-in-1.3 s is tolerated for one connection and one chat. PQ-03 stays open; keep the designed ~1 msg/s per-chat throttle, which was never exercised here. Re-test at higher volume before autonomy (M06). |
| **LV-03** Bot echo | 2026-10-18 | **No echo.** Six `sendMessage` calls through the business connection (one, then a burst of five; `message_id` 183366–183371) produced **zero** updates. Control: a message sent by the contact immediately afterwards *did* arrive, proving the poller was alive throughout — so the absence is real, not a dead listener. | **A bot does not receive its own business sends back as updates.** No plan change: ARCHITECTURE §6.5 already creates the message record at send time and treats the echo as optional. Keep the `assistant_sent` branch and the 10-minute identity-pending reconciliation anyway — fail-closed (§6 invariant 7), since `sender_business_bot` exists in the API and may be delivered under conditions we did not exercise (offline owner, other clients). **Caveat: this proves absence in this configuration, not in all.** Also noted: business `message_id`s come from the owner's chat numbering (183366, not a low per-connection counter), which matters for id-based dedupe in M02. |
| LV-01, LV-02, LV-04 … LV-08, LV-10 … LV-15, LV-17 … LV-19 | — | Pending | — |

---

## 13. Open platform questions (tracked)

Docs-checked findings that are not yet live-verified are added here as PQ rows marked "docs-checked, not live-verified", with their source URL; only an owner-executed live test moves an item to §12 (AGENTS.md §2).

| ID | Question | Resolution path |
|---|---|---|
| PQ-01 | Bot echo behaviour (LV-03). | Probe. Design handles both outcomes. |
| PQ-02 | Pause/disable observability and error codes (LV-04, LV-08). | Probe. |
| PQ-03 | Do business sends share bot flood limits? | Probe (LV-16) + conservative throttling. |
| PQ-04 | Does Telegram consider per-chat LLM inference on counterparties' messages "deployment of AI" under the Content Licensing terms? Whose "authorization" satisfies §5.4(iv)? | Acceptable risk for personal use; written clarification (@BotSupport or counsel) before public launch. |
| PQ-05 | Uzbek output quality of `gpt-6-luna` (cheapest tier) for summaries and drafts. **Raised in importance by ADR-07a:** there is no cheaper model to fall back to, so this gate now decides whether routes must move up. | Synthetic multilingual eval set (M03/M04), before enabling autonomous replies (M06). |
| PQ-06 | Card payment to the LLM provider (now OpenAI) from Uzbekistan. | Owner test in M00 (S00.3). |
| PQ-07 | New Uzbek Latin alphabet law (oʻ→ö, gʻ→ğ, sh→ş, ch→ç) passed parliament, awaiting signature; effect 6 months after publication. | Search normaliser accepts all three orthographies from day one. |
| PQ-08 | **Telegram does not document that a bot may not open a conversation.** The Bot FAQ covers messaging users "at no cost" and rate limits, but states no such restriction; the behaviour is widely reported by developers and is now live-verified for our case in §12 (LV-09). Docs-checked 2026-10-18, not documented: https://core.telegram.org/bots/faq | Behaviour settled for us by LV-09. Re-check on Bot API upgrades (AGENTS §14); do not assume the published docs back it up. |
| PQ-09 | **OpenAI's data-retention and training terms are not verified or recorded.** The owner switched provider on 2026-10-18 (ADR-07a) and confirms training on API data is disabled in the account, but §7.2 still records the *previous* provider's terms. | Verify against OpenAI's current API data-usage and retention policy, record it in §7.2 with its source URL, and update the consent text and privacy policy to name OpenAI — **all before the first live call carrying message content** (AGENTS §9). Blocks the S00.3 provider spike going live. |
