"""Multilingual message search and the search re-key (S03.1, ARCHITECTURE §11.2).

Messages arrive through the real inbox (`business_env`), so they carry the
key ingestion writes; the search runs the production SQL against Postgres 18
with `pg_trgm`. All text is synthetic.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update

from assistant.conversations.models import Message
from assistant.memory import search_index
from assistant.memory.retrieval import SearchFilters, search_messages
from assistant.memory.search import NORMALIZER_VERSION, normalize
from tests.integration.business_env import (
    CONTACT,
    OTHER_CONTACT,
    OWNER,
    T_BASE,
    Env,
    business_message,
    connect,
    connection_update,
    deleted_messages,
    deliver,
    environment,
    private_message,
)

pytestmark = pytest.mark.integration

_ids = iter(range(100, 10_000))


async def say(env: Env, message_text: str, *, contact: int = CONTACT, days_ago: float = 0) -> int:
    """A contact message `days_ago` before the base date. Returns its Telegram id."""
    message_id = next(_ids)
    await deliver(
        env,
        business_message(
            from_id=contact,
            chat_id=contact,
            message_id=message_id,
            message_text=message_text,
            date=T_BASE - int(days_ago * 86400),
        ),
    )
    return message_id


async def account_id(env: Env, owner: int = OWNER) -> int:
    async with env.factory() as session:
        return int(
            (
                await session.execute(
                    text("SELECT id FROM app.accounts WHERE telegram_user_id = :o"), {"o": owner}
                )
            ).scalar_one()
        )


async def find(env: Env, *queries: str, owner: int = OWNER, **kwargs: Any) -> list[str]:
    async with env.factory() as session:
        result = await search_messages(
            session, await account_id(env, owner), queries, now=env.clock.now(), **kwargs
        )
    return [hit.text or "" for hit in result.hits]


# ------------------------------------------------------------ cross-script
async def test_a_latin_query_finds_a_cyrillic_message(env: Env) -> None:
    await connect(env)
    await say(env, "Эртага Ўзбекистон ҳақида гаплашамиз")
    await say(env, "Unrelated synthetic note")
    assert await find(env, "ozbekiston") == ["Эртага Ўзбекистон ҳақида гаплашамиз"]
    assert await find(env, "Oʻzbekiston haqida") == ["Эртага Ўзбекистон ҳақида гаплашамиз"]


async def test_a_cyrillic_query_finds_a_latin_message(env: Env) -> None:
    await connect(env)
    await say(env, "Ertaga G'alaba bog'ida uchrashamiz")
    assert await find(env, "ғалаба боғида") == ["Ertaga G'alaba bog'ida uchrashamiz"]


@pytest.mark.parametrize(
    "query",
    ["oʻzbekiston", "o'zbekiston", "o‘zbekiston", "o’zbekiston", "o`zbekiston", "o´zbekiston"],
)
async def test_every_apostrophe_variant_matches(env: Env, query: str) -> None:
    await connect(env)
    await say(env, "Oʻzbekiston kubogi finali")
    assert await find(env, query) == ["Oʻzbekiston kubogi finali"]


async def test_apostrophe_less_spellings_match(env: Env) -> None:
    await connect(env)
    await say(env, "Gʻalaba kuni Oʻzbekistonda dam olamiz")
    assert await find(env, "ozbekiston") == ["Gʻalaba kuni Oʻzbekistonda dam olamiz"]
    assert await find(env, "galaba") == ["Gʻalaba kuni Oʻzbekistonda dam olamiz"]


async def test_new_alphabet_letters_match_old_forms_and_cyrillic(env: Env) -> None:
    await connect(env)
    await say(env, "Özbekistonda ğalaba bayrami")
    await say(env, "Ўзбекистонда ғалаба байрами")
    await say(env, "O'zbekistonda g'alaba bayrami")
    found = await find(env, "Oʻzbekistonda gʻalaba")
    assert sorted(found) == sorted(
        [
            "Özbekistonda ğalaba bayrami",
            "Ўзбекистонда ғалаба байрами",
            "O'zbekistonda g'alaba bayrami",
        ]
    )


async def test_latin_typed_russian_finds_cyrillic_russian(env: Env) -> None:
    await connect(env)
    await say(env, "Привет! Встреча завтра в офисе")
    assert await find(env, "vstrecha zavtra") == ["Привет! Встреча завтра в офисе"]


async def test_russian_and_english_queries(env: Env) -> None:
    await connect(env)
    await say(env, "Документы отправлю в пятницу")
    await say(env, "The invoice is attached, please check")
    assert await find(env, "документы") == ["Документы отправлю в пятницу"]
    assert await find(env, "invoice") == ["The invoice is attached, please check"]


async def test_light_uzbek_stems_match_inflected_forms(env: Env) -> None:
    await connect(env)
    await say(env, "Yuragim siqilyapti")
    await say(env, "Kitoblarni olib keldim")
    assert await find(env, "yurak") == ["Yuragim siqilyapti"]
    assert await find(env, "kitob") == ["Kitoblarni olib keldim"]


async def test_a_typo_still_matches_through_trigrams(env: Env) -> None:
    await connect(env)
    await say(env, "Uchrashuv joyini o'zgartirdik")
    assert await find(env, "uchrashuvv") == ["Uchrashuv joyini o'zgartirdik"]


# ------------------------------------------------------------ ranking
async def test_ranking_prefers_all_words_then_newer(env: Env) -> None:
    await connect(env)
    await say(env, "Uchrashuv vaqti soat beshda", days_ago=10)
    await say(env, "Uchrashuv qoldirildi", days_ago=1)
    await say(env, "Uchrashuv vaqti soat beshda", days_ago=2)
    async with env.factory() as session:
        result = await search_messages(
            session, await account_id(env), ["uchrashuv vaqti"], now=env.clock.now()
        )
    texts = [hit.text for hit in result.hits]
    # Both words beat one word (which may still come in through trigrams);
    # among equal matches the newer message comes first.
    assert texts[:2] == ["Uchrashuv vaqti soat beshda", "Uchrashuv vaqti soat beshda"]
    assert result.hits[0].sent_at > result.hits[1].sent_at
    assert set(texts[2:]) <= {"Uchrashuv qoldirildi"}
    assert all(hit.score > later.score for hit in result.hits[:2] for later in result.hits[2:])
    assert result.relaxed is False


async def test_no_all_word_match_falls_back_to_any_word(env: Env) -> None:
    await connect(env)
    await say(env, "Kontrakt imzolandi")
    async with env.factory() as session:
        result = await search_messages(
            session, await account_id(env), ["kontrakt bekor qilindimi"], now=env.clock.now()
        )
    assert [hit.text for hit in result.hits] == ["Kontrakt imzolandi"]
    assert result.relaxed is True


async def test_variants_are_merged_by_best_score(env: Env) -> None:
    await connect(env)
    await say(env, "Квартира ижараси тўланди")
    async with env.factory() as session:
        result = await search_messages(
            session,
            await account_id(env),
            ["kvartira ijarasi", "квартира ижараси", "apartment rent"],
            now=env.clock.now(),
        )
    assert [hit.text for hit in result.hits] == ["Квартира ижараси тўланди"]
    assert result.searched == ("kvartira ijarasi", "apartment rent")


# ------------------------------------------------------------ filters, retention
async def test_contact_and_date_filters(env: Env) -> None:
    await connect(env)
    await say(env, "Hisobot tayyor", contact=CONTACT, days_ago=5)
    await say(env, "Hisobot tayyor emas", contact=OTHER_CONTACT, days_ago=1)
    async with env.factory() as session:
        contact_id = int(
            (
                await session.execute(
                    text("SELECT id FROM app.contacts WHERE telegram_user_id = :t"),
                    {"t": OTHER_CONTACT},
                )
            ).scalar_one()
        )
    only_other = await find(env, "hisobot", filters=SearchFilters(contact_ids=(contact_id,)))
    assert only_other == ["Hisobot tayyor emas"]
    base = env.clock.now()
    recent = await find(env, "hisobot", filters=SearchFilters(since=base - timedelta(days=3)))
    assert recent == ["Hisobot tayyor emas"]


async def test_deleted_and_purged_messages_are_not_found(env: Env) -> None:
    await connect(env)
    gone = await say(env, "Maxfiy parol yozuvi")
    await say(env, "Eskirgan parol yozuvi")
    await deliver(env, deleted_messages(message_ids=[gone]))
    async with env.factory() as session, session.begin():
        await session.execute(
            update(Message)
            .where(Message.text == "Eskirgan parol yozuvi")
            .values(text=None, search_key=None, purged_at=env.clock.now())
        )
    assert await find(env, "parol") == []


async def test_rg21_search_never_returns_another_accounts_messages(migrated_url: str) -> None:
    """RG-21 (§6 invariant 12): another owner's identical message never appears."""
    other = 7000000002
    async with environment(migrated_url, owner_ids=frozenset({OWNER, other})) as env:
        await connect(env)
        await say(env, "Umumiy kalit soʻz mine")
        await deliver(env, private_message(other, "/start"))
        await deliver(env, private_message(other, "yes"))
        await deliver(env, connection_update(user_id=other, connection_id="synthetic-conn-b"))
        await deliver(
            env,
            business_message(
                "synthetic-conn-b",
                from_id=7000000077,
                chat_id=7000000077,
                message_text="Umumiy kalit soʻz theirs",
            ),
        )
        assert await find(env, "kalit soz") == ["Umumiy kalit soʻz mine"]
        assert await find(env, "kalit soz", owner=other) == ["Umumiy kalit soʻz theirs"]
        # Even a filter naming the other account's contact returns nothing.
        async with env.factory() as session:
            theirs = int(
                (
                    await session.execute(
                        text("SELECT id FROM app.contacts WHERE telegram_user_id = 7000000077")
                    )
                ).scalar_one()
            )
        assert await find(env, "kalit", filters=SearchFilters(contact_ids=(theirs,))) == []


