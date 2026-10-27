"""Fixed owner-facing texts: the retrieval answer (S03.7 B3, B5) and the
conversation modes (M03 fix round C3).

**The one place for these strings.** The application writes them itself,
never the model, in each supported locale (`agent/locales.py`). The uz-Latn,
uz-Cyrl and ru wording are drafts the owner proofreads in the slice PR;
change them here only.

- `YOU`, `CONTACT_FALLBACK`: the name in a cited message's source, e.g.
  "(Aziza, 2026-10-12 14:05)" or "(you, …)" for the owner's own message;
- `CITATION_REMOVED`: appended once when the reply cited a message no tool
  returned in the run, and the marker was removed (`agent/citations.py`);
- `CHAT_ACCESS_OFF`: appended to a reply whose run needed the owner's chats
  while AI for them is off; the `/privacy managed` card follows (D104), but
  not within a day of the owner choosing "Not now" (D110);
- `MAY_BE_WAITING`: the reason the attention list and a catch-up give for a
  chat whose contact asked a question after the owner's last answer — the
  application's own check, not a model's (M03 fix round, C1);
- `STILL_OPEN`: the reason for "needs a reply" when the summary said none is
  needed but a question, decision or request to the owner is still open (P9).

The last two are drafts too (uz-Latn, uz-Cyrl, ru); they hold no name, and
the ru wording uses no gendered verb.

The mode texts (C3) say exactly what Ignore and Observe do, for the
conversation panel, the Ignore confirmation and the `set_conversation_mode`
echo (`mode_receipt`, `mode_unchanged`, `mode_line`, `ignore_effects`).
Button names (Observe, Ignore, Delete stored data…) stay in English, as on
the buttons; a contact's name is never inflected (Russian case), so in
Russian it stands alone after "chat:" and in Uzbek before "bilan".
"""

from __future__ import annotations

import html
from typing import Final

from assistant.agent.locales import SupportedLocale

#: The owner, in a source line.
YOU: Final[dict[SupportedLocale, str]] = {
    "en": "you",
    "uz-Latn": "siz",
    "uz-Cyrl": "сиз",
    "ru": "вы",
}

#: A contact without a stored name, in a source line.
CONTACT_FALLBACK: Final[dict[SupportedLocale, str]] = {
    "en": "the contact",
    "uz-Latn": "suhbatdosh",
    "uz-Cyrl": "суҳбатдош",
    "ru": "собеседник",
}

CITATION_REMOVED: Final[dict[SupportedLocale, str]] = {
    "en": "A source I cited did not match the messages I found, so I removed it.",
    "uz-Latn": (
        "Men keltirgan manbalardan biri topilgan xabarlarga mos kelmadi, shuning uchun "
        "uni olib tashladim."
    ),
    "uz-Cyrl": (
        "Мен келтирган манбалардан бири топилган хабарларга мос келмади, шунинг учун "
        "уни олиб ташладим."
    ),
    "ru": ("Одна из ссылок на источник не совпала с найденными сообщениями, поэтому я её убрал."),
}

CHAT_ACCESS_OFF: Final[dict[SupportedLocale, str]] = {
    "en": (
        "I can't read your chats: AI for your connected chats is off. "
        "Send /privacy managed to turn it on."
    ),
    "uz-Latn": (
        "Chatlaringizni oʻqiy olmayman: ulangan chatlar uchun AI oʻchirilgan. "
        "Yoqish uchun /privacy managed yuboring."
    ),
    "uz-Cyrl": (
        "Чатларингизни ўқий олмайман: уланган чатлар учун AI ўчирилган. "
        "Ёқиш учун /privacy managed юборинг."
    ),
    "ru": (
        "Я не могу читать ваши чаты: ИИ для подключённых чатов выключен. "
        "Чтобы включить, отправьте /privacy managed."
    ),
}

MAY_BE_WAITING: Final[dict[SupportedLocale, str]] = {
    "en": (
        "May be waiting for your reply: their latest messages include a question that "
        "nobody has answered."
    ),
    "uz-Latn": (
        "Javobingizni kutayotgan boʻlishi mumkin: soʻnggi xabarlarida savol bor, unga hali "
        "hech kim javob bermagan."
    ),
    "uz-Cyrl": (
        "Жавобингизни кутаётган бўлиши мумкин: сўнгги хабарларида савол бор, унга ҳали "
        "ҳеч ким жавоб бермаган."
    ),
    "ru": (
        "Возможно, ждёт вашего ответа: в последних сообщениях собеседника есть вопрос, "
        "на который ещё нет ответа."
    ),
}

