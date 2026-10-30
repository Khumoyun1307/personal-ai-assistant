# Product Requirements Document — Telegram-Native Personal AI Agent

| | |
|---|---|
| **Status** | Draft v1.0 for owner review (2026-10-17) |
| **Owner** | Project owner (first user) |
| **Related** | [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) (vision & history) · [TELEGRAM_FEASIBILITY.md](TELEGRAM_FEASIBILITY.md) (verified platform facts) · [ARCHITECTURE.md](ARCHITECTURE.md) · [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) |
| **Working name** | "the assistant". The public product/bot name is **Alviro** (owner decision 2026-10-21, Q-01); it **must not contain "Telegram"** (Telegram Bot Developer Terms §8.1). |

**How to read this document.** Requirements describe *what the product must do and how it must behave*, not how it is built. Decision-register items (§5) are tagged; untagged requirements elsewhere are derived from them and are approved together with the register (Q-02):

- **[D]** Established decision carried over from `PROJECT_CONTEXT.md`.
- **[C]** Correction forced by verified platform facts (IDs match TELEGRAM_FEASIBILITY.md §1).
- **[R]** New recommendation in this document — **requires owner approval**.
- **[Q]** Open question (§22).

Requirement IDs (`FR-AREA-nn`, `NFR-nn`) are stable and referenced by the architecture and implementation plan. Priorities: **Must** (release blocked without it), **Should** (expected; may slip one milestone), **Could** (opportunistic).

---

## 1. Purpose and vision

Build a **Telegram-native personal AI agent**: one assistant that lives inside the owner's Telegram experience, understands the parts of their communication it is authorized to see, remembers context, monitors for what matters, tracks commitments, and takes authorized actions — under deterministic, owner-controlled rules.

It is delivered as **one Telegram bot with two complementary surfaces** [D]:

1. **The control chat** — the owner's private chat with the bot. This is where the owner talks to the agent in natural language, receives alerts and digests, approves or rejects proposed actions, reviews activity, and changes settings.
2. **The connected account** — the same bot connected to the owner's personal account through Telegram's Chat Automation ("Secretary Mode") feature. This lets the agent observe the private chats the owner selects and, when explicitly permitted, reply on the owner's behalf within Telegram's limits.

Both surfaces feed **one agent** with one memory, one policy model, and one activity history [D].

**What makes it an agent, not a chatbot:** its value comes from real actions grounded in Telegram data — summarizing what actually happened, noticing unanswered questions and promises, following up, alerting at the right time, drafting replies in context, and handling delegated conversations safely — not from free-form conversation [D].

**What it is not** [D]: a generic AI chat inside Telegram; an away-message autoresponder; a customer-support or sales bot; an external productivity tool with a Telegram plug-in; a collection of disconnected scripts; a multi-agent system for its own sake.

---

## 2. Glossary

| Term | Definition |
|---|---|
| **Owner** | The Telegram user who uses the assistant and has connected it to their account. Initially exactly one person (the developer). |
| **Contact** | Any other person in a private chat the assistant can see. Contacts are *affected parties*, not users: they never interact with the assistant's controls and never authorized it. |
| **Control chat** | The private chat between the owner and the bot. The only place owner-facing controls appear. |
| **Business connection** | Telegram's link between the bot and the owner's account (Chat Automation / Secretary Mode). Has rights (e.g. *reply*) set by the owner. |
| **Managed conversation** | A private chat between the owner and a contact that Telegram delivers to the bot because the owner included it in Chat Automation. |
| **Coverage** | The set of messages the assistant actually received: only managed conversations, only since connection, minus any outage gaps. |
| **Reply window** | Telegram rule: the bot may send/edit as the owner only in a chat whose contact sent a message within the last 24 hours. |
| **Conversation mode** | The owner's per-conversation setting for how the assistant behaves there (§11.1). |
| **Delegation** | A time-bounded or standing instruction authorizing the assistant to handle certain conversations autonomously within explicit limits (§11.3). |
| **Action** | Anything with an external effect or durable consequence: sending/editing a message as the owner, marking read, creating an automation. Every action has a recorded lifecycle. |
| **Draft / proposal** | A reply prepared by the assistant that has **not** been sent. |
| **Approval card** | A control-chat message presenting a proposed action with Approve / Edit / Reject controls. |
| **Escalation** | The assistant handing a situation to the owner instead of acting (alert, card, or digest entry). |
| **Takeover** | The owner personally participating in a managed conversation; the assistant must yield. |
| **Commitment / open loop** | A promise made by the owner or a contact, or a question awaiting an answer, extracted from conversations and tracked as a task. |
| **Monitor** | A standing owner-defined watch condition ("tell me when Daniel replies"). |
| **Digest** | A scheduled or on-demand summary of what happened and what needs attention. |

---

## 3. Users and stakeholders

| Stakeholder | Description | Needs |
|---|---|---|
| **Owner (now)** | The developer, a single Telegram user with Premium, a second test account, and friends available for testing. Communicates mostly in **Uzbek** (Latin and Cyrillic script both in use among contacts), some **Russian**, and **English**. Timezone currently Asia/Tashkent. | Catch up quickly, never miss what matters, reduce reply burden without losing control, trust that nothing embarrassing or duplicated is sent on their behalf. |
| **Contacts** | People who message the owner. | Their messages handled respectfully and privately; not misled about talking to an AI when the assistant replies autonomously; their data not shared with other contacts. |
| **Future individual users** [D] | Telegram users who might use the assistant later (free/freemium/paid undecided). Since Bot API 10.0, **any Telegram user can connect a bot — Premium is not required** [C]. | Same as owner, plus clear onboarding, isolation, and privacy guarantees. |
| **Telegram** | Platform owner with binding developer terms. | Compliance with Bot Developer Terms (§16, TELEGRAM_FEASIBILITY.md §7.1). |

---

## 4. Problems we solve

1. **Catching up is slow.** After focused time (study, work, sleep), reconstructing what happened across many chats takes time; important items hide among routine messages.
2. **Things fall through the cracks.** Unanswered questions, promises ("I'll send the documents tomorrow"), and pending decisions are easy to forget.
3. **Interruptions are costly — and so is missing urgent things.** The owner wants to be interrupted only when something genuinely needs them.
4. **Routine replies consume attention.** Many messages could be answered from known context, or need only a clarifying question.
5. **Existing tools are autoresponders.** They either send canned away messages or auto-reply without understanding, memory, policy, or auditability (TELEGRAM_FEASIBILITY.md §10).
6. **Trust.** Delegating any reply requires confidence about what the assistant can see, what it will say, when it will stop, and a record of what it did.

---

## 5. Decision register

### 5.1 Established decisions [D]

| ID | Decision |
|---|---|
| D-01 | One Telegram bot serves both the control chat and the business-connection role; one backend agent. |
| D-02 | Official Bot API business connection is the integration mechanism. MTProto/user-client integration is **not** used unless separately decided. |
| D-03 | Initial scope: individual users, private chats. Groups deferred. |
| D-04 | Channel intelligence stays in the vision; implementation depends on permitted access and comes later. |
| D-05 | The 24-hour reply restriction is accepted; no workarounds for late replies. |
| D-06 | The language model may propose actions; the application independently decides whether they are allowed and safe. |
| D-07 | Incoming messages from contacts are untrusted content, never instructions. |
| D-08 | Privacy, safety, and reliability are designed in from the start. |
| D-09 | First user is the owner; architecture must allow multi-user later without a rewrite; no premature commercialization work. |
| D-10 | Modular monolith in Python; avoid microservices, multi-agent frameworks, premature vector databases. |
| D-11 | Mini App is optional/future. |
| D-12 | Business model undecided; monetization work starts only when the owner says so. |

### 5.2 Corrections from verified research [C]