async def test_query_text_is_bound_never_interpolated(env: Env) -> None:
    await connect(env)
    await say(env, "Oddiy xabar")
    hostile = "'); DROP TABLE app.messages; -- & | ! :* <->"
    assert await find(env, hostile) == []
    async with env.factory() as session:
        assert (await session.execute(text("SELECT count(*) FROM app.messages"))).scalar_one() == 1


# ------------------------------------------------------------ reindex
def normalize_v1(value: str) -> str:
    """The M02 (v1) normalizer, as it was: NFKC, casefold, one apostrophe."""
    folded = unicodedata.normalize("NFKC", value).casefold()
    folded = folded.translate(str.maketrans(dict.fromkeys("ʻʼ‘’`´ʹʽ", "'")))
    return re.sub(r"\s+", " ", folded).strip()


async def make_v1(env: Env) -> None:
    """Turn every stored key into what M02 wrote, and record version 1."""
    async with env.factory() as session, session.begin():
        rows = (await session.execute(select(Message.id, Message.text))).all()
        for message_id, body in rows:
            if body is not None:
                await session.execute(
                    update(Message)
                    .where(Message.id == message_id)
                    .values(search_key=normalize_v1(body), lang_guess=None)
                )
        # The environment truncates system_state: write the row M02 seeded.
        await search_index.record_version(session, env.clock, 1)


