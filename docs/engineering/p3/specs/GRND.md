# GRND — Grounding extractor and language-gate keys

| | |
|---|---|
| **Unit** | GRND: the pure `content_grounded` extractor (`actions/grounding.py`) and the language-gate key mapper (`actions/language_keys.py`). Over-blocking is the safe failure |
| **Team / slot** | Josh / J2 |
| **Size** | M |
| **Depends on** | F0-C API: the frozen signatures and fail-closed stubs (P3-interfaces §3.7). F0-S creates the two stub files, so before CP1 the branch stacks on `p3/f0-s` |
| **Branch → base** | `p3/grnd` → `int/p3` (stacked on `p3/f0-s` until CP1, then retargeted) |
| **Peer reviewer** | E2 (Codex; E2 builds the delegated draft whose body GRND checks) |
| **QA** | QJ1 or QJ2, before merge, applies the mutation table |
| **Tier** | **1A ★** — Gemini read (David lead), TL deep review; the TL also reviews the token tables (P3-T30) |

## 1. Goal

Replace F0-S's fail-closed stubs with real, pure, deterministic bodies. `ground` finds every concrete value in the model's reply body (numbers, amounts and currencies, weekday, month and relative-day words, spelled numbers 0–100, URLs, emails, phone numbers, username handles) and counts those that appear in no owner-provided source, so `content_grounded` (M6-POL) turns any invented or contact-supplied value into a held reply (stored for approval; OD-11). The language-key functions map the tagger's output to the four gate languages (`uz-Latn`, `uz-Cyrl`, `ru`, `en`) and return `None` for anything else, so `language_gate` fails closed on unknown or mixed language. GRND is on the gate chain: merge by **D+2**, before the pre-screen (CP-G0, D+3).

## 2. Scope

**In:**
- The bodies of `ground`, `gate_key`, `text_key`, `contact_key` and `declared_key`.
- The per-language token tables (weekday, month and relative-day lexicons; spelled numbers 0–100; currency aliases; the non-value phrase list for the "one" words; the date-preposition list for Latin `may`), authored by J2 and reviewed by the TL.
- Private helpers inside the two modules; unit tests; ARCHITECTURE §11.2 as-built text.

**Out:**
- Calling the functions and choosing the sources: M6-PLAN's loader (`actions/policy_loader.py::autonomy_facts`) passes the model's body only, and as sources the shown shareable items' texts plus the delegation's own spec text. Contact messages are never sources (P3-interfaces §3.7).
- The rules that read the results (`content_grounded`, `language_gate`): M6-POL.
- Changing `memory/search.py` (`normalize`, `guess_lang`, `detect_script`); see §8.
- Prompt rules about values (DELEG-DRAFT).

## 3. Design

### 3.1 Frozen signatures (P3-interfaces §3.7; bodies only change)

```python
# actions/grounding.py
@dataclass(frozen=True, slots=True)
class GroundingResult:
    checked: int  # distinct values found in the text
    ungrounded: int  # of those, values found in no source


def ground(text: str, *, sources: Sequence[str]) -> GroundingResult: ...


# actions/language_keys.py
def gate_key(lang_guess: str | None, script: str | None) -> GateLanguage | None: ...
def text_key(text: str) -> GateLanguage | None: ...
def contact_key(tags: Sequence[tuple[str | None, str | None]]) -> GateLanguage | None: ...
def declared_key(tag: str) -> GateLanguage | None: ...
```

### 3.2 Purity

- Imports: the standard library, `assistant.memory.search` (`normalize`, `guess_lang`, `detect_script`; pure, standard library only) and `assistant.core.autonomy_texts.GateLanguage`. No `sqlalchemy`, `assistant.db`, logging, clock or environment.
- Total and deterministic: any `str` input returns a result and never raises; the same input always gives the same result. Empty or whitespace-only text gives `GroundingResult(0, 0)` (the empty-text case is `length_ok`'s, not GRND's).
- Every regular expression is compiled at import and has no nested unbounded quantifier over overlapping classes (no catastrophic backtracking on a 4096-character body).

### 3.3 `ground`: values and their canonical keys

