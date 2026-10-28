"""The outbound gateway and the control-chat renderer.

Named regression suite for §6 invariant 1 (nothing sends outside the gateway)
and the mis-routing failure the typed targets exist to prevent.
"""

from __future__ import annotations

import html

import pytest

from assistant.telegram.errors import ErrorClass
from assistant.telegram.gateway import (
    BusinessChat,
    BusinessSendNotAvailableError,
    ControlChat,
    OutboundGateway,
)
from assistant.telegram.render import (
    MAX_MESSAGE_CHARS,
    escape,
    render_control_message,
    sanitize,
    split_message,
)
from tests.fakes.clock import FakeClock, FakeSleep
from tests.fakes.sender import FakeTelegramSender, network_error, telegram_error

CONTROL = ControlChat(account_id=1, chat_id=584113992)


@pytest.fixture
def sender() -> FakeTelegramSender:
    return FakeTelegramSender()


@pytest.fixture
def gateway(sender: FakeTelegramSender) -> OutboundGateway:
    return OutboundGateway(sender, FakeClock(), sleep=FakeSleep())


async def test_sends_to_one_chat_are_spaced_by_the_injected_sleep(
    sender: FakeTelegramSender,
) -> None:
    """About one message per second per chat (TF §5), waited through the
    injected sleep: the first send never waits, and another chat's send waits
    only the global spacing of any two sends (< 25 msg/s, S04.4)."""
    clock = FakeClock()
    sleep = FakeSleep()
    gateway = OutboundGateway(sender, clock, sleep=sleep)
    other = ControlChat(account_id=2, chat_id=584113993)

    await gateway.send_text(CONTROL, "first")
    await gateway.send_text(other, "another chat")
    assert sleep.waits == [pytest.approx(0.04)]

    await gateway.send_text(CONTROL, "second")
    assert sleep.waits == [pytest.approx(0.04), 1.0]

    clock.advance(seconds=5)
    await gateway.send_text(CONTROL, "later")
    assert sleep.waits == [pytest.approx(0.04), 1.0]


# ------------------------------------------------------------------ renderer
def test_untrusted_text_cannot_inject_markup() -> None:
    """A contact's message containing tags must not become markup."""
    assert escape("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_allowed_tags_survive_sanitising() -> None:
    assert sanitize("<b>bold</b> and <code>code</code>") == "<b>bold</b> and <code>code</code>"


def test_unsupported_tags_are_escaped_not_stripped() -> None:
    """Escaping keeps what the text said; stripping would silently change it."""
    assert sanitize("<script>x</script>") == "&lt;script&gt;x&lt;/script&gt;"
    assert sanitize("<div>x</div>") == "&lt;div&gt;x&lt;/div&gt;"


def test_ampersands_in_plain_text_are_escaped() -> None:
    """An unescaped & makes Telegram reject the whole message with a 400."""
    assert sanitize("Tom & Jerry") == "Tom &amp; Jerry"


@pytest.mark.parametrize(
    "literal",
    [
        "Tom & Jerry < 3 > 2",
        "&amp; &lt; &quot; &#60; &#x3c; &madeup;",
        '<b>contact</b> <a href="https://synthetic.example">link</a>',
        "<script>synthetic</script>",
    ],
)
def test_preescaped_untrusted_content_is_literal_after_one_escape(literal: str) -> None:
    encoded = escape(literal)
    assert sanitize(encoded) == encoded
    assert html.unescape(sanitize(encoded)) == literal
    assert render_control_message(f"<b>Name</b>: {encoded}") == [f"<b>Name</b>: {encoded}"]


def test_escaped_contact_tags_are_never_restored_as_application_markup() -> None:
    encoded = '&lt;b&gt;contact&lt;/b&gt; &lt;a href="https://synthetic.example"&gt;link&lt;/a&gt;'
    assert sanitize(encoded) == encoded
    assert "<a" not in sanitize(encoded)
    assert "<b>" not in sanitize(encoded)


def test_application_link_preserves_escaped_url_and_label() -> None:
    text = '<a href="https://synthetic.example/?a=1&amp;b=2">Tom &amp; Jerry</a>'
    assert sanitize(text) == text


@pytest.mark.parametrize(
    "tag",
    [
        '<a href="javascript:synthetic">',
        '<a href="https://synthetic.example" onclick="synthetic">',
        '<b class="synthetic">',
        '<script src="https://synthetic.example">',
    ],
)
def test_unsupported_tag_attributes_and_links_stay_literal(tag: str) -> None:
    assert sanitize(tag) == escape(tag)


@pytest.mark.parametrize(
    "pair",
    [
        '<a href="javascript:synthetic">label</a>',
        '<a href="https://synthetic.example" onclick="synthetic">label</a>',
        '<b class="synthetic">label</b>',
        '<b>label</b class="synthetic">',
        '<a href="https://synthetic.example">label</a href="synthetic">',
        '<b class="synthetic">label</b class="synthetic">',
        "<a>label</a>",
    ],
)
def test_rejected_pairs_escape_both_sides_even_within_valid_markup(pair: str) -> None:
    assert sanitize(pair) == escape(pair)
    assert sanitize(f"<i>before {pair} after</i>") == f"<i>before {escape(pair)} after</i>"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '<b>outer <b class="synthetic">inner</b> end</b>',
            '<b>outer &lt;b class="synthetic"&gt;inner&lt;/b&gt; end</b>',
        ),
        (
            '<b class="synthetic">outer <i>inner</i> end</b>',
            '&lt;b class="synthetic"&gt;outer <i>inner</i> end&lt;/b&gt;',
        ),
        ("<b><i>crossed</b></i>", "<b>&lt;i&gt;crossed</b>&lt;/i&gt;"),
        ("<b>unclosed", "&lt;b&gt;unclosed"),
        ("orphan</b>", "orphan&lt;/b&gt;"),
        ("<b><i>valid</i></b>", "<b><i>valid</i></b>"),
    ],
)
def test_nested_and_malformed_pairs_preserve_only_balanced_supported_tags(
    text: str, expected: str
) -> None:
    assert sanitize(text) == expected


