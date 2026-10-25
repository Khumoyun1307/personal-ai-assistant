"""S03.4: the mechanical check of a final answer "with reference" (threshold 80 %)."""

from __future__ import annotations

from evals.retrieval import (
    MIN_QUOTE_CHARS,
    Answer,
    HitView,
    Reference,
    _search_queries,
    check_answer,
)
from evals.schema import RetrievalQuery

from assistant.memory.retrieval import MAX_QUERIES

QUERY = RetrievalQuery(
    id="r90",
    text="the address Kamola sent",
    lang="en",
    script="latin",
    intents=["paraphrase"],
    target_message_ids=["c09.m05"],
    conversation_ids=["c09"],
    expected_answer="Kamola's address.",
    answer_values=["Qorasuv-3", "17-uy"],
)
TEXT = "Manzil: Mirzo Ulugbek tumani, Qorasuv-3, 17-uy, 45-xonadon"
HITS = [
    HitView("h1", "c09.m05", "c09", "Kamola", "2026-10-20", TEXT),
    HitView("h2", "c09.m02", "c09", "Kamola", "2026-10-19", "Manzilni ertaga yuboraman"),
]


def good() -> Answer:
    return Answer(
        found=True,
        answer="Kamola sent: Qorasuv-3, 17-uy.",
        references=[
            Reference(ref="h1", contact="Kamola", day="2026-10-20", quote="Qorasuv-3, 17-uy")
        ],
    )


def verdict(answer: Answer) -> tuple[bool, tuple[str, ...]]:
    result = check_answer(answer, QUERY, HITS)
    return result.correct, result.problems


def test_a_correct_answer_with_a_valid_reference_passes() -> None:
    assert verdict(good()) == (True, ())


def test_the_values_may_come_from_the_quote_and_script_or_apostrophes_do_not_matter() -> None:
    answer = good().model_copy(update={"answer": "The address is in the message."})
    assert verdict(answer)[0]
    cyrillic = good().model_copy(update={"answer": "Қорасув-3, 17-уй"})
    assert check_answer(
        cyrillic, QUERY.model_copy(update={"answer_values": ["Qorasuv-3"]}), HITS
    ).correct


def test_found_false_is_never_correct() -> None:
    answer = Answer(found=False, answer="Nothing found.", references=[])
    assert verdict(answer) == (False, ("not_found",))


def test_an_answer_without_a_reference_fails() -> None:
    assert verdict(good().model_copy(update={"references": []})) == (False, ("no_reference",))


def mutate(**changes: str) -> Answer:
    reference = good().references[0].model_copy(update=changes)
    return good().model_copy(update={"references": [reference]})


def test_a_wrong_day_a_wrong_contact_and_an_invented_quote_each_fail() -> None:
    assert verdict(mutate(day="2026-10-19")) == (False, ("wrong_date",))
    assert verdict(mutate(contact="Nilufar")) == (False, ("wrong_contact",))
    assert verdict(mutate(quote="Manzil: Chilonzor 9")) == (False, ("quote_not_in_message",))


def test_a_name_with_an_honorific_still_matches() -> None:
    assert verdict(mutate(contact="Kamola opa"))[0]


def test_a_name_must_match_whole_words_not_fragments() -> None:
    assert verdict(mutate(contact="Kam")) == (False, ("wrong_contact",))
    assert verdict(mutate(contact="Kamolaxon")) == (False, ("wrong_contact",))
    assert verdict(mutate(contact="a")) == (False, ("wrong_contact",))


def test_a_quote_must_be_a_real_excerpt_not_a_fragment() -> None:
    """A bare amount or one short word occurs in many messages and proves nothing."""
    assert MIN_QUOTE_CHARS == 8
    assert verdict(mutate(quote="17-uy")) == (False, ("quote_too_short",))
    assert verdict(mutate(quote="Qorasuv-3"))[0]  # 9 characters of words: enough


def test_a_short_message_may_be_quoted_in_full() -> None:
    hit = HitView("h1", "c09.m05", "c09", "Kamola", "2026-10-20", "17-uy, 45")
    answer = Answer(
        found=True,
        answer="Qorasuv-3, 17-uy",
        references=[Reference(ref="h1", contact="Kamola", day="2026-10-20", quote="17-uy, 45")],
    )
    assert check_answer(answer, QUERY, [hit]).correct
    fragment = answer.model_copy(
        update={"references": [answer.references[0].model_copy(update={"quote": "17-uy"})]}
    )
    assert check_answer(fragment, QUERY, [hit]).problems == ("quote_too_short",)


def test_citing_a_non_target_message_fails_even_if_the_values_appear() -> None:
    other = [HitView("h1", "c09.m02", "c09", "Kamola", "2026-10-19", "Qorasuv-3, 17-uy")]
    answer = good().model_copy(
        update={
            "references": [
                Reference(ref="h1", contact="Kamola", day="2026-10-19", quote="Qorasuv-3, 17-uy")
            ]
        }
    )
    result = check_answer(answer, QUERY, other)
    assert not result.correct
    assert "no_target_cited" in result.problems


def test_an_unknown_ref_is_a_problem() -> None:
    assert not verdict(mutate(ref="h9"))[0]
    assert "unknown_ref" in verdict(mutate(ref="h9"))[1]


def test_every_value_must_be_carried_and_translations_do_not_count() -> None:
    answer = good().model_copy(
        update={
            "answer": "Building seventeen.",
            "references": [
                Reference(ref="h1", contact="Kamola", day="2026-10-20", quote="Qorasuv-3")
            ],
        }
    )
    assert verdict(answer) == (False, ("missing_value",))


def test_one_wrong_extra_reference_makes_the_answer_wrong() -> None:
    extra = Reference(
        ref="h2", contact="Kamola", day="2026-10-20", quote="Manzilni ertaga yuboraman"
    )
    answer = good().model_copy(update={"references": [*good().references, extra]})
    assert verdict(answer) == (False, ("wrong_date",))


def test_html_escaped_quotes_are_accepted() -> None:
    hit = HitView("h1", "c09.m05", "c09", "Kamola", "2026-10-20", "Qorasuv-3 & 17-uy")
    answer = Answer(
        found=True,
        answer="Qorasuv-3, 17-uy",
        references=[
            Reference(ref="h1", contact="Kamola", day="2026-10-20", quote="Qorasuv-3 &amp; 17-uy")
        ],
    )
    assert check_answer(answer, QUERY, [hit]).correct


# ----------------------------------------------------------- search queries
def test_the_owners_words_come_first_then_unique_variants() -> None:
    queries = _search_queries("  adres Kamola ", ["adres Kamola", "", "манзил Камола", "address"])
    assert queries == ["adres Kamola", "манзил Камола", "address"]


def test_variants_are_capped_like_the_search_tool() -> None:
    variants = [f"variant {n}" for n in range(20)]
    queries = _search_queries("q", variants)
    assert len(queries) == MAX_QUERIES
    assert queries[0] == "q"
    assert all(len(q) <= 200 for q in _search_queries("q", ["x" * 500]))
