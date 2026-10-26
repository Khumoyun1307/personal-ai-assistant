# Privacy policy

| | |
|---|---|
| **Version** | **v5** (M03 close-out) — professional, sectioned rewrite; adds the owner's **notes** (memory items) and the **chat summaries**, both kept until deleted (beyond the 90-day message limit; catch-up summaries 30 days), the retention settings (shorten only), chat modes, and the deletion controls (one chat, one person, everything); names OpenAI, Oracle, Backblaze, Telegram and the uptime monitor as service providers |
| **Status** | **Draft, awaiting the owner's go-ahead** ("publish v5"). To be published at https://telegra.ph/Personal-AI-assistant--privacy-policy-v1-10-19 (same page, edited in place by `tools/legal/publish_policy.py`; title "Alviro — Privacy Policy"; plain-text copy for a manual paste: [privacy-policy-v5-telegraph.txt](privacy-policy-v5-telegraph.txt)) **before** the code at v5 is deployed. **Code is at v5 in the close-out PR** (`POLICY_VERSION = "v5"`, M02 D59: existing consents stay valid; owners with a consent recorded under an older version, active or revoked, get a one-time update notice with the link and the opt-outs: `/privacy revoke` for AI processing, `/privacy delete` for what is stored, removing Alviro under Chat Automation for storage). |
| **Last updated** | 2026-10-26 (v5 drafted) |
| **Previous** | v4 — plain-language rewrite, hosting (Oracle Cloud, US East — Ashburn) and encrypted backups (server + Backblaze B2, EU; deleted data in backups at most 35 days), published 2026-10-24; v3 — deletion and "stop observing", published 2026-10-21; v2 — control chat + managed chats, published 2026-10-19; v1 — control chat only, published 2026-10-18 (see git history of this file) |

Everything between the two `---` lines below is the published text. Change it
only with a version bump (AGENTS §16); a change that widens processing needs an
owner notice with opt-out before it takes effect. v2 widens processing to
chats included under Chat Automation; the opt-in is the separate consent asked
when the connection arrives (FR-ONB-05b), and nothing from those chats reaches
an AI provider without it.

---

## Alviro — Privacy Policy

Last updated: 26 October 2026 (version 5)

Alviro is a personal assistant on Telegram, run by its owner for their own use. In this policy, "you" means the owner. It covers your chat with Alviro and the chats you connect to it through Telegram's Chat Automation, and explains what Alviro keeps, why, who helps process it, how long it is kept, and how you can change or delete it.

## Information Alviro keeps

- Account and settings: your Telegram account ID and your settings, such as language, time zone, reply preferences, how long data is kept, and whether new chats are observed. Kept while you use Alviro.
- Your chat with Alviro: your messages, including text you forward or quote, and Alviro's replies, so it can follow the conversation.
- Messages in the chats you connect: the text and basic details such as who sent it, when, and whether it was edited, from the moment you connect Alviro. Alviro can't see earlier messages, or chats you didn't include. When a message is deleted for everyone in Telegram, Alviro deletes it too, and rebuilds any summary that included it.
- Names of the people in those chats: their names and usernames as Telegram shows them.
- Chat summaries: short summaries of each connected chat, written by Alviro's AI so it keeps the context after the messages themselves expire, and summaries of a period you ask about ("what did I miss?"). They are made only while AI is on for your connected chats.
- Notes: things you ask Alviro to remember, such as your instructions, preferences and notes about people.
- Connection details: the permissions you gave Alviro in Telegram and when they changed.
- Technical records: an activity log of what Alviro did and records of AI usage and cost, which contain no message text; short-lived copies of incoming Telegram updates, used only to recover from errors; and a change Alviro asks you to confirm, held until you tap Confirm or Reject.

## How Alviro uses it

Only to help you: answering your questions, searching and summarizing your chats, telling you who is waiting for a reply, remembering your notes and settings, and showing you what it stores. Alviro doesn't send messages to other people on its own, doesn't sell your data or use it for advertising, and doesn't use it to train AI models.

## Service providers

Alviro relies on a few providers, each only for the part of the service described here:

- OpenAI processes text to produce Alviro's answers, summaries and notes. Your chat with Alviro is processed after you agree to it when you first start the bot. Content from your connected chats (their messages, and the summaries and notes based on them) is processed only after you turn this on separately, when you connect Alviro or later with /privacy managed. Under OpenAI's API terms, this data isn't used to train their models; OpenAI may keep requests for up to 30 days to check for abuse, then deletes them. More information: https://developers.openai.com/api/docs/guides/your-data
- Oracle Cloud hosts the server, in the United States (Ashburn, Virginia), on an encrypted disk.
- Backblaze B2 stores a second copy of the encrypted backups, in the EU.
- Telegram delivers all messages, under its own privacy policy.
- Healthchecks.io receives a regular signal that Alviro is running. The signal carries no information about you or your chats.

Alviro doesn't use analytics, advertising or tracking services.

## How long it is kept

- Messages in your connected chats: 90 days, or a shorter period you choose (14 to 90 days).
- Your chat with Alviro: 90 days, or a shorter period you choose (14 to 90 days).
- Chat summaries: until you delete them. Summaries of a period you asked about: 30 days.
- Notes: until you delete them. A note you ask Alviro to forget is no longer used at once and is erased after 24 hours, so that you can undo it.
- Names of people: until no message from them is kept any more.
- Connection details, the activity log and records of AI usage: 1 year.
- Copies of incoming Telegram updates: up to 7 days, or a shorter period you choose (1 to 7 days).
- A change waiting for your confirmation: until you decide, at most 24 hours.

