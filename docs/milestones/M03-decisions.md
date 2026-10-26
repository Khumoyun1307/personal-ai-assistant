# M03 — decisions waiting for your confirmation

*For the owner. Written 2026-10-26 at the M03 close-out. Every decision the
agents made while building M03 that is still marked "pending owner
confirmation" in [M03.md](M03.md) (D60–D76, D78–D94 and E-a…E-y; D77 is a
test-suite choice and not listed), in plain words, with a recommendation.
The full reasoning is in M03.md under the same number.*

**How to answer:** reply **"confirmed"** to accept every recommendation, or
**"confirmed except …"** naming the numbers you want changed (for example
*"confirmed except E-x: back to v2"*). Nothing below changes until you answer;
the code already behaves as described.

## The five that are real choices

| # | The choice | Recommendation |
|---|---|---|
| **E-x** | **Which summary prompt is live.** `summarize.v4` has been active since PR #25 merged (deployed 2026-10-26). It writes every summary in *your* language and script, whatever the chat's (eval: Cyrillic chats 0/8 → 8/8, all chats 31/40 → 40/40). Side effects: a contact's name may appear in your script ("Нодира" → "Nodira"), and what people wrote is retold ("she asked…") instead of quoted. The AI pre-screen for invented facts flagged 24 statements (v2: 15); about 16 look supported by the messages, none is confirmed yet, and the judge's input changed between the runs. The alternative is `v2` (a one-line change back): names and quotes kept as written, but Cyrillic chats summarized in Cyrillic. | **Keep v4.** The language fix is what the eval demanded; your E1 review of the 24 flags decides whether a v5 prompt is needed. |
| **E-k** | **How much of the test set you must confirm before a quality gate can pass.** Now: every item (38 search queries, 40 conversations, ~4 h of Uzbek review). A smaller confirmed subset would be faster but would be a deliberate change to the plan, and a pass would say less. | **Keep the whole set.** |
| **E-p** | **Whether "the final answer relays every coverage warning" must be measured before M03 closes.** The eval checks that the warnings are produced (they come from code), not that the model's final reply repeats them. A separate agent-level eval would cost a new paid run. | **Accept for M03**; the catch-up step of the owner guide checks it by eye. Revisit if a warning is ever missing. |
| **E-c** | **How many summaries the AI judge did *not* flag you spot-check** for invented facts. A summary the judge passed counts as clean unless you say otherwise. | **Spot-check 5** at random during the E1 review. |
| **D61** | **Background summaries** (flag `summary.refresh`): with it on, each active chat's summary is refreshed after 30 quiet minutes (one cheap call), messages are folded into it before they expire at 90 days (D72), and the chat panel shows a current summary. With it off, summaries are made only when you ask, and context older than 90 days is lost. | **Turn it on** (it is in the flags line of the owner guide); spend stays under `LLM_MONTHLY_BUDGET_USD`. |

## Summaries and catch-up

| # | What it means for you | Recommendation |
|---|---|---|
| D60 | A chat's summary refreshes after 30 quiet minutes, at the latest 6 hours after its first new message; a burst of messages costs one call. | Keep |
| D62 | "Who is waiting for my reply?" uses each chat's summary; up to 8 out-of-date chats are re-summarized on the spot (one call each); others are listed as "not checked". | Keep (M05 triage improves it) |
| D63 | A message deleted in Telegram disappears from summaries too (they are rebuilt). If a chat changes while being summarized, you get "left out, ask again shortly", never a wrong or empty summary. | Keep |
| D64 | A catch-up summarizes each active chat separately (up to 15 chats, newest 300 messages each) and code combines them: chats needing you first, then promises, then the rest; coverage warnings are written by code and must be relayed. | Keep |
| D65 | A summary point that cites no real message is dropped; after 3 bad answers in 24 h a chat's background refresh pauses until the day is over. | Keep |
| D66 | An edited message counts like a deleted one: the summary is rebuilt with the new text. | Keep |
| D72 | With AI on for your chats and background summaries on, messages are folded into the chat summary 10 days before they expire; otherwise answers say "earlier messages expired without a summary". | Keep |
| D75 | Summaries see your notes about that person or chat as background, never as something the contact said. | Keep |

## Notes (memory)

| # | What it means for you | Recommendation |
|---|---|---|
| D67 | "Remember …" saves your words as you wrote them (up to 400 characters), never a paraphrase; a correction replaces the old note. | Keep |
| D68 | Every change the agent makes (notes, chat modes, settings) is shown to you with a one-tap Undo, valid 24 h; Undo never overwrites a later change. | Keep |
| D69 | "Forget …" hides a note at once and erases it after 24 h, so Undo can bring it back. | Keep |
| D70 | A note saved while the agent was reading someone else's text (a forward, chat content) is marked as coming from other people: it is not preloaded, needs AI on for your chats, and any further change in that conversation turn asks for a tap. Cost: now and then an extra confirmation card. | Keep (safety rules 3b and 9) |
| D71 | Deleting your connected chats' data also deletes notes about those people and notes drawn from those chats; your own general notes stay. | Keep |

## Chat modes and settings

