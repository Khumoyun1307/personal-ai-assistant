# P3 autonomy texts: what contacts read (draft for the owner's proofreading)

Status: **draft, F0-C part 2 (P3-T61)**. The owner proofreads these texts before F0-S starts at D0 (at the latest before deploy #1, as a text-only ICR; specs/F0-S.md) (OD-4, OD-10, R24). F0-S then copies the approved texts verbatim into `src/assistant/core/autonomy_texts.py` (`AI_MARKER`, `DISCLOSURE_DEFAULTS`, `IDENTITY_ANSWERS`; P3-interfaces §5.2, specs/F0-S.md §3.8). After that, any wording change is a text-only ICR that needs the owner's OK.

## 1. What each text is for

| Text | Constant | Where it appears | Who writes it |
|---|---|---|---|
| AI marker | `AI_MARKER` | At the start of every autonomous reply that carries the disclosure line (next row) and of every identity answer; other autonomous replies in the same session carry neither. One marker for all languages. Never on drafts the owner approves (database CHECK, PRD §11.4) | Fixed by the app; the owner cannot remove it (OD-4) |
| Disclosure line | `DISCLOSURE_DEFAULTS[lang]` | After the marker, as a calm prefix line above the reply, on the first autonomous reply of each delegation session and again once 24 h passed since the last succeeded autonomous reply in that chat (OD-32) | The app inserts it, never the model (ADR-17). The owner may replace the line per delegation, but never empty it (OD-4) |
| Identity answer | `IDENTITY_ANSWERS[lang]` | Sent instead of a drafted reply when a contact sincerely asks whether they are talking to a bot or a person. If the delegation allows no reply, the owner gets an escalation instead (OD-10). When the sentence is sent, the owner gets no alert (OD-10a) | Fixed sentence; the model never writes it |

Message shapes (from `actions/disclosure.py`):

- Disclosed reply: `{AI_MARKER} {disclosure line}`, a blank line, then the reply body.
- Identity answer: `{AI_MARKER} {identity answer}` (one message, no body).

## 2. Rules every text follows

- Truthful: says an AI assistant is replying; never claims or implies a human is typing (FR-AUT-12, AGENTS §6 invariant 13).
- Calm and professional, written in the owner's voice ("my assistant"), not alarming.
- One line each; no names, digits, links, at-signs or placeholders.
- No `` < > & * _ ` [ ] ~ `` and no invisible (Unicode `Cf`) characters, so the disclosure lines pass `validate_wording`.
- The identity answer does not start with "yes" or "no", so it is true whatever form the question takes ("are you a bot?", "is this a real person?").
- Uzbek Latin uses the official letters: `oʻ` and `gʻ` with U+02BB (ʻ), and the tutuq belgisi with U+02BC (ʼ), as in `sunʼiy`. Both are letters (Unicode `Lm`), not `Cf`, so they pass the checks. Uzbek Cyrillic is an exact transliteration of the Latin text (ʼ becomes ъ; oʻ ў, gʻ ғ, q қ, h ҳ).

## 3. AI marker: two options (owner picks one)

| Option | `AI_MARKER` | Example (English) |
|---|---|---|
| A (with emoji; TL's suggestion) | `🤖` | 🤖 This reply was sent by my AI assistant. |
| B (no emoji) | `AI:` | AI: This reply was sent by my AI assistant. |

Both are neutral and read the same in all four languages. Option A is shorter and friendlier; option B avoids emoji for owners who prefer a plain style. The owner may also name another emoji.

## 4. Disclosure lines (`DISCLOSURE_DEFAULTS`)

| Key | Language | Text |
|---|---|---|
| `uz-Latn` | Uzbek (Latin) | Bu javobni mening sunʼiy intellekt yordamchim yubordi. |
| `uz-Cyrl` | Uzbek (Cyrillic) | Бу жавобни менинг сунъий интеллект ёрдамчим юборди. |
| `ru` | Russian | Этот ответ отправил мой ИИ-ассистент. |
| `en` | English | This reply was sent by my AI assistant. |

## 5. Identity answers (`IDENTITY_ANSWERS`)

| Key | Language | Text |
|---|---|---|
| `uz-Latn` | Uzbek (Latin) | Siz mening sunʼiy intellekt yordamchim bilan yozishyapsiz, u mening nomimdan javob bermoqda. Men bu yozishmani koʻraman va xohlasangiz, oʻzim javob beraman. |
| `uz-Cyrl` | Uzbek (Cyrillic) | Сиз менинг сунъий интеллект ёрдамчим билан ёзишяпсиз, у менинг номимдан жавоб бермоқда. Мен бу ёзишмани кўраман ва хоҳласангиз, ўзим жавоб бераман. |
| `ru` | Russian | Вы переписываетесь с моим ИИ-ассистентом, он отвечает от моего имени. Я увижу эту переписку и, если хотите, отвечу лично. |
| `en` | English | You are chatting with my AI assistant, which replies on my behalf. I will see this conversation and, if you prefer, reply personally. |

"I will see this conversation" is true because the conversation is in the owner's own Telegram account. The sentence promises no alert: the owner gets none when it is sent (OD-10a, decisions §A2).

## 6. What the owner checks

1. **Marker:** option A (🤖), option B (`AI:`), or another emoji.
2. **Naturalness:** each line reads like something you would write, in Uzbek (both scripts), Russian and English. Mark any word you would say differently (for example "sunʼiy intellekt" versus "AI", or "ИИ-ассистент" versus "ИИ-помощник").
3. **Tone:** calm and professional, not alarming, but still clearly saying an AI assistant is replying.
4. **The promise in the identity answer:** you are comfortable saying you will see the conversation and can reply personally.
5. **Uzbek spelling:** ʻ and ʼ as above (or tell the TL if you prefer the plain ' apostrophe; it must then be used the same way everywhere).

Reply with "approve texts" and the marker choice, or list the lines to change. The TL records the result in decisions.md and F0-S codes it.
