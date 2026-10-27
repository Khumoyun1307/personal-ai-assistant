"""Fixed owner-facing texts of the retrieval answer (S03.7 B3, B5).

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
  not within a day of the owner choosing "Not now" (D110).
"""

from __future__ import annotations

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