| ID | Correction | Product impact |
|---|---|---|
| C-01 | Premium no longer required to connect a bot (Bot API 10.0, May 2026). | Larger future market; no Premium checks. |
| C-02 | Feature names: "Secretary Mode" (BotFather), "Chat Automation" (user settings). | Onboarding copy mentions both old and new paths. |
| C-03 | Reply window is per chat, counts only the contact's incoming messages (per the `can_reply` wording; owner messages → LV-08), and covers sends, edits, and typing. | Late follow-ups to contacts are impossible; follow-ups become reminders/drafts for the owner. Approval cards expire when the window closes. |
| C-04 | The owner's own manual messages in managed chats are visible to the bot (after the fact). | Takeover detection is feasible but only after the owner acts; conflicts can be reduced, not eliminated. |
| C-05/C-06 | No per-chat pause signal; no history API; updates lost after ~24 h of downtime. | The assistant must be honest about coverage and must survive restarts quickly. |
| C-08 | Channels: bots read only channels where they are admins; terms prohibit scraping/aggregation. | Channel features limited to owned/admin channels and owner-forwarded posts (R3). |
| C-09 | Telegram terms: authorization before sending business message contents to third-party APIs; never conceal bot activity; state retention; encrypt at rest; no AI training/benchmarking on Telegram data. | Consent step naming the AI provider; owner-visible activity log; retention policy; synthetic evaluation data only. |
| C-10 | AI provider terms (researched under Anthropic's Usage Policy; kept as a product rule under OpenAI, whose policy is not yet re-verified — TF §7.2) require AI disclosure when an agent communicates directly with people. | Autonomous replies must disclose AI involvement; owner chooses wording/placement only (§11.4). |
| TF §2 | Default connection rights depend on the client (Desktop: view-only). | Observe-only is a first-class configuration; onboarding shows the capability tier. |

### 5.3 Recommendations for approval [R]

| ID | Recommendation | Rationale |
|---|---|---|
| R-01 | **Release structure:** Personal v1 = R0 Foundation + R1 Personal Alpha + R2 Personal Beta (§18). R3 Expansion and R4 Public follow. | Delivers the full core plus early-expansion features as the "initial release", so the product is never just an autoresponder. |
| R-02 | **Resequence milestones:** inbox triage & alerts (M05) **before** delegated autonomy (M06). | Autonomy depends on triage, escalation, and notification machinery; observation/triage is lower risk and broadly valuable. |
| R-03 | **Safe defaults:** new conversations default to *Observe* mode; nothing is sent without approval unless a delegation explicitly allows it; mark-as-read off. | Least privilege; trust-building; ToS predictability. |
| R-04 | **Information isolation:** any reply or draft for a contact may use only that conversation, that contact's memory, owner-designated shareable knowledge, and owner style preferences — never other conversations. | Prevents cross-contact leaks and prompt-injection exfiltration; aligns with Telegram and provider terms. |
| R-05 | **Owner-dictated sends** ("tell Alex I'm late") always go through a one-tap confirmation card in Personal v1 (no opt-out). | Wrong-recipient and wrong-interpretation risk; keeps one policy path (every owner-voice send is either approved text or delegation-authorized). An opt-out can be reconsidered after Personal v1. |
| R-06 | **AI provider:** OpenAI (`gpt-6-luna`, commercial API, no training, responses not stored), behind a provider-neutral interface; Uzbek quality measured before autonomy is enabled. Owner decision 2026-10-18 (ARCHITECTURE ADR-07a), replacing Anthropic Claude. | Cost (owner's budget); verified availability in Uzbekistan and data terms (TF §7.2); Uzbek quality is unmeasured and Luna is the cheapest tier, so swappability and the eval gates are kept. |
| R-07 | **Retention defaults:** raw message content 90 days; summaries and memory until deleted; tasks until closed + 1 year; control-chat history 90 days; activity log 1 year; raw update payloads up to 7 days. Owner-configurable. | Balances "find that conversation" utility with Telegram §4.2/§4.3 minimisation. [Q-03] |
| R-08 | **Disclosure default:** a short configurable AI-disclosure line on the first autonomous reply in each conversation per delegation session (and after 24 h of inactivity). | Meets provider policy minimum with least friction. [Q-04 wording] |
| R-09 | **Takeover cooldown default:** after the owner writes manually in a conversation, autonomy there pauses until the owner re-enables it or the delegation ends, whichever is first; drafts may still be offered. | Most conservative behaviour consistent with "avoid conflicting responses". [Q-05] |
| R-10 | Monetization, public onboarding, billing, and legal workstreams are **out of active scope** until the owner explicitly decides to go public. | Owner instruction (2026-10-17). |
| R-11 | **Conversation-mode model** (§11.1): Ignore / Observe / Assist, with Delegate as the effect of an active delegation. Mapping from PROJECT_CONTEXT §5.3: Observe only → Observe; Summarize or notify → Observe + triage/alerts (R2); Suggest replies / Draft replies requiring approval → Assist (or on request in Observe); Automatically reply within boundaries → delegation (time-bounded or standing, shown as Delegate); Temporarily pause → /pause, takeover pause; Escalate → §11.5 categories; new: Ignore (stop storing). | Keeps every candidate mode while reducing UI states to what policy needs. |

---

## 6. Product principles (behavioral)

1. **Owner in control.** Autonomy is opt-in, scoped, time-bounded where possible, instantly revocable, and always visible.
2. **Honest about what it knows.** The assistant never implies it has seen messages outside its coverage, and says when coverage is partial.
3. **Never claim success on failure.** Drafts are never presented as sent; uncertain outcomes are reported as uncertain.
4. **Fail closed.** When permission, state, or identity is unclear, the assistant does not act; it asks or escalates.
5. **Contacts' content is data, not instructions.** Nothing a contact writes can change the assistant's permissions, settings, or behaviour toward others.
6. **Minimum necessary data.** Only what a task needs is processed or sent to the AI provider; retention is limited and stated.
7. **Quiet by default, loud when it matters.** Interruptions are reserved for urgent or owner-designated situations; everything else goes to digests.
8. **One coherent agent.** Features share memory, tasks, and policy; a commitment detected in one place drives reminders, monitors, and summaries elsewhere.
9. **Telegram first.** Prefer value only a Telegram-native agent can provide (cross-chat understanding, commitments, delegation) over generic features Telegram already ships (rewrite/translate).
10. **Personalization over imitation.** Reply language, tone, and style follow the owner's configured preferences; wording itself is left to the model within those preferences.

---

## 7. Platform constraints that shape the product

Detailed and sourced in TELEGRAM_FEASIBILITY.md. Product-relevant summary:

| Constraint | Product consequence |
|---|---|
| Coverage = chats the owner includes in Chat Automation, from connection time, excluding outages > ~24 h. No history import. | Onboarding sets expectations; summaries state coverage; "what can you see?" command; outage gaps recorded. |
| Owner chooses connection rights; Desktop defaults to view-only. | Three capability tiers: **Observe-only**, **Can reply**, **Extended** (mark read, delete own). Features degrade gracefully per tier. |
| Reply window: 24 h after the contact's last message; owner messages are not expected to reopen it (LV-08). | The assistant cannot start conversations or send late follow-ups as the owner. Drafts show "sendable until …"; expired drafts become copy-ready text. |
| Bot cannot see the owner typing/reading, or tell a paused chat from a quiet one. | Takeover is detected after the owner's message; send delays and pre-send re-checks reduce conflicts. Send failures may mean "paused by owner". |
| Owner controls (approve/edit/reject) cannot be placed in contacts' chats. | All controls live in the control chat. |
| Bot cannot forward/copy/react on the owner's behalf; no streaming into managed chats. | Content from managed chats is quoted/re-rendered in the control chat. |
| One business bot per account. | Users of this assistant cannot simultaneously use another secretary bot. |
| Bot Developer Terms §5.4, §4.2–4.4. | Consent naming processors; owner-visible activity log; retention; encryption; deletion; no secondary use. |
| AI disclosure duty (provider policy). | Disclosure on autonomous replies (§11.4). |

---

## 8. Capability map and release plan

| Capability area | R0 Foundation (M00–M02) | R1 Personal Alpha (M03–M04) | R2 Personal Beta (M05–M07) | R3 Expansion (M08) | R4 Public (M09, on go-ahead) |
|---|---|---|---|---|---|
| Control-chat assistant | NL conversation, commands, onboarding, consent | Tool-backed answers about communications; /settings panel | Full automation management | Mini App (option) | Multi-user onboarding |
| Account integration | Connection lifecycle, ingestion, classification, coverage | Per-conversation modes, bizChat panel | — | — | Webhook scale-out |
| Summaries & search | Visibility report | Catch-up, per-chat summary, search, Q&A, attention v1 | Digests, attention v2 with triage | Semantic retrieval (if needed) | — |
| Memory & personalization | Owner settings (language, tz) | Owner instructions, contact notes, style prefs, summaries as memory | Agent-inferred memory with review, shareable knowledge | Consolidated contact profiles | Per-tenant isolation review |
| Replies | — | Drafts + approval cards + verified sending + activity log | Delegated autonomy with disclosure & takeover | Undo, rich formatting, owner-approved scheduled sends | — |
| Inbox intelligence | — | — | Triage, alerts, quiet hours, commitments/open loops, monitors | — | — |
| Automation | — | — | Reminders, conditional follow-ups, scheduled summaries, NL automation management | Advanced workflows | Quotas |
| Channels | — | — | — | Admin channels, forwarded posts | — |
| Media | Metadata only | Captions/text | — | Voice transcription, images | — |
| Privacy & ops | Privacy policy, consent, encryption at rest, logging hygiene, backups | Retention controls, deletion | Export | — | Legal review, ToS, billing (Stars) |

---

## 9. User journeys

Each journey lists the trigger, the expected experience, constraints, and key edge cases. Journeys are the basis for scenario tests (IMPLEMENTATION_PLAN.md).

### J1 — Onboarding and connecting the account (R0)

1. Owner opens the bot and sends **/start**.
2. The assistant explains in the owner's language what it does, what it can and cannot see, where data goes (named AI provider), and how to stop it. Links the privacy policy (**/privacy**).
3. Owner grants **consent** for AI processing of the owner's own control-chat messages (required to talk to the assistant), sets **timezone** and **preferred language**.
4. The assistant guides the owner to **Settings → Chat Automation** (or Settings → Telegram Business → Chatbots on older clients), recommends which chats to include, and recommends rights: *Reply to Messages* only if the owner wants sending; never profile/gift/Stars/story rights.
5. When the connection arrives, the assistant reports the **capability tier** and asks for a separate, explicit **consent for AI processing of managed conversations** (versioned, names the provider). Until granted, managed messages are stored in the owner's own encrypted database (so nothing is lost while the owner decides) but are **never sent to any AI provider**; if the owner declines, the assistant offers to delete what was stored and to keep running in a non-AI mode (coverage and visibility only) or to be disconnected.
6. The assistant confirms: "I can see chats you included, from now on. I can't see older history."

Edge cases: connection from a non-owner account (ignored; no content retained after processing); connection arrives before /start (kept inert until onboarding completes); connection event missing (bootstrapped from the first managed message); owner revokes consent (processing stops immediately, owner told what remains stored and how to delete it).

### J2 — Asking the assistant (R0 → R1)

"What did Kamola ask me yesterday?" / "Which conversations need my attention?" / "Summarize what happened while I was studying."

The assistant interprets the request, retrieves the relevant retained context, answers concisely with references (who, which chat, when), and states coverage limits when relevant. If the request is ambiguous in a way that changes the answer (e.g., two contacts named Alex), it asks one clarifying question; otherwise it proceeds.

### J3 — Catch-up after focused time (R1, enhanced in R2)

Owner: "I was studying from 2 to 7. What happened?" → a structured catch-up: conversations with new activity, grouped by importance; open questions directed at the owner; commitments made by others; decisions requested; anything the assistant sent or drafted; coverage caveats (e.g., "I was down for 26 h until 3 Oct 16:00 — messages sent 2 Oct 14:00–16:10 may be missing"; short outages only delay processing and are not reported as gaps). Each item offers next actions (draft reply, remind me, mark done).

### J4 — Draft and approve (R1)

Owner: "Draft a response to Alex, but don't send it until I approve."

1. The assistant drafts using that conversation, Alex's notes, and the owner's style preferences.
2. An **approval card** shows: the contact, the message(s) being answered, the draft, "sendable until 18:42" (window), and buttons **Approve · Edit · Reject**.
3. **Edit:** the owner replies with corrected text or an instruction ("shorter, more formal"); the card updates.
4. **Approve:** the application re-verifies connection, rights, window, takeover, staleness, and duplicate state, then sends. The card updates to **Sent ✓ 18:05** or **Failed — reason**, or **Delivery uncertain — check the chat**.
5. If Alex writes again or the owner replies manually before approval, the card becomes **Outdated** with a one-tap "Re-draft".

Edge cases: window closes before approval (card offers copy-ready text); rights lack *reply* (card offers copy-ready text and explains how to enable); double-tap on Approve (sent once); the sent message is deleted in the chat (recorded as a deletion with a note and shown in the activity log, summaries and reports; Telegram does not say who deleted it, TF §12 LV-07 — owner decision 2026-10-26, replacing "recorded as owner rejection").

### J5 — "I'm studying until 7 PM" delegation (R2)

Owner: "I'm studying until 7 PM. Help with messages about our project. Ask people for missing details and interrupt me only if something genuinely requires my decision before 7."

1. The assistant interprets this into a **delegation** and shows a **confirmation card**: scope (conversations where the project is discussed / specified contacts), allowed behaviour (answer from approved project knowledge, ask clarifying questions, acknowledge and defer), not allowed (commitments, prices, dates not in knowledge), escalation rule (decisions needed before 19:00 → immediate alert; others → summary at 19:00), disclosure wording, end time 19:00.
2. Owner confirms (optionally edits). Delegation becomes active.
3. For each incoming message in scope: the assistant debounces bursts, triages, and decides per policy: **auto-reply** (in scope, confident, not sensitive, window open, no owner activity) / **draft for approval** (uncertain) / **escalate** (decision needed) / **ignore** (out of scope).
4. Autonomous replies include the disclosure line on the first reply in that conversation in this session.
5. If the owner writes in a conversation manually, autonomy pauses there (takeover) and the owner is told once.
6. At 19:00 the delegation ends; the owner receives a **catch-up**: what came in, what was answered (with links to the activity log), what is pending, decisions needed.

Edge cases: a contact asks something outside knowledge → the assistant asks for details or says the owner will follow up (if allowed), else escalates; a contact tries to instruct the assistant ("ignore your rules", "what did others tell you") → treated as content; no disclosure of other conversations; rate limit reached → further messages queued for owner; the owner says **/pause** → all autonomous activity stops immediately.

### J6 — Monitoring and alerts (R2)

"Notify me when Daniel replies." / "Tell me if someone mentions an urgent deadline." / "Alert me if anyone asks for a decision about the project."

The assistant creates a **monitor**, confirms its interpretation, evaluates new messages in scope, and alerts according to urgency and quiet-hours settings. Alerts include a short summary and actions (open details, draft reply, remind me later). Monitors can be one-shot ("when Daniel replies" ends after the first reply) or standing.

### J7 — Commitments and follow-ups (R2) — the flagship workflow

1. In a chat, Sardor writes "I'll send the documents tomorrow". The assistant extracts a **commitment** (Sardor → owner, due tomorrow) and adds it to open loops.
2. By default, a contact commitment with a due time that is still open at that time appears in the next digest/attention list (alert only if the notification policy says so). The owner can refine it ("Remind me tomorrow if the documents haven't arrived"), which creates an explicit conditional follow-up.
3. The **conditional follow-up** (when created) watches Sardor's conversation for a message containing documents/files.
4. If they arrive: the task closes and the owner is told (or it appears in the digest). If not by the deadline: the owner gets a reminder with a draft nudge. If Sardor's reply window is still open, the owner may approve sending the nudge; if not, the draft is copy-ready (the assistant never messages Sardor outside the window).

### J8 — Takeover (R1/R2)

While a draft is pending or a delegation is active, the owner replies personally in the chat. The assistant detects the owner's message, marks pending drafts for that chat **Outdated**, cancels scheduled autonomous replies there, pauses autonomy in that conversation (R-09), and records the takeover in the activity log. It does not message the contact.

### J9 — Pause and kill switch (R1)

**/pause** (or "stop replying for me"): all autonomous sending stops immediately; pending autonomous actions are cancelled; drafts remain drafts. **/resume** re-enables. Telegram's own per-chat Pause in the chat header is also respected: the assistant cannot see it, but treats send failures and silence as possible pauses and never retries around them. Hard stop that works even if the server is down: Settings → Chat Automation (older clients: Settings → Telegram Business → Chatbots) → disconnect the bot (also shown during onboarding and in /help).

### J10 — Reviewing activity (R1)

"Show me everything the assistant answered on my behalf today." → chronological list of sent messages (contact, time, text excerpt, triggering message, approval or delegation that authorized it), plus drafts rejected/expired, escalations, blocked actions and why. **/activity** gives the same view.

### J11 — Memory and personalization (R1 → R2)

"Remember that I prefer short, polite replies in Uzbek to colleagues." / "Remember Alex is my project partner and handles design." / "What do you know about Kamola?" / "Forget that."

Owner instructions and contact notes are stored as explicit memory and echoed back with a one-tap Undo; the owner can list, correct, and delete them. From R2, the assistant may propose inferred memories (e.g., "Alex usually sends drafts on Fridays") which require owner review before being used in replies.

### J12 — Per-conversation management via "Manage Bot" (R1)

In a managed chat, the owner taps **Manage Bot** in Telegram's action bar → the control chat opens a **conversation panel**: summary, open loops, current mode (Ignore / Observe / Assist / Delegate), priority, notes, and buttons to change them. Delegate is shown when an active delegation covers the chat; choosing it opens the delegation confirmation card.

### J13 — Channel digest (R3, constrained)

"Summarize important updates from the channels I'm monitoring." Only channels where the owner added the bot as admin and posts the owner forwarded. The assistant explains this limitation when asked about other channels.

### 9.1 Traceability of the illustrative commands

| Illustrative command (PROJECT_CONTEXT §4B) | Journey | Requirements | Release |
|---|---|---|---|
| "Summarize what happened in my messages while I was studying." | J3 | FR-SUM-01, FR-SUM-06 | R1 |
| "Tell me which conversations need my attention." | J2/J3 | FR-SUM-03, FR-TRI-02 | R1 (basic) / R2 |
| "For the next four hours, help with messages from selected contacts." | J5 | FR-AUT-01..12 | R2 |
| "If someone asks about the project, answer questions you know and ask for any missing details." | J5 | FR-AUT-01, FR-MEM-08 | R2 |
| "Notify me when Daniel replies." | J6 | FR-TRI-06 | R2 |
| "Summarize important updates from the channels I'm monitoring." | J13 | FR-CHN-01..03 | R3 |
| "Find the conversation where we agreed on the meeting time." | J2 | FR-SUM-04, FR-SUM-05 | R1 |
| "Draft a response to Alex, but don't send it until I approve." | J4 | FR-DRF-01..07 | R1 |
| "Show me everything the assistant answered on my behalf today." | J10 | FR-ACT-01 | R1 |
| "Remind me tomorrow if the documents haven't arrived." | J7 | FR-FUP-02, FR-FUP-07 | R2 |

---

## 10. Functional requirements

### 10.1 Onboarding, connection, consent (ONB)

| ID | Requirement | Priority | Release |
|---|---|---|---|
| FR-ONB-01 | /start onboarding explains capabilities, visibility limits, data flow (named AI provider), and controls; collects timezone and preferred language. | Must | R0 |
| FR-ONB-02 | Only allowlisted owner accounts can use the assistant. Others receive a fixed short reply; their messages are not sent to the AI provider and not retained beyond operational minimum. | Must | R0 |
| FR-ONB-03 | Detect business connections; accept only those whose account is an allowlisted owner who has completed /start; ignore others without retaining message content after processing. | Must | R0 |
| FR-ONB-04 | Show the capability tier from the actual rights granted (Observe-only / Can reply / Extended) and explain what each enables. Recommend minimal rights. | Must | R0 |
| FR-ONB-05 | Separate, explicit, versioned consents: (a) AI processing of control-chat content; (b) AI processing of managed-conversation content, naming every external processor. No managed-conversation content reaches any AI provider before (b). | Must | R0 |
| FR-ONB-06 | Consent revocation takes effect immediately for all future processing; the assistant states what remains stored and offers deletion. | Must | R0 |
| FR-ONB-07 | Notify the owner of connection changes: connected, rights changed, disabled/disconnected, reconnected. | Must | R0 |
| FR-ONB-08 | **Visibility report** ("What can you see?" / /status): connection state, rights, connected-since, number of managed conversations observed, known observation gaps, consent state, autonomy state. | Must | R0 |
| FR-ONB-09 | Handle the "Manage Bot" deep link: open the conversation panel for that chat (J12). | Should | R0 (basic) / R1 |
| FR-ONB-10 | Onboarding copy must not hard-code a single Telegram UI path (old and new paths differ by client). | Must | R0 |

### 10.2 Control-chat assistant (CTL)

| ID | Requirement | Priority | Release |
|---|---|---|---|
| FR-CTL-01 | Natural-language conversation with short-term context (recent turns) and owner settings (R0); long-term memory (FR-MEM-*) used in control-chat answers (R1). | Must | R0 / R1 |
| FR-CTL-02 | Answer questions and perform tasks using tools over retained data and settings (search, summaries, tasks, automations, memory, activity). | Must | R1 |
| FR-CTL-03 | Ask a clarifying question only when ambiguity materially changes the outcome; otherwise act and state assumptions. | Should | R1 |
| FR-CTL-04 | Show progress for operations longer than ~2 s (typing indicator or "Thinking…" placeholder). | Should | R0 |
| FR-CTL-05 | Commands: /start, /help, /status, /pause, /resume, /settings, /privacy, /activity, /export (R2). Command visibility is cosmetic; authorization is enforced server-side. | Must | R0–R2 |
| FR-CTL-06 | Every response about actions distinguishes **done**, **proposed (awaiting approval)**, **blocked (why)**, **failed (why)**, **uncertain**. | Must | R1 |
| FR-CTL-07 | Replies to the owner use the owner's preferred language (configurable; Uzbek, Russian, English supported). | Must | R0 |
| FR-CTL-08 | Long outputs are formatted for Telegram and split cleanly; no broken formatting. | Must | R0 |
| FR-CTL-09 | If the AI provider is unavailable, the assistant says so plainly and retries background work later; owner commands like /pause work without the AI provider. | Must | R0 |
| FR-CTL-10 | `/settings` opens a deterministic inline-keyboard settings panel that works without the AI provider. Each slice that introduces a setting adds it to the panel. Changes that increase autonomy or remove a confirmation need a confirm step; all changes are audited. | Must | R1 |

### 10.3 Observation and ingestion (OBS)

| ID | Requirement | Priority | Release |
|---|---|---|---|
| FR-OBS-01 | Persist every delivered managed-conversation message, edit, and deletion durably before acknowledging it; duplicates have no effect. | Must | R0 |
| FR-OBS-02 | Classify each message: from contact, owner (manual), owner (automated away/greeting/scheduled), sent by the assistant. | Must | R0 |
| FR-OBS-03 | Edits update the stored message and keep an edited marker. Deletions purge stored content (tombstone kept) and invalidate dependent drafts. | Must | R0 |
| FR-OBS-04 | Ignore by default: the owner's chat with the bot mirrored as a business chat, and chats with other bots. | Must | R0 |
| FR-OBS-05 | Record coverage: connected-since per conversation and observation gaps (ingestion outages). | Must | R0 |
| FR-OBS-06 | Track each conversation's reply-window state from the last contact message. | Must | R0 |
| FR-OBS-07 | Media messages: store type, size, caption; media content processing is out of scope until R3. | Must | R0 |
| FR-OBS-08 | Conversations in *Ignore* mode: content is not retained or processed going forward; existing content purged on request. | Must | R1 |

### 10.4 Summaries, search, and Q&A (SUM)

| ID | Requirement | Priority | Release |
|---|---|---|---|
| FR-SUM-01 | Catch-up summary for a time range or since last check: activity grouped by importance, open questions to the owner, commitments, decisions requested, assistant actions, coverage caveats. | Must | R1 |
| FR-SUM-02 | Per-conversation summary on demand ("what's going on with Alex?"). | Must | R1 |
| FR-SUM-03 | Attention list: conversations where the last message is from the contact and appears to need a response (R1 heuristic + model), with reasons; upgraded by triage in R2. | Must | R1 |
| FR-SUM-04 | Search retained messages across Uzbek (Latin and Cyrillic, including the new alphabet letters), Russian, and English; tolerant of spelling/apostrophe variants. | Must | R1 |
| FR-SUM-05 | Answer questions about retained context with references (contact, date, quote) so the owner can verify. | Must | R1 |
| FR-SUM-06 | Every summary states limitations when relevant (outside coverage, possible/definite gaps, ignored or paused chats, pre-connection periods). | Must | R1 |
| FR-SUM-07 | Scheduled digests (default daily at an owner-chosen time; configurable). | Must | R2 |
| FR-SUM-08 | Summaries are faithful: no invented facts; uncertainty marked. Verified by evaluation (NFR-12). | Must | R1 |

### 10.5 Memory and personalization (MEM)

| ID | Requirement | Priority | Release |
|---|---|---|---|
| FR-MEM-01 | Owner instructions ("remember that …") stored and echoed back with a one-tap Undo; listable, editable, deletable. | Must | R1 |
| FR-MEM-02 | Personalization settings: preferred language, default reply language/tone/style and per-contact overrides, notification preferences, quiet hours, timezone, digest time. | Must | R1 |
| FR-MEM-03 | Contact notes provided by the owner (role, relationship, preferences). | Must | R1 |
| FR-MEM-04 | Rolling conversation summaries serve as compact long-term memory beyond raw-message retention. | Must | R1 |
| FR-MEM-05 | Agent-inferred memory is stored with provenance (source messages), marked *inferred*, and **requires owner review** before use in replies. Sensitive categories (health, religion, politics, sexuality, finances, etc.) are not inferred. | Should | R2 |
| FR-MEM-06 | Contact-scoped memory and conversation content are never used in replies to other contacts. | Must | R1 |
| FR-MEM-07 | "What do you know about X?" shows everything stored about a contact with sources; owner can delete any item. | Must | R1 |
| FR-MEM-08 | **Shareable knowledge:** owner-designated facts the assistant may share with specified contacts or within a delegation (e.g., project facts). | Must | R2 |
| FR-MEM-09 | No emotion inference, personality profiling, or trustworthiness scoring of contacts. | Must | All |

### 10.6 Drafts, approvals, and sending (DRF)

| ID | Requirement | Priority | Release |
|---|---|---|---|
| FR-DRF-01 | Draft on owner request for a named conversation. | Must | R1 |
| FR-DRF-02 | In *Assist* mode, proactively offer drafts for contact messages that need a reply (debounced; one card per conversation burst). | Must | R1 |
| FR-DRF-03 | Approval card with contact, triggering message(s), draft, sendable-until time, Approve / Edit / Reject; state updates in place. | Must | R1 |
| FR-DRF-04 | Edit by sending corrected text or a revision instruction. | Must | R1 |
| FR-DRF-05 | Drafts become **Outdated** when a newer contact message arrives, the owner writes manually, the source message is edited/deleted, or the connection/rights change. They become **Expired** when the reply window closes or the maximum card age passes. | Must | R1 |
| FR-DRF-06 | If sending is impossible (no reply right, window closed, chat unavailable), the card explains why and provides copy-ready text. | Must | R1 |
| FR-DRF-07 | At approval time the application re-verifies all send conditions (§12.2) and reports the actual outcome: Sent / Failed (reason) / Delivery uncertain. | Must | R1 |
| FR-DRF-08 | Owner-dictated messages ("tell Alex I'll be late") always produce a one-tap confirmation card showing the resolved recipient and the exact text (R-05). | Must | R1 |
| FR-DRF-09 | Each approved draft is sent at most once, including under double taps, retries, and restarts. | Must | R1 |
| FR-DRF-10 | Optional "Undo" for a just-sent assistant message (requires delete-own right; short window). | Could | R3 |
| FR-DRF-11 | Approve a message for later sending ("send this to Alex at 9:00"). At the scheduled time all §12.2 conditions are re-verified; if the window has closed or any check fails, nothing is sent and the owner gets copy-ready text. The card shows the scheduled time and "sendable until". (PROJECT_CONTEXT §5.6 scheduled actions; bots cannot schedule natively — TF §4.) | Could | R3 |

### 10.7 Delegated autonomous communication (AUT)

| ID | Requirement | Priority | Release |
|---|---|---|---|
| FR-AUT-01 | Create a delegation from natural language: scope (contacts/conversations/topic), start/end, allowed behaviours, shareable knowledge, escalation rules, disclosure wording. Show a confirmation card of the interpreted rules; activate only on confirmation. | Must | R2 |
| FR-AUT-02 | Standing per-conversation delegation (no end time, scope = one conversation), created and changed through the same confirmation card as FR-AUT-01. | Should | R2 |
| FR-AUT-03 | An autonomous reply is sent only when **all** policy conditions pass (§12.3); otherwise it becomes a draft for approval or an escalation. | Must | R2 |
| FR-AUT-04 | Autonomous replies carry an AI disclosure at least on the first autonomous reply per conversation per delegation session and after 24 h of inactivity; wording configurable; disclosure cannot be disabled while autonomy is on. | Must | R2 |
| FR-AUT-05 | Takeover: an owner manual message in a conversation cancels pending autonomous replies there and pauses autonomy per R-09; the owner is informed once. | Must | R2 |
| FR-AUT-06 | Global kill switch (/pause, "stop") halts autonomous sending within seconds, cancels pending autonomous actions, and is acknowledged. | Must | R1 |
| FR-AUT-07 | Escalate decisions, unknowns, commitments not covered by knowledge, sensitive topics, and conflicts; urgency determines alert vs end-of-session summary. | Must | R2 |
| FR-AUT-08 | Volume limits: maximum autonomous replies per conversation per hour and per delegation; never reply to bots; loop detection. | Must | R2 |
| FR-AUT-09 | End-of-delegation catch-up summary. | Must | R2 |
| FR-AUT-10 | Never initiate a conversation; never send outside the reply window; never send without approval to a conversation that no active delegation covers (§11.1); never send to a conversation in Ignore mode. | Must | All |
| FR-AUT-11 | Debounce bursts and wait a short configurable delay (default 45 s; ARCHITECTURE §15.2) before autonomous sends; re-check all conditions immediately before sending. | Must | R2 |
| FR-AUT-12 | Never claim to be human; if a contact sincerely asks whether they are talking to a person, answer truthfully and offer to involve the owner. | Must | R2 |
| FR-AUT-13 | Autonomy for Uzbek-language conversations is enabled only after the reply-quality evaluation passes (NFR-12). | Must | R2 |

### 10.8 Inbox intelligence, alerts, and monitors (TRI)

| ID | Requirement | Priority | Release |
|---|---|---|---|
| FR-TRI-01 | Triage new contact messages (batched per conversation burst): needs reply, contains question, requests decision, urgency, deadlines. | Must | R2 |
| FR-TRI-02 | Attention list with reasons and suggested next actions. | Must | R2 |
| FR-TRI-03 | Notification policy: immediate alert vs digest vs silent based on urgency, contact priority, quiet hours, active delegation, and owner preferences. | Must | R2 |
| FR-TRI-04 | Contact priority levels (e.g., VIP / normal / low) influencing alerts. | Should | R2 |
| FR-TRI-05 | Extract commitments and open loops (owner promises, contact promises, unanswered questions to/from owner) into tasks with source links. Deduplicated against open tasks; open questions and commitments close automatically when either party answers or fulfils them (with owner notification). | Must | R2 |
| FR-TRI-06 | Monitors from natural language (contact replies, keywords/topics, decision requests), one-shot or standing, confirmed before activation. | Must | R2 |
| FR-TRI-07 | Noise control: deduplicate alerts, batch related alerts, cap alerts per hour, accept feedback ("fewer alerts like this"). | Should | R2 |

### 10.9 Follow-ups, reminders, and automation (FUP)

| ID | Requirement | Priority | Release |
|---|---|---|---|
| FR-FUP-01 | Owner reminders (one-off and recurring) delivered in the control chat at the right local time. | Must | R2 |
| FR-FUP-02 | Conditional follow-ups: "if X hasn't happened by T, remind me" evaluated against observed messages. | Must | R2 |
| FR-FUP-03 | Tasks linked to conversations: list, complete, snooze, cancel; auto-close when the expected reply/event is observed (with owner notification). | Must | R2 |
| FR-FUP-04 | Scheduled summaries with owner-defined schedules. | Must | R2 |
| FR-FUP-05 | Automation management in natural language: list, explain, modify, delete; confirmation before activation or change. | Must | R2 |
| FR-FUP-06 | Automations may notify, summarize, create tasks, and draft. They may send as the owner only through an active delegation and only within the reply window. | Must | R2 |
| FR-FUP-07 | The flagship workflow (J7) works end-to-end. | Must | R2 |
| FR-FUP-08 | Scheduled work survives restarts and fires late (with a note) rather than never; obsolete occurrences are skipped with a note. | Must | R2 |

### 10.10 Activity and audit (ACT)

| ID | Requirement | Priority | Release |
|---|---|---|---|
| FR-ACT-01 | Every action and decision is recorded: sent, drafted, approved, edited, rejected, expired, escalated, blocked (with reason), failed, uncertain, read-marked, settings and automation changes. Viewable by NL query and /activity. | Must | R1 |
| FR-ACT-02 | Drafts and sent messages are always visually distinct. | Must | R1 |
| FR-ACT-03 | The assistant can explain why it sent, drafted, escalated, or blocked something (policy reasons in plain language). | Should | R2 |
| FR-ACT-04 | No assistant activity is hidden from the owner (Telegram §5.4(v)). | Must | All |

### 10.11 Privacy and data controls (PRV)

| ID | Requirement | Priority | Release |
|---|---|---|---|
| FR-PRV-01 | Custom privacy policy, versioned, registered with @BotFather, served by /privacy; states data classes, purposes, retention, processors, rights. | Must | R0 (before managed content reaches the AI provider) |
| FR-PRV-02 | Consent records with timestamps, versions, processor lists; revocable. | Must | R0 |
| FR-PRV-03 | Retention per data class with defaults (R-07) and automatic purge. | Must | R1 (basic purge in R0) |
| FR-PRV-04 | Delete data per conversation, per contact, or entirely ("delete everything"), with confirmation; deletion cascades to derived data (summaries, memory, tasks). | Must | R1 |
| FR-PRV-05 | Export the owner's data in a readable format. | Should | R2 |
| FR-PRV-06 | Owner data is never used to train, fine-tune, evaluate, or benchmark AI models; provider feedback/data-sharing programs are disabled. | Must | All |
| FR-PRV-07 | Only the minimum necessary context is sent to the AI provider per task; contact isolation per FR-MEM-06. | Must | All |

### 10.12 Channels (CHN) — R3

| ID | Requirement | Priority | Release |
|---|---|---|---|
| FR-CHN-01 | Monitor channels where the owner added the bot as administrator; summarize and alert on owner-defined topics. | Should | R3 |
| FR-CHN-02 | Accept channel posts the owner forwards; include them in digests and Q&A with source links. | Should | R3 |
| FR-CHN-03 | When asked about other channels, explain the limitation; never scrape. | Must | R3 |

---

## 11. Agent behaviour and autonomy model

### 11.1 Conversation modes [R-11]

| Mode | Stored & used for summaries/search | Triage & alerts | Drafts | Sends as owner |
|---|---|---|---|---|
| **Ignore** | No (going forward) | No | No | No |
| **Observe** (default, R-03) | Yes | Yes (R2) | On request only | Only via approved card |
| **Assist** | Yes | Yes | Proactive for messages needing reply | Only via approved card |
| **Delegate** | Yes | Yes | When not confident | Autonomous within delegation rules; otherwise approval |

Delegate is not a stored mode. It is shown for a conversation covered by an active time-bounded or standing delegation, and is entered only through the delegation confirmation card (FR-AUT-01).

Modes are set per conversation (and a default for new conversations). A **delegation** (time-bounded or standing) elevates the conversations it covers to Delegate behaviour while it is active. Telegram's own scope (which chats are included at all) is an outer boundary the assistant cannot change or read.

### 11.2 Action authority matrix

| Action | Who can trigger | Requires approval? |
|---|---|---|
| Answer the owner in the control chat | Owner request | No |
| Read/search/summarize retained data | Owner request; scheduled jobs | No |
| Create/modify reminders, monitors, delegations, memory | Owner request | Confirmation card for delegations, monitors, automations, and inferred memory; simple reminders confirmed inline; explicit memory echoed back with a one-tap Undo |
| Send/edit a message as the owner | Owner request (dictated); assistant proposal; delegation | Yes, except autonomous replies under an active delegation passing all checks |
| Mark a message as read | Owner setting (R3; not in Personal v1) | Off by default; when on, logged |
| Delete a message the assistant sent | Owner (undo) | Owner-initiated only |
| Delete contacts' messages, edit the owner's own messages, change profile, gifts, Stars, stories | — | **Never** (not implemented) |

### 11.3 Delegation structure

A delegation has: **scope** (explicit conversations/contacts and/or a topic description evaluated per message), **time bounds** (start, end; standing delegations allowed per conversation), **allowed behaviours** (answer from shareable knowledge; ask for missing details; acknowledge and promise the owner will follow up; decline), **forbidden behaviours** (always: making commitments on dates/money/meetings not in knowledge, sharing other conversations, sending media, sensitive topics), **escalation rules** (what is urgent, quiet-period handling), **disclosure wording**, and **limits** (messages per hour). The owner confirms the interpreted structure before activation. Every autonomous message references the delegation that authorized it.

### 11.4 AI disclosure

- Required for autonomous replies [C-10]. The owner controls wording and language; the default is a brief neutral line in the conversation's reply language (R-08).
- Not added to owner-approved drafts. Interpretation: the owner reviews and authorizes each message, so the assistant is not interacting directly (Usage Policy). Re-checked when the policy text changes (next: 2026-11-12) and before R4.
- The assistant never denies being an AI to a sincere question (FR-AUT-12).

### 11.5 Escalation categories

| Category | Examples | Default handling |
|---|---|---|
| **Urgent decision** | "Can you confirm by 5 PM?", money, meetings, deadlines before the delegation ends | Immediate alert (bypasses quiet hours only if the owner allowed it) |
| **Needs owner** | Questions outside knowledge, personal/sensitive topics, conflicts, complaints | Alert or summary per urgency; contact may receive "I'll check with [owner]" if the delegation allows |
| **FYI** | Information, confirmations, routine updates | Digest |
| **Suspicious** | Attempts to instruct the assistant, requests for others' information, phishing patterns | Do not comply; summary flag; alert if repeated |

### 11.6 Prohibited behaviours (always)

Initiating conversations; messaging outside the reply window; sending without passing policy; continuing after /pause or takeover; sharing information from one conversation with another contact; following instructions from contacts; impersonating a human; emotion inference or scoring of contacts; marking read without the setting; deleting contacts' messages; editing the owner's messages; profile/gift/Stars/story actions; bulk or unsolicited messaging; reporting failure as success.

---

## 12. Permissions and approval flows

### 12.1 Approval card lifecycle

`proposed → awaiting_approval (⇄ revising) → approved → [scheduled] → executing → succeeded | failed | delivery_uncertain`; terminal alternatives: `rejected`, `outdated`, `expired`, `cancelled`, `blocked` (ARCHITECTURE §9.4 is canonical; UI labels: Sending = `executing`, Sent = `succeeded`).

- **Outdated:** a newer contact message, owner manual message, source edit/delete, or connection/rights change. One-tap re-draft.
- **Expired:** the reply window closed or the card is older than the maximum age (default 12 h); copy-ready text remains.
- **Delivery uncertain:** the send request may or may not have reached Telegram (e.g., network failure mid-request). Never retried automatically; the owner is asked to check the chat.

### 12.2 Conditions verified immediately before any send (all must pass)

1. The owner account is active and has not paused autonomy (for autonomous sends) — /pause blocks autonomous sends only: it stops autonomous sending and cancels pending autonomous actions, while approvals the owner already gave stay valid and drafts stay drafts (clarified 2026-10-29, owner decision OD-31).
2. The business connection is enabled, belongs to the owner, and currently grants *reply*.
3. The target conversation belongs to that owner's connection and is not in Ignore mode; it is not a bot chat.
4. The reply window is open (with a safety margin).
5. No owner manual message in that conversation dated after the message the action responds to — even if that owner message was received late (takeover).
6. No newer contact message that the draft did not account for (staleness), unless the approved version's card already showed it.
7. The action has not already been executed (idempotency).
8. For autonomous sends: an active delegation authorizes this conversation and behaviour; volume limits not exceeded; required disclosure included.
9. The action is still relevant (not expired, cancelled, or superseded).

### 12.3 Autonomous reply decision (all must hold; else draft or escalate)

In-scope delegation active · conversation in scope · message is from the contact (not owner/bot) · triage says a reply is appropriate and not an escalation category · the model's draft is within allowed behaviours, uses only permitted information, and reports high confidence · no sensitive-topic flag · window open · no takeover · limits OK · language quality gate passed (FR-AUT-13).

---

## 13. Memory and personalization

| Memory class | Source | Used for | Retention default | Owner control |
|---|---|---|---|---|
| Owner profile & instructions | Owner statements | All behaviour | Until deleted | View/edit/delete |
| Personalization settings | Owner settings | Language, tone, style, notifications | Until changed | Settings |
| Contact notes | Owner statements | Drafts/replies for that contact; Q&A | Until deleted | View/edit/delete |
| Inferred contact facts (R2) | Assistant extraction with provenance | Same, after owner review | Until deleted | Review/approve/delete |
| Shareable knowledge (R2) | Owner | Delegated replies | Saved knowledge: until deleted. Knowledge entered only inside one delegation: until that delegation ends | View/edit/delete |
| Conversation summaries | Assistant | Long-term context, catch-up | Until deleted | Delete per conversation |
| Raw messages | Telegram | Search, Q&A, summaries | 90 days (R-07) | Retention setting, delete |
| Control-chat history | Owner + assistant | Conversation context | 90 days | Deleted with related data |
| Tasks / open loops | Assistant + owner | Follow-ups, attention | Until closed + 1 year | Complete/delete |
| Activity log | System | Review, accountability | 1 year | View; deletion with account |

Storing every message forever is not memory [D]; summaries and curated facts carry long-term context, raw messages expire.

---

## 14. Monitoring, alerts, and automation behaviour

- **Monitors** evaluate only messages in coverage and in non-Ignore conversations. Each monitor has an owner-confirmed natural-language condition plus deterministic pre-filters (contact, conversation, keywords) where possible.
- **Alerts** carry: what happened, why it matters (the matching rule), and next actions. Quiet hours suppress non-urgent alerts into the next digest. Exception: 'Automatic reply not sent' alerts under a delegation (which offer Draft reply) always arrive at once (owner decision 2026-10-29, OD-11a).
- **Distinguish four operation types** [D]: (1) reminders to the owner — always possible; (2) sending as the owner — only while the conversation's reply window is open, and only with the owner's approval of that specific text or under an active delegation that passes §12.3; (3) monitoring a condition — always possible within coverage; (4) acting when a condition is met — notify/draft always possible, send only under (2).
- **Time:** all schedules use the owner's timezone; daylight-saving and timezone changes are handled.
- **Missed schedules** (e.g., after downtime) run late with a note if still relevant, otherwise are skipped with a note.

---

## 15. Errors and exceptions (user-visible behaviour)

| Situation | Behaviour |
|---|---|
| AI provider down or rate-limited | Control chat: "I can't reach the AI service right now; I'll retry." Background triage/drafts retried with backoff; /pause, /status, /resume still work. |
| LLM unavailable or budget exhausted for > 10 min | Deterministic paths continue: messages from VIP contacts alert (respecting quiet hours); deterministic monitors ("when X replies") and reminders fire; one notice "AI review paused — N messages unreviewed"; each active delegation is reported once as paused, and its conversations fall back to alerts that offer a draft for approval (Draft reply) or escalation only (owner decision 2026-10-29, OD-11b). |
| Owner request fails terminally | Reply "I couldn't process that (ref #<run id>). Nothing was sent or changed." (or truthfully state any card that was created). |
| Telegram API failure on send | Classified: definitive failure → "Failed: <reason>" with copy-ready text; transient before delivery → retried safely; ambiguous → "Delivery uncertain". |
| Reply window closed | Card/expiry message with copy-ready text; never retried. |
| Rights missing (no *reply*) | Explain how to enable *Reply to Messages*; provide copy-ready text. |
| Chat appears paused/excluded (send rejected) | Mark conversation "possibly paused"; stop automation there until new messages arrive; tell the owner once. |
| Connection disabled | Stop all business processing and autonomy; tell the owner; resume when reconnected. |
| Ingestion outage | On recovery: record the gap, process the backlog, tell the owner if the gap may have lost messages (> ~24 h or unknown). |
| Ambiguous owner instruction | Ask one targeted question, or act on the most reasonable interpretation and state it, depending on risk (sends/automations always confirmed). |
| Model output invalid or low-confidence | Never sent; becomes a draft or escalation; logged. |
| Contact edits/deletes a message the assistant answered | Recorded; pending drafts outdated; for deletions, stored content purged. |
| Duplicate updates, restarts, out-of-order events | No visible effect; no duplicate alerts or sends. |
| Owner sends a message the assistant cannot route (e.g., unknown contact name) | Lists candidates or asks for clarification. |

---

## 16. Privacy and security requirements

| ID | Requirement |
|---|---|
| SEC-01 | Process only data Telegram delivers under the owner's authorization; never attempt to obtain data outside the business connection and control chat. |
| SEC-02 | Explicit, versioned consent before any managed-conversation content is sent to an external processor; consent names processors (FR-ONB-05). |
| SEC-03 | Data encrypted at rest, with keys stored separately from the data (Telegram §4.4(a)); backups encrypted and stored off-server. |
| SEC-04 | Secrets (bot token, AI keys, encryption keys) never in source control, logs, or the database. |
| SEC-05 | Logs and error reports contain no message content, no names beyond internal ids, and no tokens (Bot API URLs embed the token). |
| SEC-06 | Strict separation of owner-chat traffic and managed-conversation traffic; managed messages can never act as owner commands. |
| SEC-07 | Owner allowlist and per-account data isolation from day one (every record belongs to one account). |
| SEC-08 | Prompt-injection resistance: contact content is delimited as untrusted; runs triggered by contact messages have no tools; owner-requested runs that read contact content treat it as untrusted and cannot change settings or memory, or increase autonomy, without the owner's confirmation; no run sends directly (ARCHITECTURE.md §7.1, §8). |
| SEC-09 | Deletion on request cascades to derived data; retention purges run automatically (Telegram §4.2). |
| SEC-10 | No training/evaluation/benchmarking on real user data; synthetic or hand-written evaluation sets only (Telegram §4.3, Content Licensing). |
| SEC-11 | Owner-visible record of all assistant activity (Telegram §5.4(v)). |
| SEC-12 | Scope changes (new capabilities touching managed data) are announced to the owner with an opt-out before taking effect (Telegram §5.2/§5.4(vi)). |
| SEC-13 | Breach response runbook: assess, contain, notify the owner (and, for a public product, affected users/regulators as the law requires). |

---

## 17. Non-functional requirements

| ID | Requirement | Target |
|---|---|---|
| NFR-01 | **Ingestion durability** | No delivered update lost after receipt; outages shorter than Telegram's retention lose nothing. |
| NFR-02 | **Ingestion freshness** | p95 time from Telegram delivery to persisted message < 5 s. |
| NFR-03 | **Ingestion watchdog** | Owner alerted (via the bot, plus an external heartbeat) if ingestion stalls > 15 min. |
| NFR-04 | **Control-chat responsiveness** | Acknowledgement/progress < 2 s p95; simple answers < 20 s p95; catch-up summaries < 60 s p95. |
| NFR-05 | **Triage latency** (R2) | Inbound message → triage complete < 90 s p95 (includes burst debounce). Urgent alert delivered < 2 min p95 after the message. |
| NFR-06 | **Send safety** | Zero duplicate sends; zero sends that bypass policy; 100 % of sends recorded with authorization reference. Verified by automated tests and audits. |
| NFR-07 | **Kill-switch latency** | /pause stops autonomous sending within 5 s; no autonomous send starts after the pause is acknowledged (measured for /pause, the Pause button and the stop-word matcher). |
| NFR-08 | **Availability** (personal) | ≥ 99 % monthly for ingestion on the always-on host; restart to processing < 60 s. |
| NFR-09 | **Recoverability** | Daily encrypted off-server backups; restore tested before R1 exit; RPO ≤ 24 h, RTO ≤ 4 h; a restore never causes a send: after a restore, autonomy is paused and all non-terminal actions are cancelled (in-flight ones marked delivery uncertain) and listed for the owner's review (ARCHITECTURE §15.6). |
| NFR-10 | **Cost visibility** | Every AI call metered per account and purpose; configurable monthly budget with owner alerts at 50/80/100 %. |
| NFR-11 | **Multilingual support** | Uzbek (Latin, Cyrillic, new-alphabet letters), Russian, English for ingestion, search, summaries, and owner-facing replies. |
| NFR-12 | **AI quality gates** | Synthetic evaluation sets (no real chats) per capability: summary faithfulness, retrieval recall, triage accuracy, draft acceptability, injection resistance, language/script correctness. Thresholds set in the implementation plan; autonomy requires passing the draft-quality gate in each enabled language. |
| NFR-13 | **Security hygiene** | Dependencies locked; automated vulnerability check in CI; secrets scanning; least-privilege runtime. |
| NFR-14 | **Observability** | Structured logs (content-free), health status via /status, error alerting to the owner, per-run AI usage records. |
| NFR-15 | **Scalability path** | Data model and processing partitioned by account so multi-user operation needs no schema rewrite. |
| NFR-16 | **Maintainability** | Typed Python, modular boundaries, automated tests for every slice, documentation updated with each milestone. |

---

## 18. Release scope

### R0 — Foundation (M00–M02)
Repository, tooling, Telegram probe and live validation, control-chat assistant with consent and allowlist, durable ingestion, business connection lifecycle, message classification and storage, coverage and visibility report, privacy policy, encryption at rest, backups, always-on deployment.
**Exit:** owner chats with the assistant; managed conversations are captured reliably and visibly; no AI processing of managed content before consent; live validation results recorded.

### R1 — Personal Alpha (M03–M04)
Search, summaries, catch-up, attention list v1, owner instructions and contact notes, personalization and the /settings panel, conversation modes and panels, drafts with approval cards and verified sending, kill switch, activity log, retention and deletion controls.
**Exit:** the owner uses it daily for catch-up and approved replies; zero incorrect sends in the acceptance period.

### R2 — Personal Beta = **Personal v1** (M05–M07)
Triage and alerts, quiet hours, commitments/open loops, monitors, digests, delegations with disclosure and takeover, reminders, conditional follow-ups, automation management, inferred memory with review, export.
**Exit:** acceptance criteria in §21 met over a 2-week personal trial.

### R3 — Expansion (M08)
Prioritized after Personal v1 review: channels (admin/forwarded), voice transcription, images, semantic retrieval if evaluation shows need, undo, owner-approved scheduled sends (FR-DRF-11), opt-in mark-as-read, rich-message digests, Mini App settings.

### R4 — Public readiness (M09) — **only on the owner's explicit go-ahead**
Multi-user onboarding, isolation hardening, quotas, webhook scale-out, legal review (Uzbekistan, GDPR, Telegram terms clarification), public privacy policy and terms, billing inside the bot exclusively via Telegram Stars (TF §7.1); local payment options only for any sales made outside Telegram, never linked from the bot, with Stars parity; abuse prevention, operations.

---

## 19. Future capabilities (vision, not committed)

Voice and audio understanding; document processing; advanced semantic search; richer contact-aware communication; external integrations (calendar, email) as tools; Mini App dashboard; advanced conditional workflows; group features (possibly via Guest Mode) if a compelling use case emerges; per-user managed bots.

---

## 20. Explicit exclusions

- Reading chats outside the business connection; importing history; reading followed channels the bot does not administer; scraping.
- MTProto/user-client access to the owner's account (unless separately decided after review).
- Initiating conversations, cold outreach, bulk messaging, spam-like features.
- Sending outside Telegram's reply window by any workaround.
- Group chat features (deferred).
- Emotion recognition, personality/trust scoring, profiling of contacts.
- Saving contacts' deleted messages for later display.
- Deleting contacts' messages; editing the owner's messages; profile/gift/Stars/story management.
- Training or fine-tuning models on user data.
- Generic rewrite/translate features that Telegram ships natively (may exist only as part of drafting).
- Billing, public onboarding, and commercialization work before the owner's decision.

---

## 21. Product acceptance criteria (release level)

### R0
1. Non-owner accounts cannot use the assistant or connect it (verified with a third account).
2. With a test contact, a scripted list of ≥ 60 numbered synthetic messages (text, edits, deletions, media, owner replies from 2 clients) sent during a 2-hour session matches the store one-to-one via a verification query, with correct sender classification (contact / owner manual / owner automated): 0 missing, 0 duplicates.
3. Restarting the system mid-session loses no messages and creates no duplicates.
4. "What can you see?" reports connection state, rights, coverage start, and gaps accurately.
5. No managed-conversation content is sent to the AI provider before the managed-content consent; verified by tests and logs.

### R1
1. Catch-up for a 4-hour period with ≥ 20 synthetic-scenario messages across ≥ 4 conversations includes every question addressed to the owner and every commitment (≥ 95 % recall on the evaluation set) and contains no fabricated facts.
2. Search finds a known message by Uzbek Latin query when the message is in Cyrillic (and vice versa), and tolerates apostrophe variants.
3. Draft → edit → approve → sent works; the sent text equals the approved text; the card shows the final state.
4. Every outdating or expiry condition (new contact message, owner manual reply, source deletion, rights removal, window close) prevents sending a stale draft (automated scenario tests + manual checks).
5. Double-tapping Approve, restarting during sending, and simulated network timeouts never cause a duplicate send.
6. /activity lists all sends with their authorization.
7. Deleting a conversation's data removes messages, summaries, memory, and tasks derived from it.
8. Retrieval evaluation thresholds (IMPLEMENTATION_PLAN S03.4) are met.
9. Every message the assistant sends is recorded exactly once as `assistant_sent` from the send result; an echo update, if delivered, merges into it (LV-03).

### R2 (Personal v1)
1. J5 delegation scenario passes end-to-end with a real second account: in-scope known questions answered with disclosure; unknowns asked/escalated; a decision request triggers an immediate alert; owner takeover stops autonomy in that chat; end-of-session summary is accurate.
2. /pause stops autonomous sending within 5 s (measured).
3. J7 flagship workflow passes with both outcomes (documents arrive / don't arrive).
4. "Notify me when Daniel replies" fires exactly once on the first reply.
5. Triage on the synthetic evaluation set: ≥ 90 % recall for "needs owner decision/urgent", ≤ 20 % false alerts.
6. Uzbek draft-quality gate passed before Uzbek autonomy is enabled.
7. Two-week personal trial: zero sends to wrong recipients, zero duplicate sends, zero sends outside policy; owner usefulness rating ≥ 4/5 weekly; ≥ 70 % of immediate alerts not marked "not useful" via the feedback button.

---

## 22. Open product decisions

| ID | Question | Recommendation / default | Needed by |
|---|---|---|---|
| Q-01 | Product and bot name (no "Telegram"; username must end in "bot"). | **Decided 2026-10-21: Alviro** (owner, in conversation). | M02 S02.5 (prod bot); the M00 probe/dev bot uses a neutral throwaway username |
| Q-02 | Approve R-01…R-11. | As recommended. | Before M00 |
| Q-03 | Retention defaults (R-07). | Raw message content 90 days; summaries and memory until deleted; tasks until closed + 1 year; control-chat history 90 days; activity log 1 year; raw update payloads up to 7 days. | M02 |
| Q-04 | Default AI-disclosure wording and languages. | Short neutral line, owner-editable, in the reply language. | M06 |
| Q-05 | Takeover cooldown (R-09). | Until owner re-enables or delegation ends. | M06 |
| Q-06 | Default mode for new conversations (R-03). | Observe. | M03 |
| Q-07 | Digest time and quiet hours defaults. | Digest 21:00 local; quiet hours 23:00–08:00 with urgent bypass off by default. | M05 |
| Q-08 | Should the owner be able to enable mark-as-read? | Allowed as an opt-in setting, logged. | M08 |
| Q-09 | Confirmation for owner-dictated sends (R-05). | **Resolved:** always confirmed in Personal v1; revisit an opt-out after the Personal v1 trial. | — |
| Q-10 | Which managed chats to include in Chat Automation for the first weeks. | Start with a small set of trusted contacts and the test account; expand after R1. | M02 |
| Q-11 | Public launch, monetization model, target markets. | Deferred until the owner decides (R-10). | R4 |
