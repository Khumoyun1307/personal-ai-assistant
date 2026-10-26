You are checking an evaluation. You are given a list of open items from a private chat (questions the contact asked the owner that are still unanswered, and commitments still open), each with the chat messages it comes from, and a summary written for the owner in any language and script.

For each item, decide whether the summary tells the owner about it.

1. First find the line or lines of the summary that tell the owner the same question or the same promise, by the same person, even if reworded, shortened, translated or transliterated, and under any heading (a promise may be listed as a key fact, as long as it says who will do what). Copy them into `evidence`, exactly as the summary writes them. If there is none, `evidence` is empty.
2. Then set `surfaced`: true only if the evidence states the item correctly. Mentioning the topic without the open question or the promise does not count, and neither does a line that gets the person or the substance wrong.
3. Some items list key values (amounts, times, dates, names, places). The evidence must carry each of them with the same meaning, in any language, script or format: a translated weekday or month, another date or time format (18:00 for 6 pm, 2026-10-14 for 14 October), digits for a number written in words, and inflected words all count. A value that is missing or different means the item is not surfaced.

Return one entry per item id. If you are unsure, answer false. A human confirms every verdict that matters.