def test_short_messages_are_not_split() -> None:
    assert split_message("hello") == ["hello"]


def test_long_messages_split_on_block_boundaries() -> None:
    text = "\n\n".join(["paragraph " + "x" * 500 for _ in range(20)])
    parts = split_message(text)
    assert len(parts) > 1
    assert all(len(part) <= MAX_MESSAGE_CHARS for part in parts)
    # Nothing is lost, and nothing is cut mid-word.
    assert "".join(part.replace("\n", "") for part in parts).count("paragraph") == 20


def test_an_unbroken_run_is_cut_rather_than_dropped() -> None:
    """A pasted token with no spaces still has to go somewhere."""
    parts = split_message("y" * 9000)
    assert len(parts) >= 3
    assert sum(len(part) for part in parts) == 9000


def test_empty_text_produces_no_messages() -> None:
    assert render_control_message("") == []


# ------------------------------------------------------------------- gateway
async def test_control_sends_never_carry_a_business_connection_id(
    gateway: OutboundGateway, sender: FakeTelegramSender
) -> None:
    """The mis-routing guard. A connection id here would send AS THE OWNER."""
    await gateway.send_text(CONTROL, "hello")
    assert sender.sends[0]["business_connection_id"] is None


async def test_business_sends_are_refused_until_policy_exists(
    gateway: OutboundGateway, sender: FakeTelegramSender
) -> None:
    """§6 invariant 1: nothing sends on the owner's behalf without policy."""
    with pytest.raises(BusinessSendNotAvailableError):
        await gateway.send_text(BusinessChat(account_id=1, conversation_id=7), "hi")
    assert sender.sends == []


async def test_a_long_message_is_sent_as_several(
    gateway: OutboundGateway, sender: FakeTelegramSender
) -> None:
    result = await gateway.send_text(CONTROL, "word " * 2000)
    assert len(sender.sends) > 1
    assert result.ok
    assert result.message_id == 1000 + len(sender.sends)
    assert result.error_code is None
    assert not result.delivery_uncertain


@pytest.mark.parametrize("delivered", [1, 2])
@pytest.mark.parametrize(
    "error",
    [
        telegram_error("Too Many Requests", status_code=429, retry_after=7),
        telegram_error("Bad Request: synthetic refusal", status_code=400),
        telegram_error("Forbidden: bot was blocked by the user", status_code=403),
        network_error("synthetic reset"),
    ],
)
async def test_partial_control_delivery_is_uncertain_and_never_retried(
    gateway: OutboundGateway, sender: FakeTelegramSender, error: Exception, delivered: int
) -> None:
    async def fail_after_delivered_chunks() -> None:
        if len(sender.sends) == delivered:
            sender.fail_with = error

    sender.on_call = fail_after_delivered_chunks
    result = await gateway.send_text(CONTROL, "word " * 4000, keyboard={"synthetic": True})
    assert result.error_class is ErrorClass.AMBIGUOUS
    assert (result.ok, result.delivery_uncertain) == (False, True)
    assert result.error_code == "partial_delivery"
    assert result.message_id is None  # no identity for the complete logical message/card
    assert result.retry_after is None  # never invites replay of earlier chunks
    assert len(sender.sends) == delivered + 1
    assert all(call["reply_markup"] is None for call in sender.sends)


@pytest.mark.parametrize(
    ("error", "expected", "uncertain", "retry_after"),
    [
        (
            telegram_error("Too Many Requests", status_code=429, retry_after=7),
            ErrorClass.RATE_LIMITED,
            False,
            7,
        ),
        (
            telegram_error("Bad Request: synthetic refusal", status_code=400),
            ErrorClass.BAD_REQUEST,
            False,
            None,
        ),
        (network_error("synthetic reset"), ErrorClass.TRANSIENT, True, None),
    ],
)
async def test_first_chunk_failure_keeps_single_call_classification(
    gateway: OutboundGateway,
    sender: FakeTelegramSender,
    error: Exception,
    expected: ErrorClass,
    uncertain: bool,
    retry_after: int | None,
) -> None:
    sender.fail_with = error
    result = await gateway.send_text(CONTROL, "word " * 2000)
    assert result.error_class is expected
    assert result.delivery_uncertain is uncertain
    assert result.retry_after == retry_after
    assert len(sender.sends) == 1


