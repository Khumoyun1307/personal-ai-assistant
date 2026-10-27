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


def test_a_value_the_message_itself_inflects_is_carried_by_an_exact_quote() -> None:
    """E-u, the defect: the message says "500 mingni", the value is "500 ming". An
    answer that quotes the message word for word failed `missing_value`."""
    text = "Otgan oy olgan 500 mingni qachon qaytarasan?"
    query = QUERY.model_copy(
        update={"target_message_ids": ["c10.m04"], "answer_values": ["500 ming"]}
    )
    hits = [HitView("h1", "c10.m04", "c10", "Sherzod", "2026-10-15", text)]
    answer = Answer(
        found=True,
        answer="Sherzod asked when you will return it.",
        references=[Reference(ref="h1", contact="Sherzod", day="2026-10-15", quote=text)],
    )
    assert check_answer(answer, query, hits).correct
    # Digits never extend a number, and a translation still does not count.
    for wrong in ("5000 mingni", "half a million"):
        changed = answer.model_copy(
            update={
                "answer": wrong,
                "references": [
                    Reference(ref="h1", contact="Sherzod", day="2026-10-15", quote="qachon")
                ],
            }
        )
        assert "missing_value" in check_answer(changed, query, hits).problems


def test_the_free_text_answer_must_carry_a_value_as_whole_words() -> None:
    """S03.6 review: the inflection allowance is for exact quotes of the message
    only. The model's own words must carry the value whole: "Alisher" is not
    "Ali", and an inflected value in the answer text alone does not count."""
    text = "Ali ertaga 500 ming olib keladi"
    query = QUERY.model_copy(
        update={"target_message_ids": ["c10.m04"], "answer_values": ["Ali", "500 ming"]}
    )
    hits = [HitView("h1", "c10.m04", "c10", "Sherzod", "2026-10-15", text)]
    quote = Reference(ref="h1", contact="Sherzod", day="2026-10-15", quote="ertaga 500 ming olib")
    answer = Answer(found=True, answer="Alisher brings it tomorrow.", references=[quote])
    assert check_answer(answer, query, hits).problems == ("missing_value",)
    inflected = answer.model_copy(update={"answer": "Aliga 500 mingni beradi."})
    assert check_answer(inflected, query, hits).problems == ("missing_value",)
    whole = answer.model_copy(update={"answer": "Ali brings 500 ming tomorrow."})
    assert check_answer(whole, query, hits).correct


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


# ------------------------------------------------------------ agent mode (E-aa)
def agent_verdict(model_text: str, **overrides: object) -> tuple[bool, tuple[str, ...]]:
    from evals.agent_mode import check_agent_answer

    values: dict[str, object] = {
        "succeeded": True,
        "model_text": model_text,
        "cited": ("c09.m05",),
        "unverified": 0,
        "query": QUERY,
        "cited_texts": {"c09.m05": TEXT},
    }
    values.update(overrides)
    result = check_agent_answer(**values)  # type: ignore[arg-type]
    return result.correct, result.problems


def test_an_agent_answer_needs_a_succeeded_run_a_verified_target_citation_and_the_values() -> None:
    assert agent_verdict("Qorasuv-3, 17-uy [msg:41]") == (True, ())
    assert agent_verdict("Qorasuv-3, 17-uy", succeeded=False)[1][0] == "run_not_succeeded"
    assert "no_citation" in agent_verdict("Qorasuv-3, 17-uy", cited=())[1]
    assert "no_target_cited" in agent_verdict("Qorasuv-3, 17-uy", cited=("c09.m02",))[1]
    assert "unverified_citation" in agent_verdict("Qorasuv-3, 17-uy", unverified=1)[1]
    assert "missing_value" in agent_verdict("Qorasuv-3 [msg:41]")[1]