STILL_OPEN: Final[dict[SupportedLocale, str]] = {
    "en": (
        "A reply is still needed: a question, decision or request to you in this chat is "
        "still open."
    ),
    "uz-Latn": (
        "Javob hali kerak: bu chatda sizga qaratilgan savol, qaror yoki iltimos hali yopilmagan."
    ),
    "uz-Cyrl": (
        "Жавоб ҳали керак: бу чатда сизга қаратилган савол, қарор ёки илтимос ҳали ёпилмаган."
    ),
    "ru": "Ответ всё ещё нужен: в этом чате остаётся открытым вопрос, решение или просьба к вам.",
}

# ------------------------------------------------------------ conversation modes (C3)
#: The button names, in every locale (the buttons are English).
MODE_NAMES: Final[dict[str, str]] = {"observe": "Observe", "ignore": "Ignore"}

#: The panel's mode line, by mode.
MODE_LINE: Final[dict[SupportedLocale, dict[str, str]]] = {
    "en": {
        "ignore": "Mode: <b>Ignore</b> (new messages here are not saved)",
        "observe": "Mode: <b>Observe</b> (new messages here are saved)",
    },
    "uz-Latn": {
        "ignore": "Rejim: <b>Ignore</b> (bu chatdagi yangi xabarlar saqlanmaydi)",
        "observe": "Rejim: <b>Observe</b> (bu chatdagi yangi xabarlar saqlanadi)",
    },
    "uz-Cyrl": {
        "ignore": "Режим: <b>Ignore</b> (бу чатдаги янги хабарлар сақланмайди)",
        "observe": "Режим: <b>Observe</b> (бу чатдаги янги хабарлар сақланади)",
    },
    "ru": {
        "ignore": "Режим: <b>Ignore</b> (новые сообщения из этого чата не сохраняются)",
        "observe": "Режим: <b>Observe</b> (новые сообщения из этого чата сохраняются)",
    },
}

#: A receipt's first line, by the mode now on. `{name}`: escaped by `mode_receipt`.
MODE_ON: Final[dict[SupportedLocale, dict[str, str]]] = {
    "en": {
        "ignore": "<b>Ignore is on</b> for the chat with {name}.",
        "observe": "<b>Observe is on</b> for the chat with {name}.",
    },
    "uz-Latn": {
        "ignore": "<b>Ignore rejimi yoqildi</b> — {name} bilan chat.",
        "observe": "<b>Observe rejimi yoqildi</b> — {name} bilan chat.",
    },
    "uz-Cyrl": {
        "ignore": "<b>Ignore режими ёқилди</b> — {name} билан чат.",
        "observe": "<b>Observe режими ёқилди</b> — {name} билан чат.",
    },
    "ru": {
        "ignore": "<b>Режим Ignore включён</b> — чат: {name}.",
        "observe": "<b>Режим Observe включён</b> — чат: {name}.",
    },
}

#: Ignore: what `record_message` keeps (a metadata-only row, no name).
IGNORE_NOT_SAVED: Final[dict[SupportedLocale, str]] = {
    "en": (
        "New messages here are not saved: no text, media or new name, just a note "
        "that a message arrived."
    ),
    "uz-Latn": (
        "Bu chatdagi yangi xabarlar saqlanmaydi: na matn, na media, na yangi ism — "
        "faqat xabar kelgani qayd etiladi."
    ),
    "uz-Cyrl": (
        "Бу чатдаги янги хабарлар сақланмайди: на матн, на медиа, на янги исм — "
        "фақат хабар келгани қайд этилади."
    ),
    "ru": (
        "Новые сообщения из этого чата не сохраняются: ни текст, ни медиа, ни новое "
        "имя — только отметка, что сообщение пришло."
    ),
}

#: Ignore: the rolling summary, catch-ups and the attention list skip the chat.
IGNORE_LEFT_OUT: Final[dict[SupportedLocale, str]] = {
    "en": (
        "The chat is left out of summaries, catch-ups and the list of who is waiting "
        "for your reply."
    ),
    "uz-Latn": (
        "Bu chat xulosalar, catch-up sharhlari va javobingizni kutayotganlar roʻyxatiga "
        "kiritilmaydi."
    ),
    "uz-Cyrl": (
        "Бу чат хулосалар, catch-up шарҳлари ва жавобингизни кутаётганлар рўйхатига киритилмайди."
    ),
    "ru": (
        "Этот чат не попадает в сводки, обзоры (catch-up) и список тех, кто ждёт вашего ответа."
    ),
}

