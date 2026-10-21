"""`/start bizChat<id>` parsing (TF §12 LV-10; S02.3)."""

from __future__ import annotations

import pytest

from assistant.telegram.control.deeplinks import is_deep_link, parse_biz_chat


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("bizChat8139387524", 8139387524),
        ("bizchat42", 42),
        ("  bizChat7  ", 7),
        (str(2**63 - 1).join(["bizChat", ""]), 2**63 - 1),
    ],
)
def test_well_formed_parameters_parse(argument: str, expected: int) -> None:
    assert parse_biz_chat(argument) == expected


@pytest.mark.parametrize(
    "argument",
    [
        "",
        "bizChat",
        "bizChat0",
        "bizChat-1",
        "bizChatabc",
        "bizChat12a",
        "bizChat１２３",  # full-width digits: str.isdigit would accept these  # noqa: RUF001
        "bizChat" + str(2**63),  # overflow
        "bizChat 5",
        "start",
    ],
)
def test_malformed_bizchat_parameters_are_refused(argument: str) -> None:
    assert parse_biz_chat(argument) is None


def test_deep_link_detection_is_case_insensitive_and_narrow() -> None:
    assert is_deep_link("bizChat1")
    assert is_deep_link("BIZCHATx")  # detected, then refused by the parser
    assert not is_deep_link("hello")
    assert not is_deep_link("")
