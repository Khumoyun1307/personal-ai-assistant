# Privacy policy

| | |
|---|---|
| **Version** | **v6** (P3: M04–M07, OD-24) — adds drafts and replies you approve, chat triage, tasks, alerts, the daily digest and monitors (M04–M05); **delegations** with automatic replies whose AI notice can't be removed, and the knowledge you allow Alviro to share in them (M06); reminders, to-dos, follow-ups, scheduled summaries, **note suggestions** you review (never the sensitive categories, unreviewed ones deleted after 14 days), `/export` and the weekly rating (M07); the reachability record. Replaces v5's "doesn't send messages to other people on its own" (decisions conflict 6). No new service provider |
| **Status** | **Draft, awaiting the owner's approval** (OD-24, by D+3 = 2026-11-05). Not published; the published page still shows v5. Publish order (PV6 §3.4): owner approval → page edited in place at the same URL with `tools/legal/publish_policy.py` (or a manual paste of [privacy-policy-v6-telegraph.txt](privacy-policy-v6-telegraph.txt)) → live page checked for version 6 → deploy #1, which carries `POLICY_VERSION = "v6"` and sends the one-time notice (`POLICY_CHANGES["v6"]`, text in [PV6 §3.2](../engineering/p3/specs/PV6.md)). Enables 4 (`autonomy.send`) and 5 (`memory.extract`) wait until the notice is ≥ 24 h old. **Code is at v5** until then. v5: published 2026-10-26 at https://telegra.ph/Personal-AI-assistant--privacy-policy-v1-10-19 (title "Alviro — Privacy Policy"; plain-text copy: [privacy-policy-v5-telegraph.txt](privacy-policy-v5-telegraph.txt)) |
| **Last updated** | 2026-10-29 (v6 drafted) |
| **Previous** | v5 — professional, sectioned rewrite; the owner's notes and chat summaries kept until deleted, retention settings (shorten only), chat modes, deletion controls; names OpenAI, Oracle, Backblaze, Telegram and the uptime monitor, published 2026-10-26; v4 — plain-language rewrite, hosting (Oracle Cloud, US East — Ashburn) and encrypted backups (server + Backblaze B2, EU; deleted data in backups at most 35 days), published 2026-10-24; v3 — deletion and "stop observing", published 2026-10-21; v2 — control chat + managed chats, published 2026-10-19; v1 — control chat only, published 2026-10-18 (see git history of this file) |

Everything between the two `---` lines below is the published text. Change it
only with a version bump (AGENTS §16); a change that widens processing needs an
owner notice with opt-out before it takes effect. v2 widens processing to
chats included under Chat Automation; the opt-in is the separate consent asked
when the connection arrives (FR-ONB-05b), and nothing from those chats reaches
an AI provider without it. v6 widens processing again (automatic replies under
delegations, note suggestions, and the M04–M07 data classes); its notice is
`POLICY_CHANGES["v6"]`, and autonomous sends and note suggestions start no
earlier than 24 hours after it (OD-24). `[publication date]` below is filled in
when the page is published.

---

## Alviro — Privacy Policy

Last updated: [publication date] (version 6)

Alviro is a personal assistant on Telegram, run by its owner for their own use. In this policy, "you" means the owner. It covers your chat with Alviro and the chats you connect to it through Telegram's Chat Automation, and explains what Alviro keeps, why, who helps process it, how long it is kept, and how you can change or delete it.

## What changed in version 6

- Replies: Alviro can prepare replies for you to approve, and, in chats you delegate to it, reply automatically within the limits you set. The other person is always told that an AI is replying, and you can't turn this off.
- Organising: Alviro notes which chats need you, keeps track of tasks and commitments, sends alerts and a daily digest, and runs the reminders, follow-ups, scheduled summaries and monitors you set.
- Notes: Alviro can suggest notes from your chats, which it keeps only if you approve them.
- Your data: /export sends you a copy of the data Alviro stores (except short-lived technical copies of Telegram updates).

This version adds no new service provider. Each item is described below.

## Information Alviro keeps