| # | What it means for you | Recommendation |
|---|---|---|
| D73 | New chats start in **Observe** (messages saved); in /settings you can make new chats start in **Ignore** (nothing new saved, not even the name). | Keep Observe, unless you prefer to switch chats on one by one |
| D74 | On a chat's panel, Observe applies at once and Ignore asks first; bot chats have no mode buttons. The /settings chat lists can shift by a row while chats are active. | Keep |
| D76 | The agent can change your language, time zone and default mode (plus the D81 reply defaults and retention); wishes about style in your own words are kept as notes. | Keep |
| D81 | Reply defaults (language: match the contact, or a fixed one; tone: neutral / friendly / formal; length: brief / detailed) are saved now and used for drafts from M04. | Keep |

## Retention

| # | What it means for you | Recommendation |
|---|---|---|
| D80 | You can only **shorten** how long data is kept: messages and your chat with Alviro 14–90 days, raw Telegram updates 1–7 days; summaries and notes stay until you delete them; the activity log 1 year. Longer periods would need a policy change. | Keep |
| D85 | A shorter period asks first on the panel and always on an agent card; a retention change has no Undo (change it again in /settings; deleted data does not come back). | Keep |

## Deleting data

| # | What it means for you | Recommendation |
|---|---|---|
| D78 | "Delete everything" removes all stored content but keeps your account, settings, consents and connection, so Alviro keeps working and new messages are saved again. | Keep |
| D79 | Deleting one chat or one person keeps the empty chat/contact entry, so an Ignore choice survives; deleting a chat keeps your own notes about the person. | Keep |
| D82 | Deletions the agent proposes always show a card; your own buttons ask once, in place. | Keep |
| D83 | A button deletion waits for any answer in progress, so that answer cannot keep the deleted text; the receipt comes a moment later. | Keep |
| D84 | Cards and receipts say what stays: a message arriving at that very moment, encrypted backups up to 35 days, other notes about the person. | Keep |
| D86 | A catch-up over a period with a deletion says that earlier messages were deleted at your request. | Keep |
| D87 | A failed deletion is reported ("nothing was deleted, please ask again"), never retried behind your back. | Keep |
| D88 | A question you send while a deletion is waiting keeps its text and is answered. | Keep |
| D89 | Deletions also clear failed control-chat updates; `inbox requeue` skips cleared rows and says how many. | Keep |
| D90 | A deletion's outcome is recorded once, before you are told; a repeated run does nothing; a server shutdown puts it back in the queue (up to 3 times). You get at most one receipt (rare cases lose it: see *Known follow-ups* in M03.md). | Keep |
| D91 | A deletion also clears the warning about messages that could never be recovered and lifts that chat's pause. | Keep for now; revisit in M06 (follow-up 4) |
| D92 | After a failed deletion the message names the way to try again that works for that case (the chat's panel, `/privacy delete`, or a fresh Delete button). | Keep |
| D93 | Deleting a chat or person also clears changes held by an answer that was cut off; rarely an unrelated pending card expires too. | Keep |
| D94 | The same "question after the tap keeps its text" holds when you confirm a deletion on an agent card. | Keep |

## Evaluation harness (S03.4)

| # | What it means for you | Recommendation |
|---|---|---|
| E-a | Only items you confirmed count for a gate; until then every gate reads "not evaluable". | Keep |
| E-b | An open question or promise counts as covered only if all its key values appear (loosened by E-s); 42 have no key values, so the judge and then you decide. | Keep; adding values in E1 v1.1 saves review time |
| E-d | Coverage warnings are checked as structured data; a warning for a gap that lost nothing counts as an error. | Keep |
| E-e | The language check is mechanical (80 % of letters in your script, common words); it errs on strict. | Keep |
| E-f | The search eval measures the model's search phrases plus the search engine, not the whole agent. | Keep |
| E-g | A correct answer needs a real quote, the right contact and day, and every expected value, word for word. | Keep |
| E-h | A failed model call counts as a miss; queries run at a fixed time so results repeat. | Keep |
| E-i | Every paid run is capped (default USD 1) by a worst-case estimate before it starts and a hard stop during it. | Keep |
| E-j | Eval runs use only synthetic chats, so they skip the consent check (there is no owner data in them). | Keep |
| E-l | The 85 % search target counts only with model-written phrases; trial or stopped runs can never pass. | Keep |
| E-m | A quote must be at least 8 letters/digits long, or the whole message. | Keep |
| E-n | The summary eval runs the real catch-up path, one synthetic account per conversation. | Keep |
| E-o | Eval reports keep the (synthetic) summary text so you can review it. | Keep |
| E-q | The eval asks for summaries in `uz-Latn` (your `/settings language` value). | Keep if Uzbek Latin is your language; otherwise tell the agent which |
| E-r | A failed summary is a miss on every metric; your verdicts are tied to the exact summary text you judged. | Keep |

## Eval-driven tuning (S03.6)

| # | What it means for you | Recommendation |
|---|---|---|
| E-s | A key value found word for word (Uzbek endings allowed) covers a question or promise; otherwise the judge and you decide, and your verdict always wins. | Keep |
| E-t | The search eval narrows by chat partner like the agent does; its 34/38 assumes two small agent-prompt changes not yet shipped (a follow-up in M03.md). | Keep, with the follow-up |
| E-u | In the answer check, Uzbek endings are allowed only inside an exact quote. | Keep |
| E-v | The language check no longer splits Uzbek words at apostrophes (`to‘lov` is not English "to"). | Keep |
| E-w | The judges read times in your time zone; the open-question judge quotes the summary line it relied on. | Keep |
| E-y | Search ignores words like "is"/"the" only when the question is clearly in that language, and no longer cuts names (Aziza stays Aziza). | Keep |
