# SENS — Sensitive-category filter for inferred memory (junior unit)

| | |
|---|---|
| **Unit** | SENS — pure filter that drops inferred-memory candidates in a sensitive category (closed list, OD-15), with an unknown category ("uncertain"), or whose text hits a lexical screen in English, Russian and Uzbek (Latin and Cyrillic); returns the kept candidates and the dropped counts |
| **Owner slot** | D2 (David's team, junior) |
| **Mentor** | E2 (Codex; the M7-MEX owner; peer review dispatched by the TL) |
| **QA** | QD1, **before merge** (depth: the closed list, uncertain dropped, the uz/ru/en screen, the counts) |
| **Size** | S (≈ 0.5 engineer-day, one fix round included) |
| **Depends on** | F0-S: `MEMORY_CATEGORIES` and `SENSITIVE_MEMORY_CATEGORIES` in `src/assistant/memory/models.py` (P3-schema §3.2, §4; values OD-15). F0-E: `tests/log_hygiene.py` and the template `tests/unit/test_log_hygiene_f0e.py` (on `int/p3` after CP1). M03's `memory/search.py::normalize` (on `int/p3` from D0) |
| **Consumed by** | M7-MEX (`memory/inferred.py`: every candidate passes `filter_candidates` before any row is written; RG-15). The database CHECK `ck_memory_items_category` (F0-S) is the second line: it rejects every sensitive value even if this filter had a bug |
| **Branch → base** | `p3/sens` from `origin/int/p3` (after CP1) → PR to `int/p3`. If you start before CP1, branch from `origin/p3/f0-s` and retarget to `int/p3` after F0-S merges |
| **Normative inputs** | [P3-schema §3.2](../contracts/P3-schema.md) (category CHECK, the sensitive tuple), §4 (the constants); [P3-interfaces §9.3](../contracts/P3-interfaces.md) (`MemoryCandidate`, "every candidate passes SENS before any row is written"); decisions OD-15, P3-T50, P3-T36; IMPLEMENTATION_PLAN S07.4 ("dropped by a deterministic filter and never reach `pending_review`"); PRD FR-MEM-05 |

## 1. Goal

Inferred notes about contacts must never hold health, religion, politics, sexuality, money, ethnicity, legal, union, biometric, immigration or precise-location facts (OD-15). The model labels each candidate with a category, and labels can be wrong, so one pure module, `memory/sensitive_filter.py`, drops a candidate when its category is sensitive, when its category is not one of the allowed values (uncertain ⇒ drop), or when its text contains a sensitive term in English, Russian or Uzbek. Over-blocking is the safe failure: a lost suggestion costs little; a stored sensitive fact breaks a privacy promise.

## 2. Read first

1. [handoffs/junior-engineers.md](../handoffs/junior-engineers.md), all of it.
2. [WORKFLOW](../WORKFLOW.md) §2, §3, §4 (PR template), §5, §6.2.
3. `src/assistant/memory/models.py` after F0-S: `MEMORY_CATEGORIES`, `SENSITIVE_MEMORY_CATEGORIES`, `CATEGORY_OTHER_SAFE`.
4. `src/assistant/memory/search.py`: `normalize` and its module docstring. It folds case, removes apostrophes (`oʻ`, `o'` → `o`) and transliterates Cyrillic (Uzbek and Russian) to Latin ASCII, so one ASCII term list matches all four scripts.
5. `tests/unit/test_search_normalize.py`: how Cyrillic test strings are written.
6. [P3-schema §3.2](../contracts/P3-schema.md) ("Sensitive categories are absent from the CHECK on purpose").
7. This spec, to the end.

## 3. Allowed files

| Path | New or existing | Exactly what may change |
|---|---|---|
| `src/assistant/memory/sensitive_filter.py` | new | The whole module (§6.1) |
| `tests/unit/test_sensitive_filter.py` | new | The tests of §7.1. Its first line is `# ruff: noqa: RUF001, RUF003` (the Cyrillic samples look like Latin letters to ruff; this file-level directive replaces a `pyproject.toml` edit, which you may not make). If `ruff check` reports `RUF100` (unused suppression) for one of the two codes, remove only that code from the directive; add no other suppression |
| `tests/unit/test_log_hygiene_sens.py` | new | The test of §7.2, copied from F0-E's template `tests/unit/test_log_hygiene_f0e.py` |

## 4. Forbidden

- `src/assistant/policy/`, `src/assistant/actions/`, `src/assistant/telegram/` (including `gateway.py`, `ingress/`), `migrations/`, `src/assistant/agent/` (including `prompts/`, `prompt_registry.py` and M7-MEX's `memory_schemas.py`), `src/assistant/privacy/`, consent code (`src/assistant/llm/consent.py`), `src/assistant/config.py`, `src/assistant/core/`, `src/assistant/jobs/`, `src/assistant/bootstrap.py`, `AGENTS.md`, every `docs/` file.
- `src/assistant/memory/models.py` (F0-S; frozen — the category tuples change only through the owner's OD-15 answer and J1), `src/assistant/memory/search.py`, `src/assistant/memory/inferred.py` (M7-MEX) and every other `memory/` file.
- `pyproject.toml`, `uv.lock` (no new dependency), `.env*`, `var/`.
- Imports in `sensitive_filter.py` other than the standard library (`re`, `unicodedata`, `collections.abc`, `dataclasses`, `enum`, `types`, `typing`), `assistant.memory.models` (the two tuples) and `assistant.memory.search` (`normalize`). No logger. No Cyrillic characters in the module (the term lists are normalised ASCII; this keeps ruff's RUF001 quiet without a `pyproject.toml` edit).
- **Every file not in §3.**

## 5. Shared lists you must update

None. SENS registers nothing. The keys of its term table must equal `SENSITIVE_MEMORY_CATEGORIES` (a test enforces it); you read that tuple, never edit it.

## 6. Step-by-step

### 6.1 The exact API (`src/assistant/memory/sensitive_filter.py`)

```python
"""Sensitive-category filter for inferred memory (P3 SENS; OD-15, FR-MEM-05, RG-15).

Pure and deterministic: no database, no clock, no logging, no model. A
candidate is dropped when its category is sensitive, when its category is
not an allowed value (uncertain means drop), or when its normalised text
contains a sensitive term. Over-blocking is the safe failure. The database
CHECK on memory_items.category is the second line of defence."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol

from assistant.memory.models import MEMORY_CATEGORIES, SENSITIVE_MEMORY_CATEGORIES
from assistant.memory.search import normalize


class DropReason(StrEnum):
    SENSITIVE_CATEGORY = "sensitive_category"
    UNKNOWN_CATEGORY = "unknown_category"
    SENSITIVE_TERM = "sensitive_term"


class Candidate(Protocol):
    @property
    def category(self) -> str | None: ...
    @property
    def text(self) -> str: ...


@dataclass(frozen=True, slots=True)
class Lexicon:
    stems: tuple[str, ...]  # match at the start of a token; an entry with a space is a phrase
    words: tuple[str, ...]  # match whole tokens only


@dataclass(frozen=True, slots=True)
class FilterResult[C: Candidate]:
    kept: tuple[C, ...]  # input order
    dropped: int  # all drops
    by_reason: Mapping[DropReason, int]  # every DropReason key, zero included


SENSITIVE_LEXICON: Final[Mapping[str, Lexicon]] = MappingProxyType(
    {...}
)  # §6.2, keys = SENSITIVE_MEMORY_CATEGORIES


def sensitive_categories_in(text: str) -> frozenset[str]: ...
def screen(*, category: str | None, text: str) -> DropReason | None: ...  # None = keep
def filter_candidates[C: Candidate](candidates: Sequence[C]) -> FilterResult[C]: ...
```

M7-MEX's `MemoryCandidate` (a pydantic model with `category` and `text` fields) satisfies `Candidate` without importing anything from this module's internals; SENS never imports `assistant.agent` (layering).

**Rules (each tested in §7.1):**

1. **Tokens.** First remove every Unicode format character (category `Cf`: zero-width space and joiners, soft hyphen, byte-order mark, word joiner) so one hidden character cannot split a term (the same defence the grounding checker uses, TL ruling R33): `cleaned = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")`; `tokens = re.findall(r"[a-z0-9]+", normalize(cleaned))`; `joined = " " + " ".join(tokens) + " "`. The characters are removed, not turned into spaces (`kas` + zero-width space + `al` reads `kasal`).
2. **`sensitive_categories_in(text)`** returns every category whose lexicon hits: a **stem** hits when `" " + stem` occurs in `joined` (it starts at a token start; a phrase stem's last word may continue, e.g. `kasaba uyushma` hits "kasaba uyushmasi"); a **word** hits when `" " + word + " "` occurs in `joined`. Empty text → `frozenset()`.
3. **`screen(*, category, text)`**, checks in this order:
   1. `category in SENSITIVE_MEMORY_CATEGORIES` → `DropReason.SENSITIVE_CATEGORY`;
   2. `category is None` or `category not in MEMORY_CATEGORIES` (including `""` and wrong case such as `"Work"`) → `DropReason.UNKNOWN_CATEGORY` ("anything uncertain is dropped", OD-15);
   3. `sensitive_categories_in(text)` not empty → `DropReason.SENSITIVE_TERM` (whatever the allowed category says, `other_safe` included);
   4. otherwise `None` (keep).
4. **`filter_candidates(candidates)`** applies `screen` to each, keeps the `None` ones in input order, and returns `FilterResult(kept=..., dropped=<number dropped>, by_reason=MappingProxyType(<count per reason, all three keys>))`. It never raises for any `str` text, never mutates its input and never returns a dropped candidate.
5. **Nothing is logged and nothing is returned that holds the text** apart from the kept candidates themselves; the caller (M7-MEX) logs counts only.

### 6.2 The term table (exact; normalised ASCII)

Write these tuples exactly, in this order, into `SENSITIVE_LEXICON`, one `Lexicon(stems=(...), words=(...))` per category, keys in the order of `SENSITIVE_MEMORY_CATEGORIES`. Every entry is lowercase ASCII letters and digits, single spaces only, at least 3 characters, and equal to `normalize(entry)` (a test checks it). The "Source words" column is for reviewers only (the owner's native review may add terms later, OD-15); never put it in code.

| Category | `stems` | `words` | Source words (review aid; not code) |
|---|---|---|---|
| `health` | `diagnos`, `illness`, `disease`, `cancer`, `diabet`, `pregnan`, `hospitaliz`, `hospitalis`, `surgery`, `medication`, `depress`, `therap`, `disabilit`, `allerg`, `psychiatr`, `bolezn`, `bolen`, `boleyet`, `boleyu`, `boln`, `diagnoz`, `onkolog`, `beremen`, `gospital`, `lekarstv`, `tabletk`, `psixiatr`, `invalidnost`, `vrach`, `terapi`, `kasal`, `shifokor`, `homilador`, `tashxis`, `saraton`, `shifoxona`, `nogiron`, `dori`, `jarrohlik`, `ogriq` | `hiv`, `sick`, `hospital`, `hospitals`, `rak` | ru: болезнь, болен, болеет, болею, больной/больница, диагноз, онколог, беременна, госпиталь, лекарство, таблетка, психиатр, инвалидность, врач, терапия, рак; uz: kasal/касал, shifokor, homilador/ҳомиладор, tashxis, saraton, shifoxona, nogiron, dori, jarrohlik, og'riq |
| `religion` | `religi`, `church`, `mosque`, `synagogue`, `pray`, `muslim`, `christian`, `jewish`, `hindu`, `buddhis`, `atheis`, `ramadan`, `bible`, `quran`, `koran`, `tserk`, `mechet`, `molit`, `namaz`, `musulman`, `xristian`, `pravoslav`, `iudey`, `ateis`, `bibliya`, `diniy`, `dindor`, `namoz`, `masjid`, `musulmon`, `nasroniy`, `ramazon`, `ibodat`, `etiqod`, `alloh`, `olloh`, `cherkov` | `din` | ru: религия, церковь, мечеть, молитва/молиться, намаз, мусульманин, христианин, православный, иудей, атеист, Библия, Коран; uz: din, diniy, dindor, namoz, masjid/масжид, musulmon, nasroniy, ramazon, ibodat, e'tiqod, Alloh, cherkov |
| `politics` | `politic`, `politik`, `election`, `parliament`, `parlament`, `opposition`, `oppozitsi`, `protest`, `miting`, `golosova`, `deputat`, `vibori`, `siyos`, `saylov`, `muxolifat`, `ovoz ber` | `vote`, `votes`, `voted`, `voting`, `viborov`, `viborakh` | ru: политика/политический, выборы (not выбор "choice"), оппозиция, митинг, голосовать, депутат, парламент; uz: siyosat/сиёсат, siyosiy, saylov/сайлов, muxolifat, ovoz berish |
| `sexuality` | `sexual`, `homosex`, `bisex`, `heterosex`, `transgender`, `lesbi`, `lgbt`, `seks`, `gomoseks`, `biseks`, `jinsiy` | `gay`, `gays`, `gey` | ru: секс, гей, лесбиянка, гомосексуал, бисексуал, трансгендер; uz: jinsiy/жинсий |
| `finances` | `salar`, `debt`, `loan`, `mortgage`, `income`, `bankrupt`, `savings`, `credit card`, `bank account`, `zarplat`, `doljnik`, `kredit`, `ipotek`, `zarabot`, `nalog`, `bankrot`, `sberejeni`, `pensi`, `oklad`, `maosh`, `qarz`, `daromad`, `soliq`, `jamgarma`, `nafaqa` | `owe`, `owes`, `owed`, `wage`, `wages`, `tax`, `taxes`, `dolg`, `dolga`, `dolgi`, `dolgov`, `dolgu`, `dolgom`, `dolgakh`, `doxod`, `doxoda`, `doxodi`, `doxodov`, `doxodu`, `zaym`, `zayma`, `zaymi`, `zaymov`, `oyligi`, `oyligim`, `oyligini` | ru: зарплата, должник, кредит, ипотека, заработок, налог, банкрот, сбережения, пенсия, оклад, долг (whole word: долго "long" must not match), доход (whole word: доходит "reaches" must not match), займ (whole word: займусь must not match); uz: maosh/маош, qarz/қарз, kredit, ipoteka, daromad, soliq, jamg'arma, nafaqa, oyligi |
| `ethnicity` | `ethnic`, `racial`, `racism`, `nationalit`, `etnich`, `natsionalnost`, `rasov`, `rasizm`, `millat`, `irqi`, `etnik` | `race`, `races`, `rasa`, `rasi`, `rasu`, `irq` | ru: этнический, национальность, раса, расовый, расизм; uz: millat/миллат, irq/ирқ, irqiy, etnik |
| `legal` | `arrest`, `criminal`, `convict`, `prison`, `jail`, `lawsuit`, `probation`, `police`, `arest`, `ugolovn`, `sudim`, `tyurm`, `politsi`, `obvin`, `advokat`, `shtraf`, `sudlan`, `qamoq`, `jinoyat`, `hibs`, `jarima` | `sued`, `court`, `sud`, `suda`, `sude`, `sudom`, `sudu`, `sudga`, `sudda`, `suddan` | ru: арест, уголовный, судимость, суд, тюрьма, полиция, обвинение, адвокат, штраф; uz: sud, sudlangan, qamoq/қамоқ, jinoyat, politsiya, advokat, hibs, jarima |
| `union_membership` | `trade union`, `labor union`, `labour union`, `unioniz`, `unionis`, `profsoyuz`, `kasaba uyushma` | — | ru: профсоюз; uz: kasaba uyushmasi/касаба уюшмаси |
| `biometric` | `fingerprint`, `biometr`, `genetic`, `genetich`, `genetik`, `face id`, `otpechatki pal`, `otpechatok pal`, `barmoq izi` | `dna`, `dnk` | ru: биометрия, генетический, ДНК, отпечатки пальцев (not отпечатать "to print"); uz: barmoq izi, genetik |
| `immigration` | `immigra`, `emigra`, `migrant`, `deport`, `asylum`, `refugee`, `residence permit`, `work permit`, `citizenship`, `passport`, `bejen`, `grajdanstv`, `pasport`, `vid na jitel`, `ubejishch`, `fuqarolik`, `qochoq` | `visa`, `visas`, `viza`, `vizi`, `vizu`, `vizoy` | ru: иммиграция, эмиграция, мигрант, депортация, беженец, гражданство, паспорт, вид на жительство, убежище, виза (whole word: вызов "call" is also `vizov` and must not match); uz: fuqarolik, qochoq/қочоқ, pasport, viza |
| `precise_location` | `home address`, `lives at`, `zip code`, `house number`, `flat number`, `apartment`, `postcode`, `adres`, `kvartir`, `podyezd`, `ulits`, `jivyot na`, `jivet na`, `jivyot v`, `jivet v`, `manzil`, `kocha`, `uy raqami` | `street` | ru: адрес, квартира, подъезд, улица, живёт на/в, живет на/в; uz: manzil/манзил, ko'cha/кўча, uy raqami, kvartira |

Why some terms are whole words only: `dolg` would also match "долго" (long), `doxod` "доходит" (reaches), `zaym` "займусь" (I will deal with), `hospital` "hospitality", `din` "dinamo". Why some common words are left out (`party`, `president`, `union` alone, `bank`, `operation`, `xudo` as in the given name Xudoyberdi, `roza` as in the name Roza): too many innocent notes would be dropped. Adding a term later is allowed only through the issue (the owner's OD-15 review), never silently.

### 6.3 Steps (each ends with a check)

All commands run in your directory with `$env:COMPOSE_PROJECT_NAME = 'd2'` set (they run inside the container). If your session cannot run Docker, write the tests, push, let CI run them, and say so in the PR.

1. **Branch** (§ header). **Check:** `docker compose -f compose.dev.yml run --rm app uv run python -c "from assistant.memory.models import MEMORY_CATEGORIES, SENSITIVE_MEMORY_CATEGORIES; print(len(MEMORY_CATEGORIES), len(SENSITIVE_MEMORY_CATEGORIES))"` prints `7 11`. If the import fails, F0-S is not on your base: stop and ask (§10).
2. **Red tests.** Create `tests/unit/test_sensitive_filter.py` (first line `# ruff: noqa: RUF001, RUF003`) with every test of §7.1, and `src/assistant/memory/sensitive_filter.py` with the docstring, `DropReason`, `Candidate`, `Lexicon`, `FilterResult`, `SENSITIVE_LEXICON = MappingProxyType({})` and the three functions raising `NotImplementedError`. **Check:** `docker compose -f compose.dev.yml run --rm app uv run pytest tests/unit/test_sensitive_filter.py -q` → the tests fail with `NotImplementedError` or assertion errors, none with `ImportError`.
3. **The term table** (§6.2), exactly. **Check:** `… -k "lexicon or disjoint"` → pass.
4. **`sensitive_categories_in`.** **Check:** `… -k "term or kept or detects"` → pass.
5. **`screen`.** **Check:** `… -k "screen or rg15 or category"` → pass.
6. **`filter_candidates`.** **Check:** the whole file passes.
7. **Hygiene test** from the F0-E template (§7.2). First **Check:** `git cat-file -e origin/int/p3:tests/log_hygiene.py` exits 0 (F0-E is merged). If it does not, push steps 1–6 as a draft, comment on the SENS issue "hygiene test waits for F0-E", and do this step after `git fetch origin` and `git rebase origin/int/p3` once F0-E has merged. Then **Check:** `… uv run pytest tests/unit/test_log_hygiene_sens.py -q` → pass.
8. **Static checks.** `docker compose -f compose.dev.yml run --rm app uv run ruff check src/assistant/memory/sensitive_filter.py tests/unit/test_sensitive_filter.py tests/unit/test_log_hygiene_sens.py`; the same with `uv run ruff format --check`; `… uv run mypy src/assistant/memory/sensitive_filter.py`; `… uv run lint-imports`. **Check:** all clean.
9. **Diff.** `git diff --stat origin/int/p3...HEAD` → exactly the three files of §3.
10. **Commit and PR** (§12): one commit with the red tests, one with the green code.

## 7. Named tests (red first)

Helper in the test file: `@dataclass(frozen=True) class Cand: category: str | None; text: str` (synthetic, satisfies `Candidate`).

### 7.1 `tests/unit/test_sensitive_filter.py`

**Term table**
- `test_lexicon_keys_are_exactly_the_sensitive_categories` — `tuple(SENSITIVE_LEXICON) == SENSITIVE_MEMORY_CATEGORIES`.
- `test_every_lexicon_entry_is_already_normalized` — every stem and word: `normalize(e) == e`, matches `^[a-z0-9]+( [a-z0-9]+)*$`, `len(e) >= 3`.
- `test_no_lexicon_entry_is_listed_twice_in_a_category` — no duplicates inside any `stems` or `words` tuple.
- `test_sensitive_and_allowed_categories_are_disjoint` — `set(MEMORY_CATEGORIES).isdisjoint(SENSITIVE_MEMORY_CATEGORIES)`.

**Lexical screen: a sensitive term in every category and every script is detected** — `test_detects_sensitive_terms`, parametrized over these rows; each asserts `category in sensitive_categories_in(text)` and `screen(category="other_safe", text=text) is DropReason.SENSITIVE_TERM`:

| Category | en | ru | uz-Latn | uz-Cyrl |
|---|---|---|---|---|
| health | "She was diagnosed with diabetes last year" | "Он сейчас в больнице" | "Opasi kasal, shifokorga bordi" | "У ҳомиладор" |
| religion | "He prays at the mosque on Fridays" | "Она ходит в церковь" | "Har kuni namoz o'qiydi" | "Масжидга боради" |
| politics | "He will vote in the election" | "Он депутат парламента" | "Saylovda qatnashadi" | "Сиёсатга қизиқади" |
| sexuality | "He is gay" | "Он гей" | "Uning jinsiy hayoti" | "Жинсий масалалар" |
| finances | "His salary is low and he has a loan" | "У него большой долг" | "Bankdan kredit oldi, qarzi ko'p" | "Маоши кам" |
| ethnicity | "Her ethnic background" | "Его национальность" | "Millati o'zbek" | "Ирқи" |
| legal | "He was arrested and spent a year in prison" | "У него судимость" | "Sudga chaqirildi" | "Қамоқда ўтирган" |
| union_membership | "She is in the trade union" | "Член профсоюза" | "Kasaba uyushmasi a'zosi" | "Касаба уюшмаси" |
| biometric | "We stored his fingerprint" | "Анализ ДНК" | "Barmoq izi olindi" | "Генетик тест" |
| immigration | "Her visa expires in May" | "Он беженец" | "Fuqarolik olish uchun" | "Виза олди" |
| precise_location | "She lives at 12 Green Street" | "Живёт на улице Садовой" | "Manzili: Lola ko'chasi 5" | "Квартираси учинчи қаватда" |

- `test_detects_terms_typed_with_apostrophes_or_capitals` — `"KASAL"`, `"Ro'yxatda: QARZ"`, `"jamg‘armasi bor"` (with U+2018) → `health`, `finances`, `finances` detected.
- `test_detects_terms_hidden_by_format_characters` — parametrized, written with escapes: `"kas​al"` (zero-width space) → `health`; `"dia­gnosed with diabetes"` (soft hyphen) → `health`; `"sal﻿ary is low"` (byte-order mark) → `finances`; each also gives `screen(category="other_safe", text=text) is DropReason.SENSITIVE_TERM`.

**Lexical screen: innocent notes are kept** — `test_innocent_notes_are_kept`, parametrized; each asserts `sensitive_categories_in(text) == frozenset()` and `screen(category="work", text=text) is None`:
- "Works as a project manager at a logistics firm"
- "Prefers voice messages after 18:00"
- "Works in hospitality"
- "Incoming calls are answered by an assistant"
- "Работает дизайнером, отвечает быстро"
- "Он долго ждал ответа"
- "Ertaga soat 10 da uchrashuv bor"
- "Лойиҳа бўйича ҳисобот юборади"
- "" (empty text)

**Categories**
- `test_rg15_every_sensitive_category_is_dropped` — parametrized over `SENSITIVE_MEMORY_CATEGORIES`: `screen(category=c, text="Prefers short replies") is DropReason.SENSITIVE_CATEGORY` (the unit half of RG-15).
- `test_unknown_or_missing_category_is_dropped_as_uncertain` — parametrized `None`, `""`, `"Work"`, `"contact_info"`, `"emotion"`, `"trust_score"`: `screen(category=c, text="Prefers short replies") is DropReason.UNKNOWN_CATEGORY`.
- `test_every_allowed_category_with_innocent_text_is_kept` — parametrized over `MEMORY_CATEGORIES`: `screen(category=c, text="Prefers short replies") is None`.
- `test_sensitive_category_wins_over_the_term_screen` — `screen(category="health", text="Works in hospitality") is DropReason.SENSITIVE_CATEGORY`.
- `test_other_safe_with_a_sensitive_term_is_dropped` — `screen(category="other_safe", text="Her visa expires in May") is DropReason.SENSITIVE_TERM`.

**Filter**
- `test_filter_keeps_order_and_counts_every_reason` — input `[Cand("work", "Leads the design team"), Cand("health", "Prefers short replies"), Cand(None, "Likes tea"), Cand("schedule", "Has a doctor appointment at the hospital"), Cand("logistics", "Ships orders on Mondays")]`: `kept == (input[0], input[4])`, `dropped == 3`, `by_reason == {SENSITIVE_CATEGORY: 1, UNKNOWN_CATEGORY: 1, SENSITIVE_TERM: 1}`.
- `test_filter_of_nothing_reports_zero_for_every_reason` — `filter_candidates([])` → `kept == ()`, `dropped == 0`, `by_reason` has the three keys, each `0`.
- `test_filter_never_returns_a_dropped_candidate_and_never_mutates_its_input` — a list of 30 candidates: 11 `Cand(c, "Prefers short replies")`, one per sensitive category `c`; 7 `Cand(c, "Prefers short replies")`, one per allowed category `c`; 12 `Cand("other_safe", text)`: the 11 English texts of the §7.1 term table (one per sensitive category) and the uz-Latn text of its `health` row. Afterwards: `kept` is exactly the 7 allowed-category candidates (same objects, input order); `dropped == 23`; `by_reason == {SENSITIVE_CATEGORY: 11, UNKNOWN_CATEGORY: 0, SENSITIVE_TERM: 12}`; `len(kept) + dropped == 30`; the input list is equal to a copy taken before the call.

### 7.2 `tests/unit/test_log_hygiene_sens.py`

- `test_the_sensitive_filter_logs_nothing` — inside `with capture_logs() as output:` (F0-E's `tests/log_hygiene.py`, which also exports `CANARY` = `CANARY-SYNTH-P3` and `assert_content_free`), run `filter_candidates` on candidates whose texts are `f"{CANARY} diagnosed with diabetes"` (category `other_safe`), `f"{CANARY} Prefers short replies"` (category `work`) and `f"{CANARY} text"` (category `None`); then assert `output == []` and call `assert_content_free(output, CANARY)`.

You may add tests; never fewer, never with changed expected values.

## 8. Truthfulness edge cases

- **Uncertain is dropped, not guessed.** A missing, empty, mis-cased or unknown category is never mapped to the nearest allowed one.
- **A label never overrides the text.** `other_safe` or `work` with a sensitive term is dropped.
- **The counts are true.** `dropped` and `by_reason` count every drop, the unknown-category ones included; M7-MEX reports `result.dropped` as `dropped_sensitive` in the audit row `memory_suggestions_created` (P3-interfaces §13); a capped or partial count would hide drops.
- **Kept means kept unchanged.** The filter returns the same objects, never an edited or redacted text.
- **Hidden characters do not hide a term.** A zero-width or soft-hyphen character inside a word is removed before matching (rule 1), so `kas​al` is caught like `kasal`.
- **Over-blocking is accepted, under-blocking is not.** When a test of yours shows an innocent note dropped, note it as an open item; when it shows a sensitive note kept, it is a blocking bug.
- **No text leaves the function** except the kept candidates: no logs, no exception messages built from the text.

## 9. Acceptance checklist

- [ ] Every test in §7 exists, was seen failing, and passes.
- [ ] `git diff --stat origin/int/p3...HEAD` lists only the three files of §3.
- [ ] `SENSITIVE_LEXICON` equals §6.2 entry for entry (QD1 compares it).
- [ ] `sensitive_filter.py` imports only the standard library, `assistant.memory.models` and `assistant.memory.search`; `lint-imports` clean; no Cyrillic in the module.
- [ ] `ruff check`, `ruff format --check` and `mypy` (strict) clean on the changed files.
- [ ] No logging, no `assert`, no broad `except` in the module; it raises nothing for any text.
- [ ] The PR description gives M7-MEX the exact call (`filter_candidates(candidates)` → `FilterResult.kept`, `.dropped`, `.by_reason`).

## 10. When to stop and ask

Ask on the SENS issue, starting the comment with "Question for E2:" (plain text, never an @-mention). If nobody answers within 2 h, David raises it with the TL.

- `MEMORY_CATEGORIES` or `SENSITIVE_MEMORY_CATEGORIES` is missing on your base, or holds values other than OD-15's (the owner may have answered OD-15 differently: then this spec's table needs a TL update first).
- A §7 sample is not detected (or an innocent one is) with the §6.2 table exactly as written: do **not** change a sample or an expected value; ask.
- `normalize(entry) != entry` for a §6.2 entry.
- You want to add, remove or change a term.
- You think you need any file outside §3, or a `pyproject.toml` change.
- `ruff check` reports anything about the test file other than a removable unused code in its first-line directive (§3).

**Contract gaps already known (the TL tracks them; do not wait on them):** P3-interfaces §9.3 says every candidate passes SENS but does not name `filter_candidates`, `FilterResult`, `DropReason` or `Candidate` (M7-MEX consumes them; the contract should copy §6.1). M7-MEX's contact-detail screen (phone numbers, e-mail, URLs, usernames) is a separate check in M7-MEX, not part of this unit.

## 11. Coding rules (reviewers block on these)

- No broad `except`; no `assert` in production code; full type hints, `mypy --strict` clean; names exactly as §6.1.
- No logging in the module; no text in any exception message.
- No Telegram, no database, no model, no clock.
- Synthetic test data only: invented notes, no real people, places or companies (the street, the names and the firm in §7 are invented).
- GitHub: every body through `--body-file <file>`; no bare `@names`; no recurring status comments.
- Commits: Conventional Commits, `git commit -F <file>`; never `--no-verify`.

## 12. Pull request

- **Title:** `SENS: sensitive-category filter for inferred memory`.
- **Size:** about 400 changed lines or fewer (expected ≈ 170 code + ≈ 200 tests).
- **Labels:** `team:david`, `eng:d2` on the draft; when CI is green, mark it ready and add `needs-review:e2` and `needs-qa`. QA is **before merge**: the TL merges only after QD1's review is clean.
- **Template** (WORKFLOW §4.3):
  - *Unit:* `WU: SENS · Spec: docs/engineering/p3/specs/SENS.md · Issue: #<n> · Base: int/p3`; Tier: other; QA lane: before merge.
  - *Safety invariants touched:* 12 (privacy of contact data) through RG-15's unit half — name `test_rg15_every_sensitive_category_is_dropped` and `test_detects_sensitive_terms`. (Invariant 3a is M7-MEX's: `extract_memory` has no tools.)
  - *Tests:* the §7 names; commands and results, or "not run locally: <reason>; CI is the gate"; RG-15 (unit half) added.
  - *Shared lists updated:* none.
  - *Data:* no migration, no content column; logs: `test_log_hygiene_sens.py`.
  - *Prompts, flags and settings:* none.
  - *Docs:* none (M7-MEX documents the inferred-memory flow in ARCHITECTURE).
  - *Interface changes:* none.
  - *Open items for M07.md:* the term list is a TL proposal pending the owner's native review (OD-15); any innocent note you found dropped.