`ground` extracts a set of canonical keys from `text` and, with the same function, from each source; `checked = len(text_keys)`, `ungrounded = len(text_keys - union(source_keys))`. Matching is **exact key membership, never substring or prefix**: `num:30` is not grounded by a source with `300` or `12:30`. Format variants that keep the same key match; anything else over-blocks.

**Pre-step (text and every source alike):** delete every Unicode format character (category `Cf`: zero-width space and joiners, bidi controls, soft hyphen, word joiner), then NFKC. The reader never sees these characters, so they must not split a value away from the extractor ("Friday" with a soft hyphen inside, "1000" with a zero-width space inside and "dushanba" with a zero-width non-joiner inside are read as the reader sees them). `memory.search.normalize` does not remove them, so GRND does it first.

Extraction then runs in this order; each class removes what it matched before the next class runs, so digits inside an email, URL or handle are never counted again as numbers. Classes 6–9 run on the word tokens of `normalize()` applied to **what classes 1–5 left**, never to the original text:

| # | Class | Recognised | Canonical key |
|---|---|---|---|
| 1 | Email | `local@domain.tld` (local part and domain without spaces or a second `@`); trailing `.,;:!?)]}»"'` stripped | `email:<casefolded>` |
| 2 | URL | `http://…`, `https://…`, `www.…`, and bare domains `label(.label)*.tld` with a 2–24-letter alphabetic TLD and an optional `/path`; trailing punctuation stripped | `url:<casefolded host and path>` without the scheme, a leading `www.` or a trailing `/` |
| 3 | Username handle | an at-sign not preceded by a word character, followed by 3–32 of `[A-Za-z0-9_]` | `handle:<casefolded>` |
| 4 | Number group | a maximal run of digits with single separator characters `␠ . , : / -` between digit runs (`\d+(?:[ .,:/\-]\d+)*`, every Unicode decimal digit mapped to its ASCII value with `unicodedata.decimal`; a leading `+` ignored; digit runs kept as written, leading zeros included) | `num:` + the digit runs joined by `\|` (e.g. `12:30` → `num:12\|30`; `+998 90 123 45 67` → `num:998\|90\|123\|45\|67`; Arabic-Indic `٥` → `num:5`) |
| 5 | Currency | the closed symbol list `$ € ₽ £` and the alias table (§3.4) | `cur:<ISO code>` |
| 6 | Weekday | the weekday lexicon (§3.4) | `wd:1` … `wd:7` (Monday = 1) |
| 7 | Month | the month lexicon (§3.4) | `mo:1` … `mo:12` |
| 8 | Relative day | the relative-day lexicon (§3.4) | `rel:-1`, `rel:0`, `rel:+1`, `rel:+2` |
| 9 | Spelled number 0–100 | the number-word tables (§3.4), with tens-plus-units compounds | `num:<value>` (the same namespace as a one-run number group: "besh" and "5" are both `num:5`) |