#: Ignore: what stays (D73, D74). `{days}`: the account's message retention.
#: "Unless deleted" covers a deletion in the chat (it still purges, and drops
#: the summary that covered it) and the owner's own deletions.
IGNORE_KEPT: Final[dict[SupportedLocale, str]] = {
    "en": (
        "What was saved before stays unless deleted: messages until {days} days old, "
        "the summary and your notes. Ignore does not hide them from my answers."
    ),
    "uz-Latn": (
        "Avval saqlanganlar oʻchirilmaguncha qoladi: xabarlar — {days} kungacha, xulosa "
        "va eslatmalaringiz. Ignore rejimi ularni javoblarimdan yashirmaydi."
    ),
    "uz-Cyrl": (
        "Аввал сақланганлар ўчирилмагунча қолади: хабарлар — {days} кунгача, хулоса "
        "ва эслатмаларингиз. Ignore режими уларни жавобларимдан яширмайди."
    ),
    "ru": (
        "Сохранённое раньше остаётся, пока его не удалят: сообщения — пока им не "
        "исполнится {days} дн., сводка и ваши заметки. Режим Ignore не скрывает их "
        "от моих ответов."
    ),
}

IGNORE_DELETE_HINT: Final[dict[SupportedLocale, str]] = {
    "en": "To delete them: Delete stored data… on this chat's panel (/settings → Ignored chats).",
    "uz-Latn": (
        "Oʻchirish uchun: shu chat panelidagi «Delete stored data…» (/settings → Ignored chats)."
    ),
    "uz-Cyrl": (
        "Ўчириш учун: шу чат панелидаги «Delete stored data…» (/settings → Ignored chats)."
    ),
    "ru": "Чтобы удалить: «Delete stored data…» в панели этого чата (/settings → Ignored chats).",
}

#: Observe: what `record_message` stores (media as structural facts only).
OBSERVE_SAVED: Final[dict[SupportedLocale, str]] = {
    "en": (
        "New messages here are saved (text, media details but not the files, the "
        "contact's name) and used for summaries, catch-ups, search and the list of who "
        "is waiting for your reply."
    ),
    "uz-Latn": (
        "Bu chatdagi yangi xabarlar saqlanadi (matn, media haqida maʼlumot — fayllarning "
        "oʻzi emas — va suhbatdosh ismi) hamda xulosalar, catch-up sharhlari, qidiruv va "
        "javobingizni kutayotganlar roʻyxati uchun ishlatiladi."
    ),
    "uz-Cyrl": (
        "Бу чатдаги янги хабарлар сақланади (матн, медиа ҳақида маълумот — файлларнинг "
        "ўзи эмас — ва суҳбатдош исми) ҳамда хулосалар, catch-up шарҳлари, қидирув ва "
        "жавобингизни кутаётганлар рўйхати учун ишлатилади."
    ),
    "ru": (
        "Новые сообщения из этого чата сохраняются (текст, сведения о медиа без самих "
        "файлов, имя собеседника) и используются для сводок, обзоров (catch-up), поиска "
        "и списка тех, кто ждёт вашего ответа."
    ),
}

#: Observe after Ignore: there is no history API (AGENTS §7). Not "cannot be
#: recovered": a later edit in Observe, or a requeued failed update, stores
#: such a message after all (C3 review).
OBSERVE_GAP: Final[dict[SupportedLocale, str]] = {
    "en": (
        "Messages that arrived while the chat was ignored were not saved: Telegram does "
        "not give me chat history."
    ),
    "uz-Latn": (
        "Chat Ignore rejimida boʻlganida kelgan xabarlar saqlanmagan: Telegram menga chat "
        "tarixini bermaydi."
    ),
    "uz-Cyrl": (
        "Чат Ignore режимида бўлганида келган хабарлар сақланмаган: Telegram менга чат "
        "тарихини бермайди."
    ),
    "ru": (
        "Сообщения, пришедшие, пока чат был в режиме Ignore, не сохранены: Telegram не "
        "даёт мне историю чата."
    ),
}

