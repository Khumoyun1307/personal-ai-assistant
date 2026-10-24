# Privacy policy

| | |
|---|---|
| **Version** | **v4 (draft)** — plain-language rewrite; names where data is hosted (Oracle Cloud, US East — Ashburn) and introduces encrypted backups (on the server + Backblaze B2, EU) with how long deleted data can remain in them |
| **Status** | **Draft 2026-10-23, not published yet.** Owner go-ahead to publish given 2026-10-23. **Code is at v4** (`POLICY_VERSION = "v4"`, M02 D59: existing consents stay valid; owners with a consent recorded under an older version, active or revoked, get a one-time update notice with the link and the opt-outs: `/privacy revoke` for AI processing, removing Alviro under Chat Automation for storage). The lead publishes this text at https://telegra.ph/Personal-AI-assistant--privacy-policy-v1-10-19 after that change merges and before the merged code runs anywhere with `PRIVACY_POLICY_URL` set (any `just run` or `just deploy`), and **before the production server holds real data and takes its first backup**; v3 stays the published text until then. |
| **Last updated** | 2026-10-23 |
| **Previous** | v3 — deletion and "stop observing", published 2026-10-21; v2 — control chat + managed chats, published 2026-10-19; v1 — control chat only, published 2026-10-18 (see git history of this file) |

Everything between the two `---` lines below is the published text. Change it
only with a version bump (AGENTS §16); a change that widens processing needs an
owner notice with opt-out before it takes effect. v2 widens processing to
chats included under Chat Automation; the opt-in is the separate consent asked
when the connection arrives (FR-ONB-05b), and nothing from those chats reaches
an AI provider without it.

---

## Alviro — Privacy Policy

Last updated: 23 October 2026 (version 4)

Alviro is a personal assistant bot on Telegram, run by its owner for their own use. In this policy, "you" means the owner. It covers your chat with Alviro and the chats you connect to it through Telegram's Chat Automation.

## What Alviro stores

- **Your Telegram account ID and settings** (such as language and time zone), so Alviro knows it's you. Kept while you use Alviro.
- **Your chat with Alviro** — your messages and its replies, so it can follow the conversation. Message text is kept for 90 days.
- **Messages in the chats you connect** — the text and basic details such as who sent it, when, and whether it was edited, starting from the moment you connect Alviro. Message text is kept for up to 90 days. If a message is deleted in Telegram, Alviro deletes it too. Alviro can't see earlier messages or chats you didn't connect.
- **Connection details** — the permissions you gave Alviro and when they changed. Kept for 1 year.
- **Technical records** — an activity log of what Alviro did and the usage and cost of AI requests, kept for 1 year and containing no message text; and short-lived copies of incoming Telegram updates, used only to recover from errors and deleted within 7 days.

Old data is deleted automatically.

## How Alviro uses your data

Only to help you: answering your questions, summarizing and searching your chats, and remembering your settings. Alviro doesn't send messages to anyone on its own, doesn't share your data for advertising, and doesn't use it to train AI models.

## AI processing

Alviro uses OpenAI's API to understand and answer your messages.

- Your chat with Alviro is processed by OpenAI after you agree to it when you first start the bot.
- Messages from your connected chats are processed by OpenAI only after you turn on Alviro's AI agent for them — when you connect Alviro, or any time later with /privacy managed.

Under OpenAI's API terms, data sent through the API isn't used to train their models. OpenAI may keep requests for up to 30 days to check for abuse, then deletes them. More information: https://developers.openai.com/api/docs/guides/your-data

## Where your data is kept

Alviro runs on a server rented from Oracle Cloud in the United States (Ashburn, Virginia), on an encrypted disk. Once a day the database is backed up; the backup is encrypted before it is stored, on the server and as a second copy with Backblaze B2 in the EU. Only the owner holds the key that can open the backups. Backups are kept for up to 5 weeks, so something deleted — by you or automatically — can remain in an older backup until that backup expires (normally no more than 35 days).

## Other people in your chats

The chats you connect include messages from other people. Alviro stores and uses them only to help you, as described above.

If someone else writes to Alviro directly, they get a short reply that it's a private assistant; their message isn't answered by AI or stored. If someone tries to connect Alviro to their own Telegram account, the connection is refused; Alviro keeps only a short technical note to recognise it, deleted 30 days after it was last seen.

## Your choices

- **/privacy** — see your current settings and this policy.
- **/privacy managed** — turn on Alviro's AI agent for your connected chats.
- **/privacy revoke** — turn off AI processing. It takes effect right away; features that don't need AI, such as /status and /settings, keep working.
- When you turn the AI agent off for your connected chats, Alviro offers to delete everything it stored from them.
- You can disconnect Alviro or remove chats at any time in Telegram: Settings → Chat Automation.

## Other services

Telegram delivers all messages under its own privacy policy. Oracle Cloud provides the server and Backblaze stores the encrypted backups; they provide infrastructure only. Alviro doesn't use analytics, advertising or tracking services.

## Changes

If this policy changes in a way that affects what Alviro stores or shares, you'll be told in the bot before the change applies.

## Contact

Alviro is run by its owner. For any questions, contact the owner directly.

---

### Publication record

1. PQ-09 — OpenAI's terms recorded in TELEGRAM_FEASIBILITY §7.2 (2026-10-18).
2. Host: telegra.ph (plan default), same page as v1.
3. v1 published 2026-10-18 (owner go-ahead). v2 drafted 2026-10-18 in M02 S02.1;
   owner go-ahead to publish: 2026-10-19; published the same day (page edited in place).
4. @BotFather registration: same URL, no change needed.
5. v3 (2026-10-21, M02 S02.4): deletion of stored managed-chat data and the "stop observing" choice become available from inside the bot; no new processing, so no opt-out notice is required (AGENTS §16).
6. v4 drafted 2026-10-22 (M02 S02.6; owner decisions D38/D39), rewritten 2026-10-23 in plain language at the owner's request and updated for the actual home region (Oracle Cloud US East — Ashburn; the owner created the account there). Adds hosting and encrypted backups (on the server and at Backblaze B2, EU, ciphertext only), with deleted data remaining in backups for at most 35 days. This **widens** what is kept, so per AGENTS §16 it needs an owner notice with opt-out before it takes effect: for this single-owner deployment, the owner's go-ahead (given 2026-10-23) and publication before the first production backup. `POLICY_VERSION` moved to v4 in code on 2026-10-23 (M02 D59), ahead of publication: existing consents stay valid, and owners holding a consent recorded under an older version get a one-time in-bot notice with the link and the opt-out (`inbox/policy_notice.py`). The page is published at the same URL before that code runs anywhere with `PRIVACY_POLICY_URL` set; publication date: to be recorded.