def test_an_agent_answer_value_counts_inflected_only_where_a_cited_target_has_that_form() -> None:
    """E-aa: values as whole words in the model's own words; an inflected form
    (E-s) only when a cited target message has that very form."""
    inflected = {"c09.m05": "Manzil: Qorasuv-3dagi 17-uy, topasiz"}
    assert agent_verdict("Qorasuv-3dagi 17-uy", cited_texts=inflected) == (True, ())
    # The same inflection without a cited message that says it: a miss.
    assert "missing_value" in agent_verdict("Qorasuv-3dagi 17-uy")[1]
    # The markers are not the model's words: an id never carries a value.
    numeric = QUERY.model_copy(update={"answer_values": ["41"]})
    assert "missing_value" in agent_verdict("See [msg:41]", query=numeric)[1]


# ------------------------------------------------- agent mode: values as values
def test_value_key_reads_amounts_and_times_as_values() -> None:
    """S03.7 fix round (r19, r24, r26, r31): the model reformats amounts and
    times; the value is the same, so the check compares values."""
    from evals.agent_mode import value_key

    same = [
        ("03:15", ["3:15", "3:15 a.m.", "3:15 am", "03.15"]),
        ("16:30", ["4:30 p.m.", "16.30", "16:30"]),
        ("00:30", ["12:30 a.m."]),
        ("12:30", ["12:30 p.m."]),
        (
            "1 million 500 ming",
            ["1 500 000", "1,5 mln", "1.5 million", "1 500 000", "1 mln 500 ming", "1500000"],
        ),
        ("3 million 600 ming", ["3 600 000", "3 mln 600 ming", "3,6 млн", "3.600.000"]),
        ("3 миллион 380 минг", ["3 mln 380 ming", "3 380 000", "3 million 380 ming"]),
        ("3 mln", ["3 миллион", "3 000 000", "3 млн"]),
        ("300 ming", ["300 000", "300 тысяч", "300 минг", "300k", "300 thousand"]),
    ]
    for value, forms in same:
        for form in forms:
            assert value_key(form) == value_key(value), (value, form)


def test_value_key_never_makes_different_values_equal() -> None:
    from evals.agent_mode import value_key

    different = [
        ("03:15", "15:15"),
        ("03:15", "3:51"),
        ("16:30", "6:30"),
        ("1 million 500 ming", "1 million 50 ming"),
        ("3 mln", "3 ming"),
        ("300 ming", "300"),
        ("80", "800"),
        ("50%", "5%"),
    ]
    for left, right in different:
        assert value_key(left) != value_key(right), (left, right)
    # Words, addresses and numbers without a unit are untouched.
    assert value_key("Qorasuv-3, 17-uy") == "qorasuv 3 17 uy"
    assert value_key("3 kun") == "3 kun"
    assert value_key("Chilonzor 9 mavze 15 uy") == "chilonzor 9 mavze 15 uy"


def test_an_agent_answer_value_is_compared_as_a_value() -> None:
    """r24, r31 as recorded: the reply reformats the amount; r19: the time."""
    amount = QUERY.model_copy(update={"answer_values": ["1 million 500 ming"]})
    assert agent_verdict("Dacha 1 500 000 сўм [msg:41]", query=amount) == (True, ())
    assert agent_verdict("Dacha 1,5 mln so'm [msg:41]", query=amount) == (True, ())
    assert "missing_value" in agent_verdict("Dacha 1 050 000 [msg:41]", query=amount)[1]
    total = QUERY.model_copy(update={"answer_values": ["3 миллион 380 минг"]})
    assert agent_verdict("Jami 3 mln 380 ming [msg:41]", query=total) == (True, ())
    time = QUERY.model_copy(update={"answer_values": ["03:15"]})
    assert agent_verdict("He lands at 3:15 a.m. [msg:41]", query=time) == (True, ())
    assert "missing_value" in agent_verdict("He lands at 3:15 p.m. [msg:41]", query=time)[1]


def test_an_inflected_amount_still_needs_a_cited_target_with_that_form() -> None:
    """E-s is not loosened: an Uzbek ending on an amount counts only where a
    cited target message has that very form."""
    amount = QUERY.model_copy(update={"answer_values": ["300 ming"]})
    assert "missing_value" in agent_verdict("300 mingga [msg:41]", query=amount)[1]
    inflected = {"c09.m05": "Sovg'a 300 mingga tushdi"}
    assert agent_verdict("300 mingga [msg:41]", query=amount, cited_texts=inflected) == (True, ())