Old data is deleted automatically, once an hour. You can shorten these periods but not make them longer than stated here.

## Where it is kept, and backups

Your data is kept on the server described above. Once a day the database is backed up; each backup is encrypted before it is stored, on the server and as a second copy with Backblaze B2 in the EU. Only the owner holds the key that can open the backups. Backups are kept for up to 5 weeks, so something deleted, by you or automatically, can remain in an older backup until that backup expires (normally no more than 35 days).

## Your choices

- /privacy shows your current settings and this policy.
- /privacy managed turns on AI for your connected chats; /privacy revoke turns AI processing off. Revoking takes effect at once; features that don't need AI, such as /status and /settings, keep working.
- /settings lets you choose how long messages are kept, the language and time zone, reply preferences, and whether new chats are observed.
- For each chat you can choose Observe or Ignore on its panel. Ignore stops Alviro saving the content of new messages from that chat; what is already stored stays until you delete it.
- You can ask Alviro what it knows about someone, and ask it to forget a note. Alviro tells you each time it saves or forgets a note, with a button to undo it.
- You can disconnect Alviro or remove chats at any time in Telegram: Settings → Chat Automation.

## Deleting your data

- One chat: open the chat's panel and choose Delete stored data (the chat's data, or the chat's data and the notes about it), or choose Ignore together with deleting its data.
- One person: ask Alviro to delete what it stored about a person; it asks you to confirm first.
- Everything: /privacy delete, or /settings → Delete everything, removes all stored messages, summaries, notes and names, and the text of your chat with Alviro. Your account, settings and consents, the connection, and the activity log (which holds no message text) stay, so Alviro keeps working; new messages are saved again unless you set chats to Ignore or disconnect Alviro.
- When you turn AI off for your connected chats, Alviro offers to delete what it stored from them.

Before deleting at your request, Alviro asks you to confirm, and afterwards it tells you what was deleted. Deleting can't be undone; deleted data may remain in an encrypted backup until it expires, as described above.

## Other people in your chats

The chats you connect include messages from other people. Alviro stores and uses them, and any notes or summaries about them, only to help you, as described above.

If someone else writes to Alviro directly, they get a short reply that it's a private assistant; their message isn't answered by AI or stored. If someone tries to connect Alviro to their own Telegram account, the connection is refused; Alviro keeps only a short technical note to recognise it, deleted 30 days after it was last seen.

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
6. v4 drafted 2026-10-22 (M02 S02.6; owner decisions D38/D39), rewritten 2026-10-23 in plain language at the owner's request and updated for the actual home region (Oracle Cloud US East — Ashburn; the owner created the account there). Adds hosting and encrypted backups (on the server and at Backblaze B2, EU, ciphertext only), with deleted data remaining in backups for at most 35 days. This **widens** what is kept, so per AGENTS §16 it needs an owner notice with opt-out before it takes effect: for this single-owner deployment, the owner's go-ahead (given 2026-10-23) and publication before the first production backup. `POLICY_VERSION` moved to v4 in code on 2026-10-23 (M02 D59), ahead of publication: existing consents stay valid, and owners holding a consent recorded under an older version get a one-time in-bot notice with the link and the opt-out (`inbox/policy_notice.py`). The page is published at the same URL before that code runs anywhere with `PRIVACY_POLICY_URL` set; **published 2026-10-24** (page edited in place with `tools/legal/publish_policy.py` after PR #12 merged; the live page was checked to show version 4, the Ashburn hosting, the Backblaze backups and the 35-day limit). @BotFather registration: same URL, no change needed.
7. v5 drafted 2026-10-26 (M03 close-out). Closes the gap M03.md recorded since S03.3: v4 named neither the owner's notes (`memory_items`) nor the rolling chat summaries kept beyond the 90-day message limit (both "until deleted"; catch-up period summaries 30 days), nor the S03.5 controls (retention that only shortens, per-chat/per-contact/everything deletion, chat modes). Rewritten in a sectioned, industry-standard form at the owner's standing request (plain and calm; OpenAI named as a service provider, as is usual), with every statement checked against the code; the consent gates (AGENTS §6 invariant 9) are described as they are. Also names the uptime monitor (Healthchecks.io, in use since M02; its ping carries no data) and narrows "deleted in Telegram" to "deleted for everyone" (TF finding of 2026-10-26: a delete-for-me sends no update). This **widens** what is kept (notes and summaries outlive the messages), so per AGENTS §16 it needs an owner notice with opt-out before it takes effect: `POLICY_VERSION = "v5"` with its `inbox/policy_notice.py` entry sends the one-time notice (link; `/privacy revoke`, `/privacy delete`, Chat Automation) in the first minute after the deploy, and the flags that create notes and summaries are turned on only after that (M03 owner guide). The per-person deletion ("ask Alviro") needs the flag `tool.delete_contact_data`, which the owner guide's flags line turns on in the same session. Publish order: owner go-ahead → page edited in place (`tools/legal/publish_policy.py`, or a manual paste of [privacy-policy-v5-telegraph.txt](privacy-policy-v5-telegraph.txt)) → deploy of the v5 code. `/privacy` now also says that notes and summaries are kept until deleted.