- Account and settings: your Telegram account ID and your settings, such as language, time zone, reply preferences, how long data is kept, and whether new chats are observed. Kept while you use Alviro.
- Your chat with Alviro: your messages, including text you forward or quote, and Alviro's replies, so it can follow the conversation.
- Messages in the chats you connect: the text and basic details such as who sent it, when, and whether it was edited, from the moment you connect Alviro. Alviro can't see earlier messages, or chats you didn't include. When a message is deleted for everyone in Telegram, Alviro deletes it too, and rebuilds any summary that included it.
- Names of the people in those chats: their names and usernames as Telegram shows them.
- Chat summaries: short summaries of each connected chat, written by Alviro's AI so it keeps the context after the messages themselves expire, and summaries of a period you ask about ("what did I miss?"). They are made only while AI is on for your connected chats.
- Notes: things you ask Alviro to remember, such as your instructions, preferences and notes about people.
- Note suggestions: notes Alviro proposes from your connected chats, kept for your review until you approve, edit or discard them.
- Drafts and replies: replies Alviro prepares for you, including ones it held back for you to review, the replies you approve or send through Alviro, the automatic replies it sends in delegated chats, and what happened to each (sent, rejected, expired, or not confirmed by Telegram).
- Chat triage, tasks and alerts: short notes on which chats need your attention and why, tasks and commitments found in your chats, the alerts and daily digest Alviro sends you, and the monitors you set (for example "tell me when this person writes").
- Delegations: the delegations you confirm — which chats, the topic, what Alviro may do, the limits, the wording of the AI notice, and when the delegation starts and ends — and the knowledge you allow Alviro to share in them.
- Reminders and automations: reminders, to-dos, follow-ups, scheduled summaries and other automations you set.
- Connection details: the permissions you gave Alviro in Telegram and when they changed.
- Technical records: an activity log of what Alviro did and why, including what it sent, and records of AI usage and cost, which contain no message text; your weekly rating of how useful Alviro was, kept in that activity log; a minute-by-minute record that Alviro was reachable, which holds no information about you or your chats; short-lived copies of incoming Telegram updates, used only to recover from errors; and a change Alviro asks you to confirm, held until you tap Confirm or Reject.

## How Alviro uses it

Only to help you: answering your questions, searching and summarizing your chats, telling you who is waiting for a reply, keeping track of tasks, sending you alerts, reminders and summaries, preparing replies, remembering your notes and settings, and showing you what it stores. Alviro doesn't sell your data or use it for advertising, and doesn't use it to train AI models.

Messages to other people: Alviro sends a message to someone only when you approve it on a card, or, inside a delegation you confirmed, as an automatic reply. In a delegated chat, Alviro tells the other person that an AI is replying: on its first automatic reply in each delegation, and again whenever 24 hours have passed since its last automatic reply in that chat. You can change the wording of this notice for each delegation, but you can't remove it. Alviro replies only to messages people send you; it never starts a conversation and never claims to be you or a person. It stops replying automatically in a chat as soon as you write there yourself, and everywhere as soon as you tap Pause or send /pause. If Alviro has a reply but can't send it automatically (for example because a limit or a rule stops it), it tells you at once, and you can open the reply to approve, edit or reject it. When a message needs your decision, Alviro alerts you instead of replying.

Note suggestions: Alviro uses a suggested note only after you approve it, and never in replies before that. It never suggests or keeps notes about health, religion, politics, sexuality, finances (including debts and salary), ethnicity or race, legal or criminal matters, union membership, biometric or genetic data, immigration status, or a precise home address or location. This limit applies to notes Alviro infers; notes you ask it to remember are kept as you wrote them.

## Service providers

Alviro relies on a few providers, each only for the part of the service described here:

- OpenAI processes text to produce Alviro's answers, summaries, notes and note suggestions, triage, alerts, draft replies and automatic replies. Your chat with Alviro is processed after you agree to it when you first start the bot. Content from your connected chats (their messages, and the summaries, notes, drafts and replies based on them) is processed only after you turn this on separately, when you connect Alviro or later with /privacy managed; this includes the messages people send in a delegated chat, which OpenAI processes to write the automatic replies. Under OpenAI's API terms, this data isn't used to train their models; OpenAI may keep requests for up to 30 days to check for abuse, then deletes them. More information: https://developers.openai.com/api/docs/guides/your-data
- Oracle Cloud hosts the server, in the United States (Ashburn, Virginia), on an encrypted disk.
- Backblaze B2 stores a second copy of the encrypted backups, in the EU.
- Telegram delivers all messages, under its own privacy policy, including the replies you approve and the automatic replies people receive in a delegated chat.
- Healthchecks.io receives a regular signal that Alviro is running. The signal carries no information about you or your chats.

