# Privacy policy

| | |
|---|---|
| **Version** | v0 (draft — control chat only) |
| **Status** | **Not yet approved or published.** Blocked on PQ-09 and owner approval. |
| **Last updated** | 2026-10-18 |

> **This version covers the control chat only.** It does not yet cover managed
> conversations (messages between the owner and their contacts), which begin at
> M02 and require a version bump and a separate `ai_managed` consent before any
> such data is processed.

## Who this is about

This assistant is operated by its owner, for their own use. There is one owner.
"You" below means the owner.

## What is collected

**In the control chat** (your private chat with the bot):

| Data | Why | Kept for |
|---|---|---|
| Your Telegram user id | To recognise you as the owner and refuse everyone else | While the account exists |
| Messages you send to the bot, and its replies | So the assistant can hold a conversation with context | 90 days |
| Your settings (language, timezone) | To reply in the right language and at the right time | While the account exists |
| Raw Telegram updates | Debugging and replay after a failure | 7 days, then deleted |
| Records of AI calls (model, token counts, cost) | Budgeting. **No message content.** | 1 year |
| An activity log (what the assistant did, and when) | So you can audit it. **No message content.** | 1 year |

**From anyone who is not the owner:** nothing. A message from a
non-allowlisted account produces no stored record beyond the fact that an
update arrived, and its content is discarded at processing.

## Who else sees it

| Processor | What reaches them | Their terms |
|---|---|---|
| **OpenAI** (`gpt-6-luna`) | The text of your control-chat conversation, when you ask the assistant something | **To be recorded — PQ-09.** Training on API data is disabled in the account. |
| **Telegram** | Everything, inherently — it is the transport | Telegram's own privacy policy |

No other processor. No analytics, no error-tracking service, no advertising.
Adding one requires updating this policy and asking you again, **before** any
data flows (AGENTS §9).

Your messages are **not** used to train any model. They are never sent to
feedback programmes, file/upload APIs or batch APIs.

## Consent

Nothing is sent to an AI provider without your active `ai_control` consent,
which names the provider. Revoking it stops new processing immediately; work
already queued re-checks before it runs.

## Your rights

- **See it** — the activity log, and an export of your data.
- **Delete it** — any conversation, or everything.
- **Revoke consent** — at any time, with no loss of the non-AI functions.

## Two honest limitations

- **Messages already delivered stay in your Telegram history.** The bot can
  delete its own messages only within 48 hours, and cannot delete yours.
- **Deleted data can survive in encrypted backups** until they rotate out —
  up to about four weeks (14 daily + 4 weekly).

## Changes

This policy is versioned. If the scope of processing grows, you get a notice
and an opt-out **before** the change takes effect.

## Contact

(To be filled in before publication — owner decision, see IMPLEMENTATION_PLAN
§13: privacy-policy host and contact address.)

---

### What must happen before this is published

1. **PQ-09** — record OpenAI's actual retention and data-usage terms in
   TELEGRAM_FEASIBILITY §7.2, with a source URL.
2. Owner chooses the host (default: a telegra.ph page) and a contact address.
3. Owner reviews and approves this text and the consent wording.
4. Register the URL with @BotFather.

Until all four are done, no message content may be sent to an AI provider
(AGENTS §9, global Definition of Done).
