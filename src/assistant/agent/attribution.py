"""Who asked, promised or agreed: checked and worded by the application
(ARCHITECTURE §7.5, §11.1; S03.7).

A summary's items name their side in structured fields — the list an item is
in (a question or a decision is the contact's, to the owner) and a
commitment's `by` and `status` — never in their text. Two pure steps:

- **`check_attribution`** compares those fields with the authors of the
  messages each item cites (classification, never text): owner side =
  `owner_manual` + `assistant_sent`; contact side = `contact`;
  `owner_automated` (an away or greeting message) is neutral — a key fact may
  cite it, but it never supports a promise, an agreement or a question.
  An item the citations contradict is **demoted** to what they do show
  (a "promise" citing only the other side is that side's request), and
  dropped only when they show nothing: real open loops are kept, and a
  `requested` item states no more than its citations prove. It runs when a
  summary is written and again when one is read (v1-v4 rows included).
- **`render_who`** words an item's side from fixed per-locale templates and
  the contact's name — no model text ever reaches it.

`WHO_TEMPLATES` is the one table of that wording. The uz-Latn, uz-Cyrl and
ru rows are DRAFTS for the owner to proofread (S03.7): edit them there. They
avoid case suffixes on the name and gendered verbs, so any name fits.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from assistant.agent.locales import SupportedLocale

#: Message directions (classification) per side.
OWNER_SIDE: Final = frozenset({"owner_manual", "assistant_sent"})
CONTACT_SIDE: Final = frozenset({"contact"})
#: Lists whose items are the contact's, addressed to the owner.
CONTACT_LISTS: Final = ("open_questions_to_owner", "decisions_requested")
STATUSES: Final = ("promised", "agreed", "requested")
_SIDES: Final = ("owner", "contact")

#: What a reading agent is told about the `who` lines (owner_request).
ATTRIBUTION_RULE = (
    "Say who asked, promised or agreed exactly as each item's `who` says; never infer it "
    "from the text."
)


@dataclass(frozen=True, slots=True)
class AttributionCounts:
    """What a check changed (counts only: they are logged)."""

    #: Commitments whose status was lowered to what their citations show.
    demoted: int = 0
    #: Items no cited message supports from the side they name.
    dropped: int = 0


#: A check that changed nothing (also the default where none ran).
NO_CHANGES: Final = AttributionCounts()


def _ids(item: Mapping[str, Any]) -> list[int]:
    value = item.get("message_ids")
    if not isinstance(value, list):
        return []
    return [i for i in value if isinstance(i, int) and not isinstance(i, bool)]


def _side(direction: str) -> str | None:
    if direction in OWNER_SIDE:
        return "owner"
    if direction in CONTACT_SIDE:
        return "contact"
    return None  # owner_automated: neutral


def _cited_sides(item: Mapping[str, Any], authors: Mapping[int, str]) -> set[str] | None:
    """The sides the item's citations come from; None when no cited message
    has a known author (an item is then left as it is)."""
    known = [authors[i] for i in _ids(item) if i in authors]
    if not known:
        return None
    return {side for direction in known if (side := _side(direction)) is not None}


def effective_status(item: Mapping[str, Any]) -> str:
    """A commitment's status; a v1-v4 item (none stored) is a promise."""
    status = item.get("status")
    return status if status in STATUSES else "promised"


def _checked_status(status: str, *, own: bool, other: bool) -> str | None:
    """What the citations support: `own` = a message of the side that is to do
    it, `other` = one of the other side. Never upgraded; None = nothing."""
    if status == "agreed" and own and other:
        return "agreed"
    if status in ("promised", "agreed") and own:
        return "promised"
    if other:
        return "requested"
    return None


def check_attribution(
    summary: Mapping[str, Any], authors: Mapping[int, str]
) -> tuple[dict[str, Any], AttributionCounts]:
    """`summary` with every item's side checked against `authors` (message id
    → direction). Returns a new summary; the input is not changed.

    - A question or a decision needs a cited message of the contact.
    - A commitment of P: `promised` needs P's message, `agreed` both sides',
      `requested` the other side's. Short of that it is demoted (agreed →
      promised or requested; promised → requested) or, with neither side's
      message, dropped. A `requested` item is never upgraded.
    - Ids of unknown authorship are ignored; an item citing only such ids
      is kept as it is (a v1-v4 row whose messages are gone).
    """
    checked: dict[str, Any] = dict(summary)
    demoted = dropped = 0
    for key in CONTACT_LISTS:
        items = summary.get(key)
        if not isinstance(items, list):
            continue
        kept = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            sides = _cited_sides(item, authors)
            if sides is not None and "contact" not in sides:
                dropped += 1
                continue
            kept.append(item)
        checked[key] = kept
    commitments = summary.get("commitments")
    if isinstance(commitments, list):
        kept = []
        for item in commitments:
            if not isinstance(item, Mapping):
                continue
            by = item.get("by")
            if by not in _SIDES:
                dropped += 1  # no side, nothing to say who
                continue
            sides = _cited_sides(item, authors)
            if sides is None:
                kept.append(item)
                continue
            other = "contact" if by == "owner" else "owner"
            before = effective_status(item)
            after = _checked_status(before, own=by in sides, other=other in sides)
            if after is None:
                dropped += 1
                continue
            if after != before:
                demoted += 1
            kept.append({**item, "status": after})
        checked["commitments"] = kept
    return checked, AttributionCounts(demoted=demoted, dropped=dropped)