Alviro doesn't use analytics, advertising or tracking services.

## How long it is kept

- Messages in your connected chats: 90 days, or a shorter period you choose (14 to 90 days).
- Your chat with Alviro: 90 days, or a shorter period you choose (14 to 90 days).
- Chat summaries: until you delete them. Summaries of a period you asked about: 30 days.
- Notes: until you delete them. A note you ask Alviro to forget is no longer used at once and is erased after 24 hours, so that you can undo it. An approved note suggestion becomes a note.
- Note suggestions you haven't reviewed: 14 days.
- Names of people: until no message from them is kept any more.
- Drafts and replies: the text, as long as messages in your connected chats (90 days, or the shorter period you chose); the record of what happened to each, 1 year.
- Chat triage notes: as long as messages in your connected chats.
- Tasks and reminders: open ones until they are done or deleted; tasks Alviro found in your chats are also closed automatically when they go stale (a question or decision 14 days after the last message about it; a commitment 7 days after its due date, or 30 days after it was found if it has none). Closed ones: 1 year.
- Automations, monitors, repeating reminders and follow-ups: until they end or you cancel them, then 1 year.
- Alerts: 30 days. The record of each daily digest, without its text: 1 year.
- Delegations: until they end, then 1 year. Knowledge you enter for one delegation: until that delegation ends. Knowledge you save to share: until you delete it.
- Connection details, the activity log (including your weekly ratings) and records of AI usage: 1 year.
- The record that Alviro was reachable: 90 days.
- Copies of incoming Telegram updates: up to 7 days, or a shorter period you choose (1 to 7 days).
- A change waiting for your confirmation: until you decide, at most 24 hours.

Old data is deleted automatically, once an hour. You can shorten these periods but not make them longer than stated here.

## Where it is kept, and backups

Your data is kept on the server described above. Once a day the database is backed up; each backup is encrypted before it is stored, on the server and as a second copy with Backblaze B2 in the EU. Only the owner holds the key that can open the backups. Backups are kept for up to 5 weeks, so something deleted, by you or automatically, can remain in an older backup until that backup expires (normally no more than 35 days). If the database ever has to be restored from a backup, every delegation ends and Alviro tells you which ones; it never resumes automatic replies by itself.

## Your choices

- /privacy shows your current settings and this policy.
- /privacy managed turns on AI for your connected chats; /privacy revoke turns AI processing off. Revoking takes effect at once; features that don't need AI, such as /status and /settings, keep working.
- /settings lets you choose how long messages are kept, the language and time zone, reply preferences, and whether new chats are observed.
- For each chat you can choose Observe or Ignore on its panel. Ignore stops Alviro saving the content of new messages from that chat; what is already stored stays until you delete it.
- You can ask Alviro what it knows about someone, and ask it to forget a note. Alviro tells you each time it saves or forgets a note, with a button to undo it.
- Each note suggestion comes on a card: approve, edit or discard it.
- A reply Alviro prepares for your approval is sent only after you approve it on its card; you can also edit or reject it.
- A delegation starts only after you confirm it on a card. You can end it at any time with End (in /settings, or by asking Alviro).
- /pause, or the Pause button, stops all automatic replies at once; replies you already approved on a card are still sent. Turning AI off for your connected chats, or disconnecting Alviro, also ends your delegations.
- When you write in a delegated chat yourself, Alviro stops replying automatically there and tells you once; tap Resume here on that message if you want it to continue.
- /export sends you a copy of your data as files in this chat.
- You can disconnect Alviro or remove chats at any time in Telegram: Settings → Chat Automation.

## Deleting your data

- One chat: open the chat's panel and choose Delete stored data (the chat's data, or the chat's data and the notes about it), or choose Ignore together with deleting its data.
- One person: ask Alviro to delete what it stored about a person; it asks you to confirm first.
- Deleting a chat's or a person's data also ends the delegations that name them, deletes their tasks, and deletes or cancels their reminders and follow-ups.
- Everything: /privacy delete, or /settings → Delete everything, removes all stored messages, summaries, notes and names, the text of your chat with Alviro, the text of drafts and replies, your tasks, reminders, automations and delegations, and cancels replies not yet sent. Your account, settings and consents, the connection, your daily digest setting, the record of what happened to each reply (without its text) and the activity log (which holds no message text) stay, so Alviro keeps working; new messages are saved again unless you set chats to Ignore or disconnect Alviro.
- When you turn AI off for your connected chats, Alviro offers to delete what it stored from them. That deletion also ends your delegations and deletes the notes Alviro inferred.
- A file sent to you by /export stays in this chat's history until you delete it in Telegram; Alviro can't delete it.

