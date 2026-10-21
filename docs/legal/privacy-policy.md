# Privacy policy

| | |
|---|---|
| **Version** | v3 — adds deletion of stored managed-chat data and the "stop observing" choice (no new processing) |
| **Status** | Published 2026-10-21 (plan approved by the owner 2026-10-21) at the same URL as v1/v2: https://telegra.ph/Personal-AI-assistant--privacy-policy-v1-10-19 (Telegraph keeps the original slug; the page title says v3). |
| **Last updated** | 2026-10-21 |
| **Previous** | v2 — control chat + managed chats, published 2026-10-19; v1 — control chat only, published 2026-10-18 (see git history of this file) |

Everything between the two `---` lines below is the published text. Change it
only with a version bump (AGENTS §16); a change that widens processing needs an
owner notice with opt-out before it takes effect. v2 widens processing to
chats included under Chat Automation; the opt-in is the separate consent asked
when the connection arrives (FR-ONB-05b), and nothing from those chats reaches
an AI provider without it.

---

## Personal AI assistant — privacy policy (v3)

This is a personal assistant bot, run by its owner for their own use. There is one user, the owner, and "you" below means the owner. This version covers your private chat with the bot (the control chat) and the chats you choose to include when you connect the bot to your account under Telegram's Chat Automation (managed chats).

## What is stored

- Your Telegram user id — to recognise you and refuse everyone else. Kept while your account exists.
- The messages you send to the bot and its replies — so it can hold a conversation with context. The text is deleted after 90 days.
- Your settings (language, timezone) — kept while your account exists.
- The connection between the bot and your account: its id, the permissions you granted, and when it changed — so the assistant knows what it may do. No message content. The change history is deleted after 1 year.
- Messages in your managed chats, from the moment you connect the bot: the text and basic details (who sent it, when, whether it was edited). The text is deleted after at most 90 days; a message deleted in Telegram is deleted here too. The bot cannot see anything sent before you connected it, or chats you did not include.
- Raw Telegram updates — for recovery after a failure. Their content is deleted after at most 7 days, and immediately for anything the assistant does not keep.
- Records of AI calls (model, token counts, estimated cost) — for budgeting. No message content. Deleted after 1 year.
- An activity log (what the assistant did and when) — so you can audit it. No message content. Deleted after 1 year.

Deletion happens automatically, every hour.

## The people you talk to

Your managed chats contain messages written by other people. The assistant stores them, as described above, only because you included those chats; it uses them only to help you. It never writes to anyone on its own initiative: in this version it does not send anything into your managed chats at all.

If someone who is not you writes to the bot directly, they get one fixed reply saying it works only for its owner. Their message is not answered by AI, not stored beyond the technical fact that an update arrived, and its content is discarded when it is processed. If someone else connects the bot to their own account, nothing from it is kept: the assistant records only the connection id and their Telegram user id, to refuse it, and deletes that record 30 days after it was last seen.

## Who else receives data

- OpenAI (model gpt-6-luna) — receives the text of your control-chat conversation when you ask the assistant something, and only after you have agreed to it. Text from your managed chats is sent to OpenAI only after you have separately agreed to that, and only when the assistant works on those chats for you. OpenAI does not use API data to train its models, and data sharing is turned off in this account. OpenAI keeps abuse-monitoring logs, which can include prompts and replies, for up to 30 days. The assistant tells OpenAI not to store responses, so nothing else is kept there. OpenAI's terms: https://developers.openai.com/api/docs/guides/your-data
- Telegram — carries every message, under Telegram's own privacy policy.

There is no other recipient: no analytics, no error-tracking service, no advertising. No message is ever used to train or evaluate any AI model, or sent to feedback programmes, file-upload or batch services.

## Your consent

There are two separate consents, and each request names the provider:

1. AI for the control chat — asked when you first start the bot.
2. AI for your managed chats — asked when you connect the bot to your account. Until you agree, nothing from those chats is sent to any AI provider.

You can withdraw both at any time by sending /privacy revoke to the bot, and give the second one later with /privacy managed. Withdrawal takes effect immediately: work already queued checks your consent again before it runs and stops. Everything that does not need AI (/status, /settings, /privacy) keeps working.

## Your rights in this version

- See your consent state and this policy at any time with /privacy.
- Withdraw consent at any time with /privacy revoke.
- Stop the bot seeing your chats at any time by disconnecting it, or by removing chats from Chat Automation, in your Telegram settings.
- Delete everything the assistant stored from your included chats (messages, names, raw updates): when you decline or withdraw the second consent, the bot offers a **Delete** button, and then a choice between continuing without AI (coverage and visibility only) and **stopping** (it stores nothing further; to disconnect it completely, remove it under Settings → Chat Automation — the bot cannot do that itself). Deleting chosen conversations and exporting your data from inside the bot are planned and not yet available.

## Limitations, stated plainly

- Messages already delivered stay in your Telegram chat history and in the other person's; this policy covers what the assistant stores, not what Telegram shows.
- There are no backups yet. When backups are introduced, deleted data may survive in them until they rotate out, and this policy will say for how long.

## Changes

This policy is versioned. If what is processed ever grows, you will be told and offered an opt-out before the change takes effect.

## Contact

This is a personal deployment: the operator is the owner. Questions about it go to the owner directly.

---

### Publication record

1. PQ-09 — OpenAI's terms recorded in TELEGRAM_FEASIBILITY §7.2 (2026-10-18).
2. Host: telegra.ph (plan default), same page as v1.
3. v1 published 2026-10-18 (owner go-ahead). v2 drafted 2026-10-18 in M02 S02.1;
   owner go-ahead to publish: 2026-10-19; published the same day (page edited in place).
4. @BotFather registration: same URL, no change needed.
5. v3 (2026-10-21, M02 S02.4): deletion of stored managed-chat data and the "stop observing" choice become available from inside the bot; no new processing, so no opt-out notice is required (AGENTS §16).
