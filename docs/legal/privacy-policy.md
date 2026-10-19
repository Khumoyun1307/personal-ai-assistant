# Privacy policy

| | |
|---|---|
| **Version** | v1 — control chat only |
| **Status** | Published 2026-10-18 (owner go-ahead given in the session of 2026-10-18). URL recorded in M01.md and set as `PRIVACY_POLICY_URL`. |
| **Last updated** | 2026-10-18 |

Everything between the two `---` lines below is the published text. Change it
only with a version bump (AGENTS §16); a change that widens processing needs an
owner notice with opt-out before it takes effect.

---

## Personal AI assistant — privacy policy (v1)

This is a personal assistant bot, run by its owner for their own use. There is one user, the owner, and "you" below means the owner. This version covers only your private chat with the bot (the control chat). It does not cover your conversations with other people; processing those would need a new version of this policy and your separate consent first.

## What is stored

- Your Telegram user id — to recognise you and refuse everyone else. Kept while your account exists.
- The messages you send to the bot and its replies — so it can hold a conversation with context. The text is deleted after 90 days.
- Your settings (language, timezone) — kept while your account exists.
- Raw Telegram updates — for recovery after a failure. Their content is deleted after 7 days.
- Records of AI calls (model, token counts, estimated cost) — for budgeting. No message content. Deleted after 1 year.
- An activity log (what the assistant did and when) — so you can audit it. No message content. Deleted after 1 year.

Deletion happens automatically, every hour.

## People who are not the owner

If anyone else writes to the bot, they get one fixed reply saying it works only for its owner. Their message is not answered by AI, not stored beyond the technical fact that an update arrived, and its content is discarded when it is processed.

## Who else receives data

- OpenAI (model gpt-6-luna) — receives the text of your control-chat conversation when you ask the assistant something, and only after you have agreed to it. OpenAI does not use API data to train its models, and data sharing is turned off in this account. OpenAI keeps abuse-monitoring logs, which can include prompts and replies, for up to 30 days. The assistant tells OpenAI not to store responses, so nothing else is kept there. OpenAI's terms: https://developers.openai.com/api/docs/guides/your-data
- Telegram — carries every message, under Telegram's own privacy policy.

There is no other recipient: no analytics, no error-tracking service, no advertising. Your messages are never used to train or evaluate any AI model, and are never sent to feedback programmes, file-upload or batch services.

## Your consent

Nothing is sent to an AI provider until you agree, and the request names the provider. You can withdraw at any time by sending /privacy revoke to the bot. Withdrawal takes effect immediately: work already queued checks your consent again before it runs and stops. Everything that does not need AI (/status, /settings, /privacy) keeps working.

## Your rights in this version

- See your consent state and this policy at any time with /privacy.
- Withdraw consent at any time with /privacy revoke.
- Deleting chosen data and exporting your data from inside the bot are planned and not yet available. Until they are, the data lives only in the owner's own database, and the owner — you — can delete it directly.

## Limitations, stated plainly

- Messages already delivered stay in your Telegram chat history; this policy covers what the assistant stores, not what Telegram shows you.
- There are no backups yet. When backups are introduced, deleted data may survive in them until they rotate out, and this policy will say for how long.

## Changes

This policy is versioned. If what is processed ever grows, you will be told and offered an opt-out before the change takes effect.

## Contact

This is a personal deployment: the operator is the owner. Nobody else's data is kept, so there is no one else to contact about it.

---

### Publication record

1. PQ-09 — OpenAI's terms recorded in TELEGRAM_FEASIBILITY §7.2 (2026-10-18).
2. Host: telegra.ph (plan default). Contact: no address published — the owner
   is the only data subject and operator; an address can be added in v1.1.
3. Owner go-ahead to finalise and publish: 2026-10-18.
4. @BotFather registration: done by the owner (no Bot API method exists).
