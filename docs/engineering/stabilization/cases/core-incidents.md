# Four reported-pattern families — synthetic conversations

Use [runner rules](README.md). The review cannot establish each private trajectory, so this maps its four recurring owner-facing failure families to deterministic state expectations. New-topic/lost-instruction variants also appear in J04/J05. All strings below are synthetic. Preserve source apostrophes, line breaks, `X1`, case and punctuation exactly.

## I01 — Dictation survives a real recipient clarification

Setup: two observed contacts named Kamola, surnames Ziyo and Safo; two separate chats. No live cards. Both consents active. Execute owner 1 → actual recipient question → restart → owner 2. Select **Kamola Ziyo**, never Safo. Text oracle is exactly the substring after the first `: ` in owner 1, with no added quotes or punctuation.

| Locale | Owner 1 | Owner 2 | Required artifact |
|---|---|---|---|
| uz-Latn | `Kamolaga aynan yoz: X1 — uchrashuv bekor. Oʻzgartirma!` | `Kamola Ziyo.` | `X1 — uchrashuv bekor. Oʻzgartirma!` |
| uz-Cyrl | `Камолага айнан ёз: X1 — учрашув бекор. Ўзгартирма!` | `Камола Зиё.` | `X1 — учрашув бекор. Ўзгартирма!` |
| ru | `Напиши Камоле дословно: X1 — встреча отменена. Не меняй!` | `Камола Зиё.` | `X1 — встреча отменена. Не меняй!` |
| en | `Write to Kamola exactly: X1 — meeting cancelled. Keep this!` | `Kamola Ziyo.` | `X1 — meeting cancelled. Keep this!` |

Checks: same request/source before/after restart; recipient slot filled; one owner_dictated v1 card, **Your words** equivalent, exact copied Unicode; no request to paste again; zero nested writer calls; zero sends. Then tap Approve v1 once and replay tap: one committed claim/attempt, exact approved body; no second send. Repeat with a model attempting `x1`/`Mah`/folded punctuation: cannot change copied body. Source cleared/foreign variants must fail explicitly.

## I02 — Missing substance is a question, then continuation

Setup: Kamola Ziyo uniquely observed; old chat asks about an unrelated invoice. No card. Execute owner 1 → actual content question → owner 2 → card. Do not answer the invoice unless owner asks.

| Locale | Owner 1 | Owner 2 | Quality oracle |
|---|---|---|---|
| uz-Latn | `Kamola Ziyoga yoz.` | `Juma kungi kitob yetkazilishini soʻra. 20 soʻzdan oshmasin.` | Ask delivery of books on Friday, ≤20 words. |
| uz-Cyrl | `Камола Зиёга ёз.` | `Жума кунги китоб етказилишини сўра. 20 сўздан ошмасин.` | Same meaning in chosen script. |
| ru | `Напиши Камоле Зиё.` | `Спроси о доставке книг в пятницу. Не больше 20 слов.` | Same topic, ≤20 words. |
| en | `Write to Kamola Ziyo.` | `Ask about Friday's book delivery. At most 20 words.` | Same topic, ≤20 words. |

Checks after owner 1: selected recipient + missing_slot=content, no action/card. After owner 2: same request, original/source constraints retained, standalone new_message, answered_message_id=NULL, assistant_written v1, full text/recipient shown, **Written by the assistant** equivalent. Never invoke draft_owner. Advice-only follow-up creates no second card.

## I03 — Unique real contact does not compete with weak suggestions

Setup: Kamola Ziyo, plus weak nickname/prefix alternative Kamariddin Nur. One adequate full-token Kamola match. No other Kamola. Run both turns; do not let recency pick a weak candidate.

| Locale | Owner 1 | Owner 2 | Required outcome |
|---|---|---|---|
| uz-Latn | `Kamolaga aynan yoz: Kitob keldi, X1.` | `Kimga tayyorlading?` | Ziyo selected with no recipient question; later report recipient from read facts. |
| uz-Cyrl | `Камолага айнан ёз: Китоб келди, X1.` | `Кимга тайёрладинг?` | Same selected IDs. |
| ru | `Напиши Камоле дословно: Книга пришла, X1.` | `Для кого подготовлено?` | Same selected IDs. |
| en | `Write to Kamola exactly: The book arrived, X1.` | `Who is the draft for?` | Same selected IDs. |

Checks: verdict unique_strong computed before page cap; no unnecessary question; binding survives next turn; exact body after `: `; no alias saved; no second card/send. Countervariant: replace directory with two real Kamolas ⇒ ask between those real matches. Only typo/short-form ⇒ weak_only and ask; do not silently reuse a recipient from a different request.

## I04 — Chat reports the actual card, version and outcome

Setup: two conversations (Kamola Ziyo and Nora Vale), each with a generated v1 card from actual earlier turns. Alias them `k` and `n`. User replies directly to `k`'s control message. Revise `k` to v2; `n` unchanged. Between owner turns, reject `k` v2. On the final question, force an artifact read. Do not infer text from proposal instructions.

| Locale | Owner 1 (reply_to=k) | Owner 2 (after reject k v2) | Required narration |
|---|---|---|---|
| uz-Latn | `Buni qisqaroq qil, 10 soʻzdan oshmasin.` | `Oʻsha xabar nima boʻldi va unda nima yozilgandi?` | Rejected, not sent; actual retained k v2 content only. |
| uz-Cyrl | `Буни қисқароқ қил, 10 сўздан ошмасин.` | `Ўша хабар нима бўлди ва унда нима ёзилганди?` | Same factual meaning/script. |
| ru | `Сделай это короче, не больше 10 слов.` | `Что стало с тем сообщением и что в нём было?` | Rejected, not sent; actual k v2 text. |
| en | `Make this shorter, at most 10 words.` | `What happened to that message, and what did it say?` | Rejected, not sent; actual k v2 text. |

Checks: explicit reply association selects k/presented v1; new version/token only on k; no draft_owner call; rejected terminal receipt injected despite no live card; get_draft binds content to k v2. Race variant: outdate after tool result before final answer ⇒ final reports outdated reason; artifact purge ⇒ unavailable text, no reconstruction; uncertain card presentation ⇒ “delivery uncertain”, never “shown”. Stale k v1 token cannot approve v2. Owner may explicitly recover controls under I27 without a business-send attempt.