Consequences, each pinned by a test:
- The separator kind is ignored but the grouping is not: `12:30` = `12.30`; `1 000` ≠ `1000`; `1.5` ≠ `15`; `+998 (90) 123-45-67` gives three keys (`998`, `90`, `123|45|67`), so a phone number is grounded only when a source writes it with the same digit groups. A differently formatted source over-blocks (a card), which is safe.
- An amount needs both its number key and its currency key grounded. Multiplier words ("ming", "тысяч", "k") are not values; "5 ming" against a source "5000" over-blocks.
- A `[knowledge:<id>]` marker echoed into the body yields `num:<id>`, which no source contains: a card (DELEG-DRAFT's prompt forbids echoing markers).

### 3.4 Token tables (J2 writes them; the TL reviews them)

**Matching.** Lexicon classes (6–9) match on the word tokens of `memory.search.normalize(text)` (`[a-z0-9]+`), so Uzbek Latin (1995 and new alphabet, with or without apostrophes), Uzbek Cyrillic and Russian Cyrillic all meet in one ASCII form. Tables are written in the native script and normalised **at import** with `normalize`, never hand-transliterated; a test asserts every entry normalises to exactly one non-empty token. Three match modes:
- *uz exact + suffix:* the token equals the entry, or the entry plus one suffix from the closed list `ta, tasi, tadan, da, ga, dan, ni, ning, gacha, gi, dagi` ("dushanbada", "beshta", "bugungi"); "uchun" (for) is not "uch" because `un` is not in the list;
- *stem:* the token starts with the entry (Russian weekday and month case forms only);
- *exact:* English, and the Russian number forms, which are listed in full.

A token used by an earlier class is not reused (so "пятница" is a weekday, never the number five).

| Table | uz-Latn (exact + suffix) | uz-Cyrl (exact + suffix; normalises to the uz-Latn form) | ru | en (exact) |
|---|---|---|---|---|
| Weekday | dushanba, seshanba, chorshanba, payshanba, juma, shanba, yakshanba | душанба, сешанба, чоршанба, пайшанба, жума, шанба, якшанба | stems понедельник, вторник, четверг, пятниц, суббот, воскресен; exact среда, среду, среды, среде | monday … sunday (full names only) |
| Month | yanvar, fevral, mart, aprel, may† (suffixed forms always count), iyun, iyul, avgust, sentyabr, oktyabr, noyabr, dekabr | январ, феврал, март, апрел, май, июн, июл, август, сентябр, октябр, ноябр, декабр | stems январ, феврал, март, апрел, июн, июл, август, сентябр, октябр, ноябр, декабр; exact май, мая, мае | january … december (may†) |
| Relative day | bugun, ertaga, kecha, indinga, ertasiga | бугун, эртага, кеча, индинга, эртасига | exact сегодня, завтра, вчера, послезавтра | today, tonight, tomorrow, yesterday |
| Spelled numbers | nol, bir‡, ikki, uch, to'rt, besh, olti, yetti, sakkiz, to'qqiz, o'n, yigirma, o'ttiz, qirq, ellik, oltmish, yetmish, sakson, to'qson, yuz | the same words in Cyrillic (тўрт, тўққиз, ўн, ўттиз, тўқсон, …) | exact form lists: ноль/нуль; один/одна/одно/одну/одного/одной/одному/одним‡; два/две/двух/двум/двумя; три/трёх/трех/трём/трем/тремя; четыре/четырёх/четырех; пять/пяти/пятью; шесть, семь, восемь/восьми, девять, десять (each with its -и/-ью forms); одиннадцать … девятнадцать; двадцать, тридцать, сорок/сорока, пятьдесят, шестьдесят, семьдесят, восемьдесят, девяносто; сто/ста | zero, one‡, two … nineteen, twenty, thirty, forty, fifty, sixty, seventy, eighty, ninety, hundred |
| Currency aliases | so'm, sum → UZS | сўм, сум → UZS | exact рубль/рубля/рублей/руб → RUB; stem доллар → USD; exact евро → EUR | usd, dollar(s) → USD; eur, euro(s) → EUR; rub, ruble(s), rouble(s) → RUB; uzs → UZS; gbp, pound(s) → GBP |

- **Compounds:** a tens word followed by a units word 1–9 (`twenty one`, `twenty-one`, `yigirma uch`, `двадцать три`) is one value. Values above 100 written in words ("ikki yuz") stay separate values (2 and 100) and over-block.
- **† The one ambiguous month word** is the bare Latin-script token `may` (the English modal and the bare uz-Latn month share it). It counts only when the previous or next token is a number group or a spelled number, or the previous token is in a closed date-preposition list the TL reviews with the tables (en `in`, `on`, `of`, `by`, `from`, `until`, `till`, `since`, `before`, `after`, `early`, `mid`, `late`, `next`, `last`, `this`, `end`; uz `shu`, `kelasi`, `otgan`). "You may call" is not a value; "5 May" and "in May" are `mo:5`. Everything else always counts: `march`, the suffixed uz forms (`mayda`, `maygacha`) and the Cyrillic `май`, `мая`, `мае` (in Cyrillic they are unambiguous, so "в мае" is `mo:5`). Because `normalize` can map Cyrillic `май` to the same ASCII token, J2 keeps for each token whether its source word was Cyrillic (for example by normalising word by word); when that cannot be decided, the token counts (fail closed).
- **‡ The "one" words** (en `one`, uz `bir`, ru `один` and its forms) **always count** as `num:1`, as the contract's "spelled numbers 0–100" requires, except inside a closed list of non-value phrases J2 writes with the tables and the TL reviews (at most 10 per language, two tokens each, e.g. en `one moment`, `one sec`, `one of`; uz `bir oz`, `bir pas`, `bir zum`, `bir narsa`; ru `один момент`). A phrase that names a unit (`bir soat`, `one hour`, `одну минуту`) is never on the list. Over-blocking first: the pre-screen (D+3) shows how often these words cause `ungrounded_value`, and the TL may extend the phrase list in J2's follow-up PR (§9); a rule that skips the words by default needs an ICR, because it narrows the contract.

### 3.5 Language keys

| Function | Behaviour |
|---|---|
| `gate_key(lang, script)` | `("uz", "latin")` → `uz-Latn`; `("uz", "cyrillic")` → `uz-Cyrl`; `("ru", "cyrillic")` → `ru`; `("en", "latin")` → `en`; anything else → `None`, including `script = "mixed"`, either value `None`, `("ru", "latin")` (Latin-typed Russian) and unknown strings |
| `text_key(text)` | Empty or whitespace → `None`; else `gate_key(guess_lang(text), detect_script(text))` — the same tagger that wrote `messages.lang_guess` and `messages.script`, so the draft and the contact's stored tags are judged alike |
| `contact_key(tags)` | Empty → `None`; else the one key that every `(lang_guess, script)` maps to; if any tag maps to `None` or two keys differ → `None` (a mixed burst fails closed). A message without letters (emoji, digits only) has `None` tags, so a burst that contains one gives `None` |
| `declared_key(tag)` | Case-insensitive exact match on the four `GateLanguage` values (`"uz-latn"` → `uz-Latn`); anything else → `None` (no trimming, no `"uz"`, no `"uz-latin"`) |

**Known tagger limits** (pinned by `test_known_tagger_limits_fail_closed`; each only ever yields `None` or a key the gate then cross-checks):
- `guess_lang` returns `None` for Cyrillic text without an Uzbek-only letter (ў қ ғ ҳ) or a Russian-only letter (щ ы э), e.g. a short "Привет, как дела?", so many short Cyrillic messages get a card.
- `guess_lang` treats `э` as Russian-only, but Uzbek Cyrillic uses it ("эртага"). A Cyrillic Uzbek message without ў қ ғ ҳ but with э is tagged `ru`. In practice the draft's declared or tagged key then differs and the gate gives a card; see §8 for the proposed fix.

### 3.6 Fail-closed summary

| Situation | Result |
|---|---|
| A value only the contact wrote | Not in any source ⇒ ungrounded ⇒ card |
| A digit the mapping cannot read (`unicodedata.decimal` returns `None`; defensive, `\d` only matches decimal digits) | Key `num:?<raw>`, which no source matches ⇒ ungrounded |
| Hidden format characters inside a value | Removed before extraction, so the value is still found (and must be grounded) |
| An ambiguous word that cannot be resolved (Latin `may` without a date context is the only one) | The "one" words and every other lexicon word count; when the script of a token is unknown it counts |
| An unknown, mixed or `None` tag; Latin-typed Russian; uz-ru mix | Key `None` ⇒ `language_unknown` ⇒ card |
| A missed value class (e.g. "next week", "half past five", ordinal words) | Passes grounding; `draft_safe` (`makes_commitment`, `uses_only_permitted_information`) and the gate still apply (§9) |

## 4. Files

| Owned (GRND) | What |
|---|---|
| `src/assistant/actions/grounding.py` | Body of `ground`, tables and helpers. `GroundingResult` and the signature stay byte-identical to F0-S's stub |
| `src/assistant/actions/language_keys.py` | Bodies of the four functions. Signatures stay byte-identical |
| `tests/unit/test_grounding.py` | Created by F0-S with `test_the_f0_stub_marks_every_text_ungrounded`; GRND replaces that stub test with §5's tests |
| `tests/unit/test_language_keys.py` | Created by F0-S with `test_the_f0_stubs_return_no_key`; GRND replaces it |

Named touch points in other files: none. ARCHITECTURE §11.2 (as built). **Never edit without an ICR:** `memory/search.py`, `policy/*`, `actions/policy_loader.py`, every other `actions/` file. No log lines are added, so no hygiene file is needed (say so in the PR).

## 5. Tests (red first; real code, no fakes needed)

**`tests/unit/test_grounding.py`**
- `test_a_contact_supplied_value_is_not_grounded` (named; P3-interfaces §3.7; unit half of IMPLEMENTATION_PLAN S06.2 #8): the body repeats a number, a weekday and a link that appear only in the contact's message, which is not a source ⇒ `ungrounded == 3`.
- `test_a_value_from_a_shown_shareable_item_is_grounded`
- `test_a_value_from_the_delegation_spec_is_grounded`
- `test_text_without_values_checks_nothing` — `GroundingResult(0, 0)`, also for `""`.
- `test_number_groups_ignore_the_separator_kind_but_not_the_grouping` — `12:30`/`12.30` grounded; `1 000`/`1000` and `1.5`/`15` not.
- `test_values_match_by_whole_key_never_by_substring` — `30` is not grounded by a source containing only `300`, `12:30` or `1 230`; `example.uz/a` is not grounded by `example.uz/ab`.
- `test_hidden_format_characters_cannot_split_a_value` — a weekday with a soft hyphen inside, a number with a zero-width space inside and a URL with a zero-width joiner inside are each found and are ungrounded when no source has them; the same value written plainly in a source grounds them.
- `test_unicode_digits_are_read_as_their_values` — Arabic-Indic `٥` and full-width `１２` are `num:5` and `num:12` and are grounded by `5` and `12`.
- `test_a_phone_number_is_grounded_only_with_the_same_digit_groups`
- `test_currency_symbols_codes_and_words_map_to_one_currency` — `$`, `USD`, `dollar`, `доллар` are one key; `so'm`/`сўм`/`сум` another.
- `test_an_amount_needs_both_its_number_and_its_currency`
- `test_urls_emails_and_handles_must_appear_exactly` — case-insensitive; trailing punctuation ignored; `www.` and scheme ignored.
- `test_digits_inside_an_email_url_or_handle_are_not_separate_numbers`
- `test_weekday_names_are_values_in_every_language` — parametrized over uz-Latn, uz-Cyrl, ru (with case forms "в пятницу", "в среду") and en.
- `test_month_names_are_values_in_every_language`
- `test_relative_day_words_are_values_in_every_language`
- `test_spelled_numbers_0_to_100_are_values_in_every_language` — parametrized over every value 0–100 for en and uz-Latn, the listed forms for uz-Cyrl and ru.
- `test_a_spelled_number_is_grounded_by_the_same_digits` — "besh" grounded by "5", "двадцать три" by "23".
- `test_compound_spelled_numbers_read_as_one_value`
- `test_uzbek_latin_and_cyrillic_forms_are_the_same_value` — душанба/dushanba, тўрт/to'rt/tort.
- `test_uzbek_suffixes_come_from_the_closed_list` — "dushanbada", "beshta", "bugungi" are values; "uchun" is not.
- `test_the_one_words_count_except_in_listed_phrases` — "bir oz" and "one moment" (listed) are not values; "bir soat", "one hour", "only one seat left" and "один билет" are `num:1`; every listed phrase has exactly two tokens and names no unit word.
- `test_latin_may_needs_a_date_context_and_other_month_words_always_count` — "You may call" is not a value; "5 May", "in May", "may 5", "March", "в мае", "мая" and "mayda" are month values.
- `test_a_weekday_token_is_never_also_a_number` — "пятница" is `wd:5` only.
- `test_every_table_entry_normalizes_to_one_token`
- `test_every_lexicon_has_entries_for_every_gate_language`
- `test_ground_is_total_and_deterministic` — hypothesis (derandomized): arbitrary text and sources never raise; equal inputs give equal results; `0 <= ungrounded <= checked`.
- `test_long_inputs_complete` — a 4096-character adversarial body (long digit/separator runs, many at-signs and dots) with 30 sources returns a result (no timing assertion).
- `test_grounding_and_language_keys_import_nothing_that_reaches_the_database` — in a fresh interpreter (the `test_f0_shared.py` pattern), importing both modules leaves `sqlalchemy` out of `sys.modules`.

**`tests/unit/test_language_keys.py`**
- `test_none_or_mixed_fails_closed` (named; P3-interfaces §3.7): `gate_key(None, …)`, `gate_key(…, None)`, `gate_key("uz", "mixed")`, `text_key("")`, `contact_key([])`, a burst with one `None` tag, a burst with two keys — all `None`.
- `test_gate_key_maps_exactly_the_four_pairs` — parametrized over lang ∈ {uz, ru, en, None, "xx"} × script ∈ {latin, cyrillic, mixed, None}.
- `test_text_key_follows_the_stored_tagger` — "Salom, qalesiz?" → uz-Latn; "Ҳа, эртага келаман" → uz-Cyrl; "Спасибо, ещё раз" → ru; "Thanks, see you tomorrow" → en; "ok" → None; "privet kak dela" → None.
- `test_contact_key_needs_every_message_to_agree`
- `test_an_uz_ru_mixed_burst_fails_closed`
- `test_declared_key_matches_only_the_four_tags_ignoring_case`
- `test_known_tagger_limits_fail_closed` — pins §3.5's limits (Cyrillic without distinguishing letters → None; the `э` behaviour as §8 decides).

**Mutation table (Tier-1A; QA applies each, never committed)**

| # | Rule or guard | Mutation | Named test expected to fail |
|---|---|---|---|
| 1 | Only the given sources ground a value | add the text itself to the sources | `test_a_contact_supplied_value_is_not_grounded` |
| 2 | Number keys keep the grouping | join digit runs without separators | `test_number_groups_ignore_the_separator_kind_but_not_the_grouping` |
| 3 | Emails, URLs, handles removed before numbers | skip the removal | `test_digits_inside_an_email_url_or_handle_are_not_separate_numbers` |
| 4 | Currency class | drop the currency class | `test_an_amount_needs_both_its_number_and_its_currency` |
| 5 | Weekday table | empty the ru weekday stems | `test_weekday_names_are_values_in_every_language` |
| 6 | Month table | empty the uz-Cyrl months | `test_month_names_are_values_in_every_language` |
| 7 | Relative-day table | drop "ertaga" | `test_relative_day_words_are_values_in_every_language` |
| 8 | Spelled numbers | drop "yigirma" | `test_spelled_numbers_0_to_100_are_values_in_every_language` |
| 9 | Spelled and digit values share keys | key spelled numbers as `word:` | `test_a_spelled_number_is_grounded_by_the_same_digits` |
| 10 | Compounds | read tens and units separately | `test_compound_spelled_numbers_read_as_one_value` |
| 11 | The "one" words count outside listed phrases | skip "bir" unless before a unit word / count it inside "bir oz" | `test_the_one_words_count_except_in_listed_phrases` |
| 12 | Only Latin `may` needs a date context | require the context for Cyrillic `мае` too / count Latin `may` always | `test_latin_may_needs_a_date_context_and_other_month_words_always_count` |
| 13 | Tokens not reused across classes | let numbers re-match weekday tokens | `test_a_weekday_token_is_never_also_a_number` |
| 13a | uz suffixes are a closed list | accept any suffix (prefix match) | `test_uzbek_suffixes_come_from_the_closed_list` |
| 14 | Unicode digits read by value | keep the raw character instead of its value | `test_unicode_digits_are_read_as_their_values` |
| 14a | Whole-key matching | match a text key when it is a substring of a source key | `test_values_match_by_whole_key_never_by_substring` |
| 14b | `Cf` characters removed first | skip the pre-step | `test_hidden_format_characters_cannot_split_a_value` |
| 15 | `gate_key` mapping | map `("ru", "latin")` → ru | `test_gate_key_maps_exactly_the_four_pairs` |
| 16 | Mixed fails closed | map `"mixed"` with uz → uz-Latn | `test_none_or_mixed_fails_closed` |
| 17 | `contact_key` needs agreement | return the first message's key | `test_contact_key_needs_every_message_to_agree` |
| 18 | `declared_key` exact | accept `"uz"` as uz-Latn | `test_declared_key_matches_only_the_four_tags_ignoring_case` |

## 6. Acceptance criteria

1. The five signatures and `GroundingResult` are byte-identical to F0-S's stubs (P3-interfaces §3.7); only the bodies changed.
2. Every §5 test exists and passes in CI; ruff, format, mypy (`src`) and `lint-imports` are clean on `actions/`.
3. A value only the contact supplied is never grounded; `None`, mixed and Latin-typed Russian never yield a key; hidden format characters never hide a value; matching is by whole key only.
4. Every lexicon class has entries for all four gate languages; the TL's table review (including the "one" phrase list and the `may` date-preposition list) is recorded in the PR.
5. The mutation table is complete and QA killed every row.
6. Neither module imports the database, logs or reads the clock.
7. ARCHITECTURE §11.2 is updated; the PR lists the known limits for M06.md.
8. Merged into `int/p3` by D+2.

## 7. Docs

- **ARCHITECTURE §11.2 (as built):** the tagger feeds the gate through `language_keys` (four pairs; everything else, including mixed and `None`, fails closed); `content_grounded` uses `actions/grounding.py` (the nine value classes, keys, "over-blocking is the safe failure"). The §9.3 rule text itself is the TL's (F0-C part 2) and M6-POL's.
- **PR description:** the token tables in short, the "one" and ambiguous-month decisions, the known tagger limits, and any open items for M06.md.

## 8. Owner decisions

- **OD-14 (language gate):** assumes the default (four gate languages; mixed and unknown always a card). If the owner drops the language gate, `language_keys` is still built (M6-POL's `language_gate` would then be removed through a codes ICR), and the "Language" column of §3.5 is unused.
- **OD-10 (identity answer):** M6-PLAN uses `contact_key` to choose the identity sentence's locale; nothing here changes if OD-10 differs.
- **TL question (proposed ICR):** remove `э`/`Э` from `memory/search.py::_RUSSIAN_ONLY_LETTERS` (Uzbek Cyrillic uses the letter). It only makes tagging more conservative (more `None`, never a new wrong key), but it changes the stored `messages.lang_guess` for new messages, so it needs an ICR naming `memory/search.py` as a GRND touch point. **Default until the TL rules: GRND does not edit `memory/search.py`;** `test_known_tagger_limits_fail_closed` pins the current behaviour and the item goes to M06.md open items.
- **TL decisions in this spec** (accepted by the TL, ruling R33; P3-interfaces §3.7 adopts this text; over-blocking is the safe failure): the "one" words count except in a reviewed phrase list (§3.4 ‡; the earlier draft counted them only before a unit word, which let "only one seat left" pass ungrounded; the review pass changed it to over-block first); only the bare Latin `may` needs a date context (§3.4 †; Cyrillic `май/мая/мае` and `march` always count); English weekday full names only; the `Cf` pre-step (§3.3).

## 9. Risks and notes

- **Over-blocking lowers the autonomy rate.** Safe, but it can make J5 thin; counting `bir`/`one` everywhere outside the phrase list is the largest expected source in Uzbek and English. EV-AUT reports reason codes, so the pre-screen (D+3) shows how often `ungrounded_value` fires; table fixes (more listed phrases, more date prepositions) go in a small follow-up PR by J2 before the candidate freezes (D+4). A fix may only add a listed phrase or context word the TL reviewed; it never makes a class skip words by default.
- **Under-blocking** comes from value classes the lexicons do not cover (relative weeks, clock times in words, ordinal words like "birinchi"). Mitigations: `draft_safe` flags, the delegated prompt's rule against inventing values, the gate's zero-violation threshold, and the shadow window.
- **Tagger limits** (§3.5) push many Cyrillic chats to cards. A better `guess_lang` is an ICR on `memory/search.py`, decided after the pre-screen; it is not this unit's change.
- **Normaliser changes:** a `NORMALIZER_VERSION` bump changes table normalisation at import, but text and sources are normalised by the same function at run time, so matching stays consistent; `test_every_table_entry_normalizes_to_one_token` catches a table entry the new version splits.