#: Observe without `ai_managed` consent: stored, not read by AI (§14.3).
OBSERVE_AI_OFF: Final[dict[SupportedLocale, str]] = {
    "en": (
        "AI for your connected chats is off: I save these messages but do not read them "
        "with AI. Send /privacy managed to turn it on."
    ),
    "uz-Latn": (
        "Ulangan chatlar uchun AI oʻchirilgan: bu xabarlarni saqlayman, lekin AI bilan "
        "oʻqimayman. Yoqish uchun /privacy managed yuboring."
    ),
    "uz-Cyrl": (
        "Уланган чатлар учун AI ўчирилган: бу хабарларни сақлайман, лекин AI билан "
        "ўқимайман. Ёқиш учун /privacy managed юборинг."
    ),
    "ru": (
        "ИИ для подключённых чатов выключен: я сохраняю эти сообщения, но не читаю их "
        "с помощью ИИ. Чтобы включить, отправьте /privacy managed."
    ),
}

#: The chat was already in that mode (M03 D68). `{mode}`: the button's name.
MODE_UNCHANGED: Final[dict[SupportedLocale, str]] = {
    "en": "Nothing to change: the chat with {name} is already in {mode}.",
    "uz-Latn": "Oʻzgartiradigan narsa yoʻq: {name} bilan chat allaqachon {mode} rejimida.",
    "uz-Cyrl": "Ўзгартирадиган нарса йўқ: {name} билан чат аллақачон {mode} режимида.",
    "ru": "Менять нечего: чат ({name}) уже в режиме {mode}.",
}

#: The panel's counts: rows with content, and rows that arrived in Ignore.
SAVED_COUNT: Final[dict[SupportedLocale, str]] = {
    "en": "Messages saved: {saved}",
    "uz-Latn": "Saqlangan xabarlar: {saved}",
    "uz-Cyrl": "Сақланган хабарлар: {saved}",
    "ru": "Сохранено сообщений: {saved}",
}
NOT_SAVED_COUNT: Final[dict[SupportedLocale, str]] = {
    "en": "Not saved (arrived while ignored): {unsaved}",
    "uz-Latn": "Saqlanmagan (Ignore rejimida kelgan): {unsaved}",
    "uz-Cyrl": "Сақланмаган (Ignore режимида келган): {unsaved}",
    "ru": "Не сохранено (пришли в режиме Ignore): {unsaved}",
}


def _who(locale: SupportedLocale, name: str | None) -> str:
    """The contact's stored name, HTML-escaped, or the fixed fallback."""
    return html.escape(name, quote=False) if name else CONTACT_FALLBACK[locale]


def mode_line(locale: SupportedLocale, mode: str) -> str:
    """The panel's mode line; a mode without a text yet (`assist`, M04) is
    shown by its value."""
    line = MODE_LINE[locale].get(mode)
    return line if line is not None else f"Mode: {html.escape(mode, quote=False)}"


def ignore_effects(locale: SupportedLocale, *, days: int) -> tuple[str, ...]:
    """What Ignore does, as bullet lines: for the confirmation and the receipt."""
    return (
        "• " + IGNORE_NOT_SAVED[locale],
        "• " + IGNORE_LEFT_OUT[locale],
        "• " + IGNORE_KEPT[locale].format(days=days),
    )


def mode_receipt(
    locale: SupportedLocale,
    *,
    mode: str,
    previous: str,
    name: str | None,
    days: int,
    ai_on: bool,
    deleting: bool = False,
) -> str:
    """What a mode change did, in a few fixed lines (§6 invariant 11).

    `name`: the contact's stored name (escaped here), or None. `days`: the
    account's message retention. `ai_on`: `ai_managed` consent is granted.
    `deleting`: the chat's data is being deleted with the switch (Ignore +
    delete data), so nothing is said to stay."""
    lines = [MODE_ON[locale][mode].format(name=_who(locale, name))]
    if mode == "ignore":
        effects = ignore_effects(locale, days=days)
        lines += effects[:2] if deleting else [*effects, "• " + IGNORE_DELETE_HINT[locale]]
    else:
        lines.append("• " + OBSERVE_SAVED[locale])
        if previous == "ignore":
            lines.append("• " + OBSERVE_GAP[locale])
        if not ai_on:
            lines.append("• " + OBSERVE_AI_OFF[locale])
    return "\n".join(lines)


def mode_unchanged(locale: SupportedLocale, *, mode: str, name: str | None) -> str:
    """The chat was already in `mode`: nothing to change (M03 D68)."""
    return MODE_UNCHANGED[locale].format(
        name=_who(locale, name), mode=MODE_NAMES.get(mode, html.escape(mode, quote=False))
    )