async def stored_version(env: Env) -> int | None:
    async with env.factory() as session:
        return await search_index.stored_normalizer_version(session)


async def test_m02_keyed_messages_are_found_after_reindex(env: Env) -> None:
    await connect(env)
    await say(env, "Oʻzbekiston terma jamoasi")
    await say(env, "Ўзбекистон терма жамоаси")
    await make_v1(env)
    assert await stored_version(env) == 1
    # Before: the v1 key keeps the Cyrillic, so a Latin query cannot reach it.
    assert "Ўзбекистон терма жамоаси" not in await find(env, "ozbekiston terma")

    report = await search_index.reindex(env.factory, env.clock, batch_size=1)

    assert (report.scanned, report.updated, report.skipped_concurrent) == (2, 2, 0)
    assert report.previous_version == 1
    assert report.version == NORMALIZER_VERSION
    assert await stored_version(env) == NORMALIZER_VERSION
    assert sorted(await find(env, "ozbekiston terma")) == sorted(
        ["Oʻzbekiston terma jamoasi", "Ўзбекистон терма жамоаси"]
    )
    async with env.factory() as session:
        langs = (await session.execute(select(Message.lang_guess))).scalars().all()
    assert set(langs) == {"uz"}


async def test_reindex_is_idempotent(env: Env) -> None:
    await connect(env)
    await say(env, "Gʻalaba bayrami")
    await make_v1(env)
    first = await search_index.reindex(env.factory, env.clock)
    assert first.updated == 1

    again = await search_index.reindex(env.factory, env.clock)
    assert again.up_to_date is True
    forced = await search_index.reindex(env.factory, env.clock, force=True)
    assert (forced.scanned, forced.updated) == (1, 0)


async def test_a_concurrent_edit_wins_over_the_reindex(env: Env) -> None:
    await connect(env)
    message_id = await say(env, "Eski matn oʻzgaradi")
    await make_v1(env)
    async with env.factory() as session:
        batch = await search_index.read_batch(session, after_id=0, size=10)
    # The contact edits the message between the read and the write; the edit
    # stores its own (current) key and a new content hash.
    await deliver(
        env,
        business_message(message_id=message_id, message_text="Yangi matn", edit=True),
    )
    async with env.factory() as session, session.begin():
        updated, skipped = await search_index.apply_batch(session, batch)
    assert (updated, skipped) == (0, 1)
    async with env.factory() as session:
        key = (await session.execute(select(Message.search_key))).scalar_one()
    assert key == normalize("Yangi matn")


async def test_reindex_skips_deleted_and_purged_rows(env: Env) -> None:
    await connect(env)
    gone = await say(env, "Oʻchirilgan xabar")
    await say(env, "Qolgan xabar")
    await deliver(env, deleted_messages(message_ids=[gone]))
    await make_v1(env)
    report = await search_index.reindex(env.factory, env.clock)
    assert report.scanned == 1
