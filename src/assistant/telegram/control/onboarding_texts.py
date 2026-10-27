"""Fixed texts of onboarding and of the language and time-zone screens
(M03 fix round C4; FR-ONB-01, J1 step 3).

**The one place for these strings**, like `agent/owner_texts.py`: the
application writes them itself, never the model, in each supported locale
(`agent/locales.py`). The English wording is the M01 wording unchanged (the
consent question is owner-approved, M02 D58); the uz-Latn, uz-Cyrl and ru
wording are drafts the owner proofreads in the fix-round PR. Change them
here only. A translated consent question must say exactly what the English
one says: a consent row records the policy version, not the language shown.

Only onboarding and these two screens are localised; /help, /settings,
/privacy and the cards stay English until a wider localisation.

The language picker offers four languages. Telegram's `language_code` of
the owner's control message ("the user's language", commonly their app's;
nothing says it tells Latin from Cyrillic Uzbek; TF §13 PQ-11) only orders
the buttons (`hint_locale`); it is never
saved and never logged except as the locale it maps to.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo

from assistant.agent.locales import SupportedLocale, supported_locale

#: The picker's options, as `(callback op, stored tag, label)`. Ops are
#: callback-safe (`accounts/callbacks.py`); each label is in its own
#: language. Uzbek Latin uses U+02BB in oʻ (M01 D13).
LANGUAGE_OPTIONS: Final[tuple[tuple[str, SupportedLocale, str], ...]] = (
    ("uz_latn", "uz-Latn", "Oʻzbekcha (lotin)"),
    ("uz_cyrl", "uz-Cyrl", "Ўзбекча (кирилл)"),
    ("ru", "ru", "Русский"),
    ("en", "en", "English"),
)
LANGUAGE_BY_OP: Final[dict[str, SupportedLocale]] = {op: tag for op, tag, _ in LANGUAGE_OPTIONS}
OP_BY_LANGUAGE: Final[dict[SupportedLocale, str]] = {tag: op for op, tag, _ in LANGUAGE_OPTIONS}
LANGUAGE_LABELS: Final[dict[SupportedLocale, str]] = {
    tag: label for _, tag, label in LANGUAGE_OPTIONS
}
#: With no usable hint: the owner's audience writes Uzbek first.
DEFAULT_ORDER: Final[tuple[SupportedLocale, ...]] = ("uz-Latn", "uz-Cyrl", "ru", "en")

#: A `language_code` longer than this is not a language tag: no hint. The
#: inbox drops it before it reaches the dispatcher.
MAX_LANGUAGE_HINT_CHARS = 35
_HINT = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8})*")

#: The picker's question, one line per language, shown together.
LANGUAGE_QUESTION: Final[dict[SupportedLocale, str]] = {
    "uz-Latn": "Qaysi tilda yozay? Tilni tanlang.",
    "uz-Cyrl": "Қайси тилда ёзай? Тилни танланг.",
    "ru": "На каком языке мне писать? Выберите язык.",
    "en": "Which language should I use? Choose one.",
}

#: The picker, edited in place once a language is chosen.
LANGUAGE_SAVED: Final[dict[SupportedLocale, str]] = {
    "uz-Latn": (
        "✅ Til: Oʻzbekcha (lotin). Uni istalgan vaqtda /settings orqali oʻzgartirishingiz mumkin."
    ),
    "uz-Cyrl": (
        "✅ Тил: Ўзбекча (кирилл). Уни исталган вақтда /settings орқали ўзгартиришингиз мумкин."
    ),
    "ru": "✅ Язык: русский. Его можно изменить в любое время в /settings.",
    "en": "✅ Language: English. You can change it any time in /settings.",
}

#: The `ai_control` consent question (SEC-02: it names the processors).
CONSENT_PROMPT: Final[dict[SupportedLocale, str]] = {
    "en": (
        "<b>Before we start</b>\n\n"
        "To answer you, I send the text of this chat to "
        "<b>{processors}</b>. Nothing is sent anywhere else, and "
        "your messages are not used to train any model.\n\n"
        "See /privacy for what I store and how to delete it.\n\n"
        "Reply <b>yes</b> to agree, or anything else to decline. "
        "You can revoke this later at any time."
    ),
    "uz-Latn": (
        "<b>Boshlashdan oldin</b>\n\n"
        "Sizga javob berish uchun men ushbu chatdagi matnni <b>{processors}</b> "
        "xizmatiga yuboraman. Boshqa hech qayerga hech narsa yuborilmaydi va "
        "xabarlaringiz hech qanday modelni oʻqitish uchun ishlatilmaydi.\n\n"
        "Nimalarni saqlashim va ularni qanday oʻchirish mumkinligi — /privacy buyrugʻida.\n\n"
        "Rozi boʻlsangiz <b>ha</b> deb yozing; boshqa har qanday javob rad etish deb "
        "hisoblanadi. Bu rozilikni istalgan vaqtda bekor qilishingiz mumkin."
    ),
    "uz-Cyrl": (
        "<b>Бошлашдан олдин</b>\n\n"
        "Сизга жавоб бериш учун мен ушбу чатдаги матнни <b>{processors}</b> "
        "хизматига юбораман. Бошқа ҳеч қаерга ҳеч нарса юборилмайди ва "
        "хабарларингиз ҳеч қандай моделни ўқитиш учун ишлатилмайди.\n\n"
        "Нималарни сақлашим ва уларни қандай ўчириш мумкинлиги — /privacy буйруғида.\n\n"
        "Рози бўлсангиз <b>ҳа</b> деб ёзинг; бошқа ҳар қандай жавоб рад этиш деб "
        "ҳисобланади. Бу розиликни исталган вақтда бекор қилишингиз мумкин."
    ),
    "ru": (
        "<b>Прежде чем начать</b>\n\n"
        "Чтобы отвечать вам, я отправляю текст этого чата в сервис <b>{processors}</b>. "
        "Больше никуда ничего не отправляется, и ваши сообщения не используются для "
        "обучения каких-либо моделей.\n\n"
        "Что я храню и как это удалить — в /privacy.\n\n"
        "Напишите <b>да</b>, чтобы согласиться; любой другой ответ означает отказ. "
        "Согласие можно отозвать в любой момент."
    ),
}

#: The word each consent question asks for. `handlers.AGREEMENT_WORDS`
#: includes all of them.
CONSENT_WORD: Final[dict[SupportedLocale, str]] = {
    "en": "yes",
    "uz-Latn": "ha",
    "uz-Cyrl": "ҳа",
    "ru": "да",
}

#: Each names a word in `handlers.STOP_WORDS`.
CONSENT_GRANTED: Final[dict[SupportedLocale, str]] = {
    "en": "Done. Ask me anything, or see /help.\n\nSend <b>stop</b> to pause.",
    "uz-Latn": (
        "Tayyor. Istalgan savolingizni bering yoki /help ni koʻring.\n\n"
        "Toʻxtatish uchun <b>toʻxta</b> deb yozing."
    ),
    "uz-Cyrl": (
        "Тайёр. Исталган саволингизни беринг ёки /help ни кўринг.\n\n"
        "Тўхтатиш учун <b>тўхта</b> деб ёзинг."
    ),
    "ru": (
        "Готово. Спрашивайте что угодно или посмотрите /help.\n\n"
        "Чтобы поставить на паузу, напишите <b>стоп</b>."
    ),
}

CONSENT_DECLINED: Final[dict[SupportedLocale, str]] = {
    "en": (
        "No problem — nothing will be sent to an AI provider.\n"
        "/settings and /privacy still work. Run /start again to change your mind."
    ),
    "uz-Latn": (
        "Mayli — AI xizmatiga hech narsa yuborilmaydi.\n"
        "/settings va /privacy ishlashda davom etadi. Fikringiz oʻzgarsa, /start ni "
        "qayta yuboring."
    ),
    "uz-Cyrl": (
        "Майли — AI хизматига ҳеч нарса юборилмайди.\n"
        "/settings ва /privacy ишлашда давом этади. Фикрингиз ўзгарса, /start ни "
        "қайта юборинг."
    ),
    "ru": (
        "Хорошо — сервису ИИ ничего не будет отправляться.\n"
        "/settings и /privacy продолжают работать. Если передумаете, снова отправьте /start."
    ),
}

#: /start once consent is given. Above the picker (in English: no language
#: is known yet) for an account set up before C4 without one.
ALREADY_SET_UP: Final[dict[SupportedLocale, str]] = {
    "en": "You are already set up. /help shows what I can do.",
    "uz-Latn": "Hammasi sozlangan. Nimalar qila olishimni /help koʻrsatadi.",
    "uz-Cyrl": "Ҳаммаси созланган. Нималар қила олишимни /help кўрсатади.",
    "ru": "Всё уже настроено. Что я умею — в /help.",
}

#: The time-zone card, once, after the consent answer (C4).
TIMEZONE_QUESTION: Final[dict[SupportedLocale, str]] = {
    "en": (
        "🕒 <b>Which time zone are you in?</b> I show times and dates in it.\n\n"
        "Other: <code>/settings timezone Area/City</code>"
    ),
    "uz-Latn": (
        "🕒 <b>Qaysi vaqt mintaqasidasiz?</b> Vaqt va sanalarni shu mintaqada koʻrsataman.\n\n"
        "Boshqasi: <code>/settings timezone Area/City</code>"
    ),
    "uz-Cyrl": (
        "🕒 <b>Қайси вақт минтақасидасиз?</b> Вақт ва саналарни шу минтақада кўрсатаман.\n\n"
        "Бошқаси: <code>/settings timezone Area/City</code>"
    ),
    "ru": (
        "🕒 <b>В каком вы часовом поясе?</b> Время и даты я показываю в нём.\n\n"
        "Другой: <code>/settings timezone Area/City</code>"
    ),
}

TIMEZONE_SAVED: Final[dict[SupportedLocale, str]] = {
    "en": "✅ Time zone: {zone}. You can change it any time in /settings.",
    "uz-Latn": (
        "✅ Vaqt mintaqasi: {zone}. Uni istalgan vaqtda /settings orqali oʻzgartirishingiz mumkin."
    ),
    "uz-Cyrl": (
        "✅ Вақт минтақаси: {zone}. Уни исталган вақтда /settings орқали ўзгартиришингиз мумкин."
    ),
    "ru": "✅ Часовой пояс: {zone}. Его можно изменить в любое время в /settings.",
}

#: The card's options, as `(callback op, IANA zone)`.
TIMEZONE_OPTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("tashkent", "Asia/Tashkent"),
    ("moscow", "Europe/Moscow"),
    ("utc", "UTC"),
)
TIMEZONE_BY_OP: Final[dict[str, str]] = dict(TIMEZONE_OPTIONS)
CITY_NAMES: Final[dict[SupportedLocale, dict[str, str]]] = {
    "en": {"Asia/Tashkent": "Tashkent", "Europe/Moscow": "Moscow", "UTC": "UTC"},
    "uz-Latn": {"Asia/Tashkent": "Toshkent", "Europe/Moscow": "Moskva", "UTC": "UTC"},
    "uz-Cyrl": {"Asia/Tashkent": "Тошкент", "Europe/Moscow": "Москва", "UTC": "UTC"},
    "ru": {"Asia/Tashkent": "Ташкент", "Europe/Moscow": "Москва", "UTC": "UTC"},
}

Rows = list[list[tuple[str, str]]]


def hint_locale(hint: str | None) -> SupportedLocale | None:
    """The supported locale Telegram's `language_code` points at, or None
    for a missing, malformed or other language. Only `uz` with a Cyrillic
    script subtag means uz-Cyrl: a bare `uz` is the Latin default."""
    if not hint or len(hint) > MAX_LANGUAGE_HINT_CHARS or not _HINT.fullmatch(hint):
        return None
    parts = hint.lower().replace("_", "-").split("-")
    if parts[0] == "uz":
        return "uz-Cyrl" if "cyrl" in parts[1:] else "uz-Latn"
    if parts[0] in ("ru", "en"):
        return supported_locale(parts[0])
    return None


def language_order(first: SupportedLocale | None) -> tuple[SupportedLocale, ...]:
    """The four languages, `first` first, the rest in the default order."""
    if first is None:
        return DEFAULT_ORDER
    return (first, *(locale for locale in DEFAULT_ORDER if locale != first))


def chosen_locale(tag: str | None) -> SupportedLocale | None:
    """The picker option a stored language tag is (`uz` is uz-Latn, `ru-RU`
    is ru), or None when unset or another language (`kk`)."""
    if not tag:
        return None
    locale = supported_locale(tag)
    same_language = tag.strip().lower().partition("-")[0] == locale.lower().partition("-")[0]
    return locale if same_language else None


def language_label(tag: str | None) -> str:
    """The language as /settings shows it: "not set" when unset, never "auto"."""
    if not tag:
        return "not set"
    locale = chosen_locale(tag)
    return LANGUAGE_LABELS[locale] if locale is not None else tag


def picker_text(order: Sequence[SupportedLocale]) -> str:
    return "🌐 " + "\n".join(LANGUAGE_QUESTION[locale] for locale in order)


def picker_rows(order: Sequence[SupportedLocale], *, ticked: SupportedLocale | None = None) -> Rows:
    """Two rows of two, in `order`; `ticked` marks the current language."""
    buttons = [
        (LANGUAGE_LABELS[locale] + (" ✓" if locale == ticked else ""), OP_BY_LANGUAGE[locale])
        for locale in order
    ]
    return [buttons[:2], buttons[2:]]


def consent_prompt(locale: SupportedLocale, processors: Sequence[str]) -> str:
    return CONSENT_PROMPT[locale].format(processors=", ".join(processors))


def _offset(zone: str, now: datetime) -> str:
    """`UTC+5`, `UTC+5:30`, `UTC` — at `now`, so it is never stale."""
    offset = ZoneInfo(zone).utcoffset(now)
    minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    if minutes == 0:
        return "UTC"
    sign = "+" if minutes > 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{rest:02d}" if rest else "")


def timezone_rows(locale: SupportedLocale, now: datetime) -> Rows:
    """One row: each zone by its city's name in the owner's language and
    its current offset (UTC alone needs neither)."""
    buttons = []
    for op, zone in TIMEZONE_OPTIONS:
        offset = _offset(zone, now)
        name = CITY_NAMES[locale][zone]
        buttons.append((name if name == offset else f"{name} ({offset})", op))
    return [buttons]