Before deleting at your request, Alviro asks you to confirm, and afterwards it tells you what was deleted. Deleting can't be undone; deleted data may remain in an encrypted backup until it expires, as described above.

## Other people in your chats

The chats you connect include messages from other people. Alviro stores and uses them, and any notes or summaries about them, only to help you, as described above.

People in your chats receive automatic replies only in a chat you delegated, and they are told that an AI is replying. If someone sincerely asks whether they are talking to a bot or a person, they are never told it is a person: they get a truthful answer, or no automatic reply, in which case you are alerted.

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
7. v5 drafted 2026-10-26 (M03 close-out). Closes the gap M03.md recorded since S03.3: v4 named neither the owner's notes (`memory_items`) nor the rolling chat summaries kept beyond the 90-day message limit (both "until deleted"; catch-up period summaries 30 days), nor the S03.5 controls (retention that only shortens, per-chat/per-contact/everything deletion, chat modes). Rewritten in a sectioned, industry-standard form at the owner's standing request (plain and calm; OpenAI named as a service provider, as is usual), with every statement checked against the code; the consent gates (AGENTS §6 invariant 9) are described as they are. Also names the uptime monitor (Healthchecks.io, in use since M02; its ping carries no data) and narrows "deleted in Telegram" to "deleted for everyone" (TF finding of 2026-10-26: a delete-for-me sends no update). This **widens** what is kept (notes and summaries outlive the messages), so per AGENTS §16 it needs an owner notice with opt-out before it takes effect: `POLICY_VERSION = "v5"` with its `inbox/policy_notice.py` entry sends the one-time notice (link; `/privacy revoke`, `/privacy delete`, Chat Automation) in the first minute after the deploy, and the flags that create notes and summaries are turned on only after that (M03 owner guide). The per-person deletion ("ask Alviro") needs the flag `tool.delete_contact_data`, which the owner guide's flags line turns on in the same session. Publish order: owner go-ahead → page edited in place (`tools/legal/publish_policy.py`, or a manual paste of [privacy-policy-v5-telegraph.txt](privacy-policy-v5-telegraph.txt)) → deploy of the v5 code. **Published 2026-10-26** (page edited in place with `tools/legal/publish_policy.py`, at the same URL). `/privacy` now also says that notes and summaries are kept until deleted.
8. v6 drafted 2026-10-29 (P3 pre-start, PV6; OD-24). One version covering M04–M07: drafts and replies the owner approves and their outcomes, chat triage, tasks, alerts, the daily digest and monitors (M04–M05, already built behind flags and not yet named by v5); delegations, automatic replies with the app-inserted AI notice (wording the owner may change but never remove, OD-4; re-shown per OD-32), held replies with the Draft reply button (OD-11), shareable knowledge (M06); reminders, to-dos, follow-ups, scheduled summaries, note suggestions (never the OD-15 categories; unreviewed ones deleted after 14 days, OD-16), `/export` (the file stays in Telegram, OD-20) and the weekly rating (an activity-log entry, R59) (M07); the content-free reachability record (NFR-08, 90 days). States that restore and data deletion end delegations (OD-12) and that approvals survive `/pause` (OD-31). Replaces v5's "Alviro doesn't send messages to other people on its own" (decisions conflict 6). Retention is stated as built: the text of drafts and replies follows the message period (`actions.text`), the action rows and the activity log 1 year (ARCHITECTURE §10.4; P3-schema §6). No new processor (AGENTS §9). This **widens** processing (messages to other people, inferred notes), so per AGENTS §16 it needs an owner notice with opt-out before it takes effect: `POLICY_VERSION = "v6"` with `POLICY_CHANGES["v6"]` (text: PV6 §3.2) sends the one-time notice in the first minute after deploy #1, and enables 4 (`autonomy.send`) and 5 (`memory.extract`) are turned on only once that notice is ≥ 24 h old (PV6 §3.4). Owner approval: pending (due 2026-11-05). Publication: pending (before deploy #1, same URL, `tools/legal/publish_policy.py`; the live page is checked for version 6).
