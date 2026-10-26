"""Metrics for the E1 summary/catch-up set and the shared metric builders.

Everything here is deterministic and model-free, so it is unit-tested on
hand-built fixtures (`tests/unit/test_eval_metrics.py`). Thresholds are the
S03.4 plan's (IMPLEMENTATION_PLAN §5):

- open-loop recall >= 95 % (the open questions and open commitments a catch-up
  must surface; README "How S03.4 is expected to score it");
- zero fabricated facts (judge pre-screen, `evals/judge.py`);
- a coverage caveat whenever gaps exist (100 %), and none for a delayed-only gap;
- owner-facing language and script correct 100 %.

What each deterministic check can and cannot see is stated on the function;
where a check is conservative (counts a doubtful item as a miss) it says so.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from assistant.memory.search import normalize
from evals.review import ReviewLedger, coverage_key
from evals.schema import Commitment, Conversation, QuestionToOwner
from evals.targets import Measure, MetricResult

OwnerLanguage = Literal["uz-Latn", "uz-Cyrl", "ru", "en"]

RECALL_THRESHOLD = 0.95
CAVEAT_THRESHOLD = 1.0
LANGUAGE_THRESHOLD = 1.0
#: At least this share of a text's letters must be in the owner's script; the
#: rest is allowed for quoted names, addresses and other verbatim fragments.
SCRIPT_SHARE_MIN = 0.8
#: A language needs this many function-word hits before it is "detected".
MIN_LANGUAGE_EVIDENCE = 2


# --------------------------------------------------------------------- builders
def ratio_metric(
    *,
    key: str,
    label: str,
    outcomes: Mapping[str, bool],
    threshold: float | None,
    ledger: ReviewLedger,
    note: str = "",
    set_size: int | None = None,
    gated: bool = True,
) -> MetricResult:
    """A pass-rate metric over `outcomes` (item id -> passed). The gate looks
    only at the items the owner has confirmed, and needs all `set_size` of them
    (default: the items measured). `gated=False` keeps the threshold as a
    pre-screen comparison only."""
    confirmed = {item: ok for item, ok in outcomes.items() if ledger.is_confirmed(item)}
    return MetricResult(
        key=key,
        label=label,
        unit="ratio",
        goal="at_least",
        threshold=threshold,
        all_items=Measure(sum(outcomes.values()), len(outcomes)),
        confirmed=Measure(sum(confirmed.values()), len(confirmed)),
        failed_items=tuple(item for item, ok in outcomes.items() if not ok),
        note=note,
        set_size=set_size,
        gated=gated,
    )


def count_metric(
    *,
    key: str,
    label: str,
    items: Collection[str],
    offending: Collection[str],
    threshold: float,
    ledger: ReviewLedger,
    pending: int = 0,
    note: str = "",
    set_size: int | None = None,
    gated: bool = True,
) -> MetricResult:
    """A "no more than `threshold` offences" metric (zero fabrications)."""
    confirmed_items = {item for item in items if ledger.is_confirmed(item)}
    confirmed_offences = [item for item in offending if ledger.is_confirmed(item)]
    return MetricResult(
        key=key,
        label=label,
        unit="count",
        goal="at_most",
        threshold=threshold,
        all_items=Measure(len(offending), len(items)),
        confirmed=Measure(len(confirmed_offences), len(confirmed_items)),
        failed_items=tuple(dict.fromkeys(offending)),
        pending=pending,
        note=note,
        set_size=set_size,
        gated=gated,
    )


# ----------------------------------------------------------- summary output
@dataclass(frozen=True, slots=True)
class SummaryOutput:
    """What the summary target produced for one conversation.

    The caveats are **structured** (which gaps the catch-up named), not parsed
    out of prose: the catch-up renderer knows deterministically which gaps it
    reported (S03.2), and the language of the prose is the owner's, not the
    conversation's.
    """

    conversation_id: str
    text: str
    owner_language: OwnerLanguage
    #: Ids of the coverage gaps the output carries a caveat for.
    caveat_gap_ids: frozenset[str] = frozenset()
    #: A "history before the connection is not covered" caveat is present.
    pre_connection_caveat: bool = False


# ------------------------------------------------------------------- recall
Matcher = Callable[[QuestionToOwner | Commitment, str], bool | None]


def word_key(text: str) -> str:
    """Script-, apostrophe- and punctuation-insensitive form: the product's
    ASCII key reduced to alphanumeric words (`19:00` and `19.00` agree, `Ўзбек`
    and `Ozbek` agree)."""
    return " ".join(re.findall(r"[a-z0-9]+", normalize(text)))


def contains_words(haystack: str, needle: str) -> bool:
    """`needle` occurs in `haystack` as whole words (both are `word_key`s)."""
    return bool(needle) and f" {needle} " in f" {haystack} "


#: An inflected value keeps at least this many letters of its last word.
MIN_INFLECTED_STEM = 3
#: The endings an Uzbek text puts on a word it quotes (ASCII key forms): case
#: (-ni, -ning, -ga/-ka/-qa, -da/-ta, -dan/-tan, -dagi, -gacha), possessive (-i,
#: -si, -im, -ing, -imiz, -ingiz), plural -lar, and -li/-lik/-cha/-dek, -ta and
#: ordinal -inchi. They chain (`nusxa-si-ni`, `oktabr-da-gi`). Nothing else: no
#: negation (-ma-: bormaydi, kelishmadi), no letters that make another stem
#: (Ali → Alisher, Olim → Olimpiada, Aziz → Aziza, Karim → Karimov), and no
#: Russian case endings (no value in E1 needs one, and -a/-ov make names).
INFLECTIONS = frozenset(
    {
        *("ni", "ning", "ga", "ka", "qa", "da", "ta", "dan", "tan", "dagi", "tagi"),
        *("gacha", "kacha", "qacha", "gi", "ki"),
        *("i", "si", "im", "ing", "imiz", "ingiz", "miz", "ngiz", "lar"),
        *("li", "lik", "cha", "dek", "day", "inchi", "nchi"),
    }
)
#: At most this many endings in a row.
MAX_INFLECTIONS = 3


def is_inflection(suffix: str, limit: int = MAX_INFLECTIONS) -> bool:
    """`suffix` is a chain of at most `limit` `INFLECTIONS` ("" is the empty chain)."""
    if not suffix:
        return True
    if limit == 0:
        return False
    return any(
        suffix.startswith(ending) and is_inflection(suffix[len(ending) :], limit - 1)
        for ending in INFLECTIONS
    )


def contains_value(haystack: str, needle: str) -> bool:
    """`needle` occurs in `haystack` as whole words, except that its **last**
    word may carry Uzbek case and possessive endings (both are `word_key`s).

    Uzbek is agglutinative and a summary inflects what it quotes: "900 ming" is
    written "900 mingga", "15-oktabr" "15-oktabrdagi", "narxlar ro'yxati"
    "narxlar ro'yxatini" (E-s). Only a chain of `INFLECTIONS` may follow, so "80"
    never matches "800", "Ali" never "Alisher" and "bor" never "bormaydi"; a last
    word shorter than `MIN_INFLECTED_STEM` letters must match whole, so "12-й"
    ("12 y") never matches "12 yanvar". The other words of the value still match
    whole.
    """
    if contains_words(haystack, needle):
        return True
    words = needle.split()
    if not words:
        return False
    *head, last = words
    if last.isalpha() and len(last) < MIN_INFLECTED_STEM:
        return False
    tokens = haystack.split()
    for start in range(len(tokens) - len(words) + 1):
        window = tokens[start : start + len(words)]
        if window[:-1] != head:
            continue
        candidate = window[-1]
        if candidate.startswith(last) and is_inflection(candidate[len(last) :]):
            return True
    return False


def values_matcher(item: QuestionToOwner | Commitment, summary_text: str) -> bool | None:
    """True when every `values` entry of `item` occurs in the summary; else None.

    A literal match is **sufficient** evidence that the loop was surfaced, but a
    failed one is **not** evidence that it was not (E-s): the summary is written
    in the owner's language, so a value from the chat is often translated or
    reformatted ("до пятницы" → "juma kunigacha", "6 pm" → "18:00", a date as
    `2026-10-14`). Such an item, like one with no `values`, is left to the judge
    (pre-screen) and to the owner's verdict (gate): None. Values are compared in
    the product's ASCII form (script and apostrophes do not matter), whole words
    except for an inflectional suffix on the value's last word (`contains_value`).
    """
    needles = [word_key(value) for value in item.values]
    if not needles:
        return None
    haystack = word_key(summary_text)
    if all(contains_value(haystack, needle) for needle in needles):
        return True
    return None


def open_loops(conversation: Conversation) -> list[QuestionToOwner | Commitment]:
    """The open questions and open commitments a catch-up must surface."""
    loops: list[QuestionToOwner | Commitment] = []
    loops += [q for q in conversation.labels.questions_to_owner if q.status == "open"]
    loops += [c for c in conversation.labels.commitments if c.status == "open"]
    return loops


def open_loop_coverage(
    conversation: Conversation, summary_text: str, matcher: Matcher = values_matcher
) -> dict[str, bool | None]:
    """Label id -> surfaced (True), missed (False) or not checkable (None)."""
    return {loop.id: matcher(loop, summary_text) for loop in open_loops(conversation)}


def recall_metric(
    coverage: Mapping[str, bool | None],
    ledger: ReviewLedger,
    judged: Mapping[str, bool] | None = None,
    set_size: int | None = None,
    summary_texts: Mapping[str, str] | None = None,
) -> MetricResult:
    """Open-loop recall, with three sources of truth for "was it surfaced":

    1. the deterministic `values` match (`open_loop_coverage`): True when every
       value is found; it never decides a miss (E-s);
    2. the owner's verdict in the ledger (`coverage`): the gate's source for an
       item not matched on `values`, and it **overrides** a string pass (a value
       can be found in a sentence that does not surface the loop; S03.6 review);
    3. for the rest, an optional judge verdict (`judged`): pre-screen only.

    An item with no verdict from any source counts as **missed** in the
    pre-screen. In the gate it is *unresolved*: it leaves the metric pending
    (never a pass) until the owner rules on it. A definite miss (False: the
    conversation was not summarized) stays a miss whatever the ledger says.

    With `summary_texts` (loop id -> the text of the summary it was judged in), the
    owner's verdict is looked up under `coverage_key(loop, text)`: it belongs to that
    summary and does not carry over to another run's. Without it the plain loop id
    is the key (hand-built fixtures).
    """
    judged = judged or {}
    pre_screen: dict[str, bool] = {}
    gate: dict[str, bool] = {}
    unresolved: list[str] = []
    for item, checked in coverage.items():
        pre_screen[item] = checked if checked is not None else judged.get(item, False)
        if not ledger.is_confirmed(item):
            continue
        key = item if summary_texts is None else coverage_key(item, summary_texts.get(item, ""))
        owner = ledger.coverage_verdict(key)
        if checked is False:
            gate[item] = False
        elif owner is not None:
            gate[item] = owner
        elif checked:
            gate[item] = True
        else:
            unresolved.append(item)
    uncheckable = sorted(item for item, checked in coverage.items() if checked is None)
    note = (
        f"{len(uncheckable)} open loop(s) not matched on `values` (none, or not found "
        "literally, e.g. translated): judge pre-screen only, "
        f"the gate needs the owner's verdict ({len(unresolved)} unresolved among the "
        "owner-confirmed items)"
        if uncheckable
        else ""
    )
    return MetricResult(
        key="open_loop_recall",
        label="Open question/commitment recall",
        unit="ratio",
        goal="at_least",
        threshold=RECALL_THRESHOLD,
        all_items=Measure(sum(pre_screen.values()), len(pre_screen)),
        confirmed=Measure(sum(gate.values()), len(gate)),
        failed_items=tuple(item for item, ok in pre_screen.items() if not ok),
        pending=len(unresolved),
        note=note,
        set_size=set_size,
    )


# ------------------------------------------------------------------ caveats
@dataclass(frozen=True, slots=True)
class CaveatCheck:
    conversation_id: str
    #: Expected caveat ids that the output does not carry.
    missing: tuple[str, ...]
    #: Caveats the output carries that the conversation does not call for
    #: (a delayed-only gap, an unknown gap, a pre-connection note with no
    #: coverage start).
    spurious: tuple[str, ...]
    expected: int


def check_caveats(conversation: Conversation, output: SummaryOutput) -> CaveatCheck:
    """Compare the caveats the output carries with `expected_caveats`."""
    missing: list[str] = []
    wanted_gaps: set[str] = set()
    for caveat in conversation.labels.expected_caveats:
        if caveat.reason == "pre_connection":
            if not output.pre_connection_caveat:
                missing.append(caveat.id)
        else:
            wanted_gaps.add(caveat.gap_id or "")
            if caveat.gap_id not in output.caveat_gap_ids:
                missing.append(caveat.id)
    spurious = [f"gap:{gap}" for gap in sorted(output.caveat_gap_ids - wanted_gaps)]
    if output.pre_connection_caveat and not any(
        c.reason == "pre_connection" for c in conversation.labels.expected_caveats
    ):
        spurious.append("pre_connection")
    return CaveatCheck(
        conversation.id,
        tuple(missing),
        tuple(spurious),
        len(conversation.labels.expected_caveats),
    )


def caveat_metrics(
    checks: Sequence[CaveatCheck],
    ledger: ReviewLedger,
    *,
    presence_set_size: int | None = None,
    spurious_set_size: int | None = None,
    gated: bool = True,
    note: str = "",
) -> list[MetricResult]:
    """Presence (every conversation that has expected caveats carries them all)
    and spurious caveats (none, e.g. for a delayed gap that lost nothing).

    The set sizes are the whole set's (a `--limit` or stopped run measures
    fewer, and its gate must not read as complete); default: the items measured.
    `gated=False` keeps the thresholds as a pre-screen comparison only, with `note`
    saying why (the summary target: the caveats do not come from the model)."""
    with_caveats = {c.conversation_id: not c.missing for c in checks if c.expected}
    presence = ratio_metric(
        key="caveat_presence",
        label="Coverage caveat present whenever gaps exist",
        outcomes=with_caveats,
        threshold=CAVEAT_THRESHOLD,
        ledger=ledger,
        set_size=presence_set_size,
        gated=gated,
        note=note,
    )
    spurious = count_metric(
        key="spurious_caveats",
        label="Caveats for gaps that lost nothing",
        items=[c.conversation_id for c in checks],
        offending=[c.conversation_id for c in checks if c.spurious],
        threshold=0,
        ledger=ledger,
        set_size=spurious_set_size,
        gated=gated,
        note=note,
    )
    return [presence, spurious]


# ----------------------------------------------------------------- language
#: Frequent function words, a deliberately small vocabulary: the check needs
#: "is this written in the owner's language", not language identification of
#: arbitrary text. English and Russian are matched on the raw lowercase words,
#: Uzbek on the product's ASCII form, so Uzbek Latin and Uzbek Cyrillic share
#: one list.
_ENGLISH = frozenset(
    [
        "the",
        "and",
        "is",
        "are",
        "to",
        "of",
        "in",
        "on",
        "for",
        "with",
        "that",
        "this",
        "at",
        "from",
        "by",
        "will",
        "has",
        "have",
        "was",
        "were",
        "not",
        "you",
        "he",
        "she",
        "they",
        "it",
        "an",
        "about",
        "asked",
        "wants",
        "said",
        "as",
        "be",
        "but",
        "or",
        "if",
        "his",
        "her",
        "their",
        "your",
        "our",
        "please",
        "also",
        "after",
        "before",
        "need",
        "needs",
    ]
)
_RUSSIAN = frozenset(
    [
        "и",
        "в",
        "не",
        "на",
        "что",
        "это",
        "как",
        "для",
        "по",
        "из",
        "он",
        "она",
        "они",
        "вы",
        "от",
        "но",
        "его",
        "её",
        "их",
        "мы",
        "нужно",
        "будет",
        "был",
        "была",
        "или",
        "до",
        "за",
        "при",
        "после",
        "также",
        "просит",
        "спросил",
        "спрашивает",
        "хочет",
        "сообщил",
        "написал",
        "пожалуйста",
        "нужна",
        "нужен",
        "тоже",
        "уже",
        "еще",
        "ещё",
    ]
)
_UZBEK = frozenset(
    [
        "va",
        "bilan",
        "uchun",
        "bu",
        "men",
        "siz",
        "emas",
        "ham",
        "bor",
        "yoq",
        "nima",
        "qanday",
        "keyin",
        "kerak",
        "ertaga",
        "deb",
        "soradi",
        "aytdi",
        "ozi",
        "qildi",
        "yozdi",
        "haqida",
        "lekin",
        "yoki",
        "ular",
        "biz",
        "sizga",
        "menga",
        "bolsa",
        "mumkin",
        "shu",
        "bir",
        "uchrashuv",
        "sorayapti",
        "yuboring",
        "yubordi",
        "keldi",
        "kelishdi",
        "iltimos",
        "aka",
        "opa",
    ]
)
_LETTERS = re.compile(r"[^\W\d_]+")


@dataclass(frozen=True, slots=True)
class LanguageVerdict:
    ok: bool
    #: `uz`, `ru`, `en`, or None when there is too little evidence.
    detected: str | None
    #: Share of the letters written in the owner's script.
    script_share: float


def _script_share(text: str, script: Literal["latin", "cyrillic"]) -> float:
    counts = {"latin": 0, "cyrillic": 0}
    for char in text:
        if char.isalpha():
            name = unicodedata.name(char, "")
            if name.startswith("LATIN"):
                counts["latin"] += 1
            elif name.startswith("CYRILLIC"):
                counts["cyrillic"] += 1
    total = counts["latin"] + counts["cyrillic"]
    return counts[script] / total if total else 0.0


#: Apostrophe-like characters: inside a word they join it ("to‘lov", "don't"), so
#: the raw-word tokenizer must not split there (E-v: "to‘lov" read as English "to").
_APOSTROPHES = str.maketrans(dict.fromkeys("ʻʼ‘’'`´ʹʽ′‛ꞌˈˊˋ", None))


def detect_language(text: str) -> str | None:
    raw = _LETTERS.findall(text.translate(_APOSTROPHES).casefold())
    latin = [w for w in raw if _script_share(w, "latin") == 1.0]
    cyrillic = [w for w in raw if _script_share(w, "cyrillic") == 1.0]
    uzbek_words = _LETTERS.findall(normalize(text))
    scores = {
        "en": sum(word in _ENGLISH for word in latin),
        "ru": sum(word in _RUSSIAN for word in cyrillic),
        "uz": sum(word in _UZBEK for word in uzbek_words),
    }
    ranked = sorted(scores, key=lambda lang: -scores[lang])
    best, second = ranked[0], ranked[1]
    if scores[best] < MIN_LANGUAGE_EVIDENCE or scores[best] == scores[second]:
        return None
    return best


_EXPECTED: dict[OwnerLanguage, tuple[str, Literal["latin", "cyrillic"]]] = {
    "uz-Latn": ("uz", "latin"),
    "uz-Cyrl": ("uz", "cyrillic"),
    "ru": ("ru", "cyrillic"),
    "en": ("en", "latin"),
}


def check_language(text: str, expected: OwnerLanguage) -> LanguageVerdict:
    """The text is in the owner's language *and* script.

    Both must hold: the language by function-word evidence, the script by at
    least `SCRIPT_SHARE_MIN` of the letters. Doubt (no clear language) fails,
    so the check can only be too strict, never too lenient.
    """
    language, script = _EXPECTED[expected]
    share = _script_share(text, script)
    detected = detect_language(text)
    return LanguageVerdict(detected == language and share >= SCRIPT_SHARE_MIN, detected, share)


def language_metric(
    verdicts: Mapping[str, LanguageVerdict], ledger: ReviewLedger, set_size: int | None = None
) -> MetricResult:
    return ratio_metric(
        key="owner_language",
        label="Owner-facing language and script correct",
        outcomes={item: verdict.ok for item, verdict in verdicts.items()},
        threshold=LANGUAGE_THRESHOLD,
        ledger=ledger,
        set_size=set_size,
    )
