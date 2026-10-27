# M03 — the owner's guide to finishing the milestone

*Written for the owner, in plain language. Last updated 2026-10-26. The agent
keeps this in step with [M03.md](M03.md). Do the steps in order; each line
says what to do and what you should see. Tell the agent the short phrase in
**bold** where a step has one.*

## Where things stand

- All M03 code is merged and running on the server (`main` at `9ed9280`):
  search, summaries and catch-up, notes, chat modes, retention and deletion.
  Every new feature is still **off** (its release flag), and AI is not yet on
  for your connected chats.
- This close-out adds: privacy policy **v5** (notes and chat summaries kept
  until you delete them, the retention and deletion controls), the flags line
  below, the NFR-04 speed measurement, and the [decisions page](M03-decisions.md).
- The checks run on **Alviro** (production): the dev bot cannot connect to a
  second account without a second Telegram Premium. Every message in the
  checks is one *you* send; the agent never sends anything to your chats. Use
  your second account's chat as the test chat; it is "Name" below.
- The quality gates still need your E1 review (dataset v1.1 and the review
  ledger, ~4 h); that is separate from this checklist.

## Setup (once)

1. **Merge the close-out PR** on GitHub, then tell the agent **"publish v5"**.
   The agent updates the policy page at the same link
   (`tools/legal/publish_policy.py`; the page belongs to that script's
   Telegraph account, so a browser paste is only the fallback:
   [privacy-policy-v5-telegraph.txt](../legal/privacy-policy-v5-telegraph.txt))
   and then deploys. *You see:* within a minute Alviro sends "Alviro's privacy
   policy was updated (version 5)…" with the link and the opt-outs.
2. Open `var\prod.env` in Notepad and add this line (replace an existing
   `FEATURE_FLAGS=` line if there is one), save, and tell the agent **"flags
   added"**; it runs `just prod-env-push` and redeploys:

   ```text
   FEATURE_FLAGS=tool.get_status,tool.search_messages,tool.resolve_contact,tool.get_conversation,tool.list_conversations,tool.summarize_period,tool.get_attention_list,summary.refresh,tool.remember,tool.forget,tool.list_memories,tool.set_conversation_mode,tool.update_preferences,tool.delete_conversation_data,tool.delete_contact_data,tool.delete_everything
   ```

   *You see:* the agent reports the deploy healthy; `/status` answers as before.
3. Send `/settings timezone Asia/Tashkent` (or your own zone), then
   `/privacy managed` → **Turn on**. This lets Alviro's AI (OpenAI) read the
   messages saved from your connected chats to search, summarize and answer,
   and keep a summary of each chat; `/privacy revoke` turns it off any time.
   *You see:* the time zone confirmed; `/privacy` shows "AI for your chats with
   other people: granted (policy v5, processor: OpenAI)".

## Checks (your own messages; about 20 questions in all, which is also the NFR-04 sample)

4. **Test traffic.** From your second account send about six messages in
   Uzbek Cyrillic to your main account: a meeting time, an address, a
   question, a promise ("I'll send the file tomorrow"). Answer two of them
   from the main account and leave the last one unanswered.
5. **Search** (5 questions). Ask in Uzbek Latin, e.g. "Name bilan qachon
   uchrashamiz deb kelishgandik?", "Name qaysi manzilni yuborgan?", "What's
   going on with Name?", and two of your own. *You see:* each answer names the
   person, the day and time (your time zone) and quotes the message; nothing
   invented.
6. **Catch-up** (3 questions, best after half a day of normal use): "What did
   I miss since 9:00?", "Summarize today in my chats", "What happened between
   12:00 and 15:00?". *You see:* chats needing you first, then promises, then
   the rest, with quotes; every coverage note (e.g. "before you connected…")
   is passed on; asking the same again gives the same content.
7. **"Who is waiting for my reply?"** *You see:* Name's chat (last message
   unanswered) with a reason; chats it could not check are named as such.
8. **Notes** (4 messages): "Remember that Name handles the design" → a
   confirmation card first ("Confirm a change?"), naming Name → **Confirm**
   → the saved note with **Undo**, then "✅ Done"; "What do you know about
   Name?" → includes the note; "Forget that" → again a card first →
   **Confirm** → confirmed with Undo; tap **Undo** → the note is back.
   *Why a card:* to find Name the assistant looks the contact up, and the
   result carries the name Name chose for themselves — other people's text —
   so any change in that request waits for your tap (the safety rule that
   other people's text never changes anything on its own; the answers to
   steps 5–7 in the recent history count too). Since the C2 fix, saving a
   note about a person works whether the assistant passes the person, their
   chat or both. If a step still says saving or reading failed, the server log has
   one `agent_tool_call_failed` line per failed call (the tool and a code,
   no text): `just prod-logs <ip>`.
9. **Forwarded text.** Forward one of Name's messages to Alviro, then send
   "Remember what I just forwarded". *You see:* a confirmation card instead
   of a saved note (the same card as in step 8: both requests carry other
   people's text);
   tap **Reject** → nothing saved. Then `/status` → the same status as before.
10. **Deleted message.** In Name's chat, delete the address message *for
    everyone*. A minute later ask "Which address did Name send?". *You see:*
    the address is no longer mentioned (or "left out, ask again shortly").
11. **Chat mode.** Open Name's chat panel (Settings → Chat Automation →
    Alviro → Manage Bot, or `/settings` → the chat) → **Ignore** → **Ignore**.
    From the second account send "ignore test". Open the panel again. *You
    see:* the earlier summary and notes, no new text. Tap **Observe**.
12. **Settings and cards.** `/settings` → Retention… → Msgs 30d → it asks
    first ("Keep 30 days" / Cancel) → **Cancel**. `/settings` → Replies… →
    Formal (set it back if you like). `/privacy delete` → **Cancel**. Ask "Delete my chat data with Name" → a card names the chat and
    counts its messages → **Reject**. *You see:* nothing changed or deleted.
13. **Deleting while it answers.** Ask "Summarize my chat with Name today" and
    at once open Name's panel → Delete stored data… → **Chat + notes**. *You
    see:* the panel's first line says you asked to delete this chat's data
    (no name, summary or notes shown); the answer arrives first, then the
    receipt (counts, a line about backups); the panel's line still reads true
    afterwards; opening the panel again shows nothing stored.
14. *Optional — deletes everything Alviro stored (settings and consents
    stay).* Ask "Delete everything you stored", tap **Confirm**, and at once
    send "What can you do?". *You see:* the receipt, then an answer to the
    question (not "please send it again"). Skip it if you want to keep the
    stored data; automated tests cover it.

## Finish

15. Tell the agent **"checks done"** and note any step that looked wrong. The
    agent runs `just prod <ip> nfr04` (answer times only, no text) and
    records p95 for simple answers (< 20 s) and catch-ups (< 60 s) in M03.md;
    if fewer than 20 questions were counted it asks for a few more everyday
    ones. Then reply to the [decisions page](M03-decisions.md) ("confirmed"
    or exceptions), read M03.md's close-out section, and reply **"M03 OK"**.