# ------------------------------------------------------------------ wording
#: The owner-facing words for who asked, promised or agreed — one table.
#: en is final; uz-Latn, uz-Cyrl and ru are DRAFTS for the owner to proofread.
#: `{name}` is the contact's name (or `FALLBACK_NAMES`); never add a case
#: suffix to it, so that any name, in any script, fits.
WHO_TEMPLATES: dict[SupportedLocale, dict[str, str]] = {
    "en": {
        "question": "{name} asks you",
        "decision": "{name} is waiting for your decision",
        "promised/owner": "You promised {name}",
        "promised/contact": "{name} promised you",
        "agreed/owner": "Agreed with {name}: you will do it",
        "agreed/contact": "Agreed with {name}: {name} will do it",
        "requested/owner": "{name} asks you to (not agreed yet)",
        "requested/contact": "You asked {name} to (not agreed yet)",
    },
    "uz-Latn": {
        "question": "{name} sizdan so‘rayapti",
        "decision": "{name} sizning qaroringizni kutmoqda",
        "promised/owner": "Siz {name} uchun va’da bergansiz",
        "promised/contact": "{name} sizga va’da bergan",
        "agreed/owner": "{name} bilan kelishilgan: siz bajarasiz",
        "agreed/contact": "{name} bilan kelishilgan: {name} bajaradi",
        "requested/owner": "{name} sizdan so‘ramoqda (siz hali rozi bo‘lmagansiz)",
        "requested/contact": "Siz so‘ragansiz (suhbatdosh: {name}; hali rozilik yo‘q)",
    },
    "uz-Cyrl": {
        "question": "{name} сиздан сўраяпти",
        "decision": "{name} сизнинг қарорингизни кутмоқда",
        "promised/owner": "Сиз {name} учун ваъда бергансиз",
        "promised/contact": "{name} сизга ваъда берган",
        "agreed/owner": "{name} билан келишилган: сиз бажарасиз",
        "agreed/contact": "{name} билан келишилган: {name} бажаради",
        "requested/owner": "{name} сиздан сўрамоқда (сиз ҳали рози бўлмагансиз)",
        "requested/contact": "Сиз сўрагансиз (суҳбатдош: {name}; ҳали розилик йўқ)",
    },
    "ru": {
        "question": "{name} спрашивает вас",
        "decision": "{name} ждёт вашего решения",
        "promised/owner": "Вы обещали (собеседник: {name})",
        "promised/contact": "{name} обещает вам",
        "agreed/owner": "Договорились (собеседник: {name}): делаете вы",
        "agreed/contact": "Договорились (собеседник: {name}): делает {name}",
        "requested/owner": "{name} просит вас (вы ещё не согласились)",
        "requested/contact": "Вы попросили (собеседник: {name}; согласия пока нет)",
    },
}
#: The contact's name when none is known.
FALLBACK_NAMES: dict[SupportedLocale, str] = {
    "en": "the contact",
    "uz-Latn": "suhbatdosh",
    "uz-Cyrl": "суҳбатдош",
    "ru": "собеседник",
}
#: How a summary addresses the owner.
YOU_WORDS: dict[SupportedLocale, str] = {
    "en": "you",
    "uz-Latn": "siz",
    "uz-Cyrl": "сиз",
    "ru": "вы",
}
#: A longer name is cut (Telegram's own limit is 64 + 64 characters).
MAX_NAME = 130


def you_word(locale: SupportedLocale) -> str:
    return YOU_WORDS[locale]


def clean_name(name: str | None) -> str | None:
    """A display name on one line, bounded; None when empty."""
    if name is None:
        return None
    flat = " ".join(name.split())
    return flat[:MAX_NAME] or None


def name_or_fallback(name: str | None, locale: SupportedLocale) -> str:
    return clean_name(name) or FALLBACK_NAMES[locale]


def who_key(list_key: str, item: Mapping[str, Any]) -> str | None:
    """The template an item takes; None for a key fact or an item without a
    side."""
    if list_key == "open_questions_to_owner":
        return "question"
    if list_key == "decisions_requested":
        return "decision"
    if list_key == "commitments" and item.get("by") in _SIDES:
        return f"{effective_status(item)}/{item['by']}"
    return None


def render_who(
    list_key: str,
    item: Mapping[str, Any],
    contact_name: str | None,
    locale: SupportedLocale,
) -> str | None:
    """Who asked, promised or agreed, from the item's list, `by` and `status`
    only — never from its text."""
    key = who_key(list_key, item)
    if key is None:
        return None
    return WHO_TEMPLATES[locale][key].format(name=name_or_fallback(contact_name, locale))