async def test_the_keyboard_goes_on_the_last_chunk_only(
    gateway: OutboundGateway, sender: FakeTelegramSender
) -> None:
    """Buttons belong where the reader ends up, not mid-thread."""
    keyboard = {"inline_keyboard": []}
    await gateway.send_text(CONTROL, "word " * 2000, keyboard=keyboard)
    assert sender.sends[0]["reply_markup"] is None
    assert sender.sends[-1]["reply_markup"] is keyboard


async def test_an_unreachable_owner_is_not_reported_as_a_block(
    gateway: OutboundGateway, sender: FakeTelegramSender
) -> None:
    """TF §12 LV-09: `chat not found` means "press Start", not "blocked"."""
    sender.fail_with = telegram_error(
        "Telegram server says - Bad Request: chat not found", status_code=400
    )
    result = await gateway.send_text(CONTROL, "hello")
    assert result.error_class is ErrorClass.CHAT_UNREACHABLE
    assert result.error_class is not ErrorClass.FORBIDDEN
    assert not result.ok


async def test_rate_limiting_is_reported_with_retry_after(
    gateway: OutboundGateway, sender: FakeTelegramSender
) -> None:
    sender.fail_with = telegram_error(
        "Too Many Requests: retry after 7", status_code=429, retry_after=7
    )
    result = await gateway.send_text(CONTROL, "hello")
    assert result.error_class is ErrorClass.RATE_LIMITED
    assert result.retry_after == 7


async def test_a_blocked_owner_is_forbidden(
    gateway: OutboundGateway, sender: FakeTelegramSender
) -> None:
    sender.fail_with = telegram_error("Forbidden: bot was blocked by the user", status_code=403)
    assert (await gateway.send_text(CONTROL, "x")).error_class is ErrorClass.FORBIDDEN


async def test_a_timeout_is_ambiguous_and_never_claimed_as_sent(
    gateway: OutboundGateway, sender: FakeTelegramSender
) -> None:
    """A send with no answer may or may not have arrived (§6 invariant 6)."""
    sender.fail_with = TimeoutError("no response")
    result = await gateway.send_text(CONTROL, "hello")
    assert result.ok is False
    assert result.error_class in {ErrorClass.TRANSIENT, ErrorClass.AMBIGUOUS}
    assert result.delivery_uncertain


@pytest.mark.parametrize(
    "error",
    [
        network_error("Request timeout error"),
        network_error("ClientConnectorError: synthetic reset"),
        telegram_error("Bad Gateway", status_code=502),
    ],
)
async def test_a_send_telegram_never_answered_is_delivery_uncertain(
    gateway: OutboundGateway, sender: FakeTelegramSender, error: Exception
) -> None:
    """§6 invariant 6, with the exceptions aiogram really raises: the message
    may be on screen, so the result must say so (and never be retried)."""
    sender.fail_with = error
    result = await gateway.send_text(CONTROL, "hello")
    assert (result.ok, result.delivery_uncertain) == (False, True)


async def test_a_refused_send_is_a_definite_failure(
    gateway: OutboundGateway, sender: FakeTelegramSender
) -> None:
    """Verification review, 2026-10-23: a real TelegramBadRequest has no
    status attribute, and this used to come back TRANSIENT (uncertain)."""
    sender.fail_with = telegram_error(
        "Bad Request: can't parse entities: synthetic", status_code=400
    )
    result = await gateway.send_text(CONTROL, "hello")
    assert result.error_class is ErrorClass.BAD_REQUEST
    assert (result.ok, result.delivery_uncertain) == (False, False)


async def test_a_refused_edit_is_a_definite_failure(
    gateway: OutboundGateway, sender: FakeTelegramSender
) -> None:
    sender.fail_with = telegram_error("Bad Request: message to edit not found", status_code=400)
    result = await gateway.edit_control_text(CONTROL, 7, "hello")
    assert result.error_class is ErrorClass.BAD_REQUEST
    assert not result.delivery_uncertain


async def test_chat_action_failure_never_breaks_a_turn(
    gateway: OutboundGateway, sender: FakeTelegramSender
) -> None:
    """A typing indicator is cosmetic; losing it must not lose the reply."""
    sender.fail_with = telegram_error("boom", status_code=500)
    await gateway.chat_action(CONTROL)  # must not raise


async def test_callbacks_are_always_answered(
    gateway: OutboundGateway, sender: FakeTelegramSender
) -> None:
    """An unanswered callback leaves a spinner on the owner's button."""
    await gateway.answer_callback("cb-1", "done")
    assert sender.answered_callbacks[0]["id"] == "cb-1"
