"""Handwritten synthetic transcripts; scripts are fixtures, not language ratings."""

from dataclasses import dataclass

from tests.journeys.models import Locale


@dataclass(frozen=True)
class Transcript:
    dictate: str
    choice: str
    recipient_question: str
    empty: str
    content_question: str
    brief: str
    generated: str
    unique: str
    who: str
    revise: str
    revised: str
    outcome: str
    rejected: str
    advice: str
    list_answer: str


TEXT: dict[Locale, Transcript] = {
    "uz-Latn": Transcript(
        dictate="Kamolaga aynan yoz: X1 — uchrashuv bekor. Oʻzgartirma!",
        choice="Kamola Ziyo.",
        recipient_question="Qaysi Kamola: Kamola Ziyomi yoki Kamola Safomi?",
        empty="Kamola Ziyoga yoz.",
        content_question="Kamola Ziyoga nima yozay?",
        brief="Juma kungi kitob yetkazilishini soʻra. 20 soʻzdan oshmasin.",
        generated="Juma kuni kitob yetkaziladimi?",
        unique="Kamolaga aynan yoz: Kitob keldi, X1.",
        who="Kimga tayyorlading?",
        revise="Buni qisqaroq qil, 10 soʻzdan oshmasin.",
        revised="Juma kuni kitob keladimi?",
        outcome="Oʻsha xabar nima boʻldi va unda nima yozilgandi?",
        rejected="Rad etildi, yuborilmadi.",
        advice="Roʻyxatni qisqartir, hech kimga xabar yozma.",
        list_answer="1. Kitob.\n2. Juma.",
    ),
    "uz-Cyrl": Transcript(
        dictate="Камолага айнан ёз: X1 — учрашув бекор. Ўзгартирма!",
        choice="Камола Зиё.",
        recipient_question="Қайси Камола: Камола Зиёми ёки Камола Сафоми?",
        empty="Камола Зиёга ёз.",
        content_question="Камола Зиёга нима ёзай?",
        brief="Жума кунги китоб етказилишини сўра. 20 сўздан ошмасин.",
        generated="Жума куни китоб етказиладими?",
        unique="Камолага айнан ёз: Китоб келди, X1.",
        who="Кимга тайёрладинг?",
        revise="Буни қисқароқ қил, 10 сўздан ошмасин.",
        revised="Жума куни китоб келадими?",
        outcome="Ўша хабар нима бўлди ва унда нима ёзилганди?",
        rejected="Рад этилди, юборилмади.",
        advice="Рўйхатни қисқартир, ҳеч кимга хабар ёзма.",
        list_answer="1. Китоб.\n2. Жума.",
    ),
    "ru": Transcript(
        dictate="Напиши Камоле дословно: X1 — встреча отменена. Не меняй!",
        choice="Камола Зиё.",
        recipient_question="Какой Камоле: Камоле Зиё или Камоле Сафо?",
        empty="Напиши Камоле Зиё.",
        content_question="Что написать Камоле Зиё?",
        brief="Спроси о доставке книг в пятницу. Не больше 20 слов.",
        generated="Доставят ли книги в пятницу?",
        unique="Напиши Камоле дословно: Книга пришла, X1.",
        who="Для кого подготовлено?",
        revise="Сделай это короче, не больше 10 слов.",
        revised="Книга придёт в пятницу?",
        outcome="Что стало с тем сообщением и что в нём было?",
        rejected="Отклонено, не отправлено.",
        advice="Сократи список, никому не пиши.",
        list_answer="1. Книга.\n2. Пятница.",
    ),
    "en": Transcript(
        dictate="Write to Kamola exactly: X1 — meeting cancelled. Keep this!",
        choice="Kamola Ziyo.",
        recipient_question="Which Kamola: Kamola Ziyo or Kamola Safo?",
        empty="Write to Kamola Ziyo.",
        content_question="What should I write to Kamola Ziyo?",
        brief="Ask about Friday's book delivery. At most 20 words.",
        generated="Will the books be delivered on Friday?",
        unique="Write to Kamola exactly: The book arrived, X1.",
        who="Who is the draft for?",
        revise="Make this shorter, at most 10 words.",
        revised="Will the book arrive Friday?",
        outcome="What happened to that message, and what did it say?",
        rejected="Rejected, not sent.",
        advice="Shorten the list; do not write to anyone.",
        list_answer="1. Book.\n2. Friday.",
    ),
}

QUERIES: dict[Locale, str] = {
    "uz-Latn": "Kamola",
    "uz-Cyrl": "Камола",
    "ru": "Камола",
    "en": "Kamola",
}

OUTDATED: dict[Locale, str] = {
    "uz-Latn": "Eskirdi, yuborilmadi.",
    "uz-Cyrl": "Эскирди, юборилмади.",
    "ru": "Устарело, не отправлено.",
    "en": "Outdated, not sent.",
}

UNAVAILABLE: dict[Locale, str] = {
    "uz-Latn": "Matn mavjud emas.",
    "uz-Cyrl": "Матн мавжуд эмас.",
    "ru": "Текст недоступен.",
    "en": "Text unavailable.",
}

SENT: dict[Locale, str] = {
    "uz-Latn": "Telegram yuborilganini tasdiqladi.",
    "uz-Cyrl": "Telegram юборилганини тасдиқлади.",
    "ru": "Telegram подтвердил отправку.",
    "en": "Telegram confirmed sent.",
}

UNCERTAIN: dict[Locale, str] = {
    "uz-Latn": "Yetkazilganligi noaniq.",
    "uz-Cyrl": "Етказилганлиги ноаниқ.",
    "ru": "Доставка не подтверждена.",
    "en": "Delivery uncertain.",
}

OWNER_LABEL: dict[Locale, str] = {
    "uz-Latn": "Sizning soʻzlaringiz",
    "uz-Cyrl": "Сизнинг сўзларингиз",
    "ru": "Ваши слова",
    "en": "Your words",
}

BOOK_QUESTION: dict[Locale, str] = {
    "uz-Latn": "Kitob keldimi?",
    "uz-Cyrl": "Китоб келдими?",
    "ru": "Книга пришла?",
    "en": "Has the book arrived?",
}

CONSTRAINT_QUESTION: dict[Locale, str] = {
    "uz-Latn": "Necha soʻz boʻlsin?",
    "uz-Cyrl": "Неча сўз бўлсин?",
    "ru": "Сколько слов оставить?",
    "en": "How many words should it be?",
}
