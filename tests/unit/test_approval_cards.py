"""Approval-card rendering and keyboards (S04.3 §3.1; FR-ACT-02, FR-DRF-03/06).

Pure: views are built by hand with synthetic text; nothing touches the
database or Telegram. What reaches Telegram is checked after the gateway's
own sanitizer (`render_control_message`), as the owner would see it.
"""

from __future__ import annotations

import html
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from assistant.accounts.callbacks import MAX_CALLBACK_DATA_BYTES, new_token, parse
from assistant.actions.states import ActionOrigin, ActionState, StateReason
from assistant.telegram.control.approvals import (
    CARD_BUDGET,
    COPY_TEXT_MAX,
    PREVIEW_CHARS,
    ApprovalCardView,
    card_keyboard,
    render_card,
)
from assistant.telegram.render import render_control_message

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
TEXT = "Synthetic reply: see you at 18:00."

#: The reason each terminal-ish state is shown with in these tests.
REASONS: dict[ActionState, StateReason | None] = {
    ActionState.FAILED: StateReason.NOT_DELIVERED,
    ActionState.OUTDATED: StateReason.NEW_CONTACT_MESSAGE,
    ActionState.EXPIRED: StateReason.WINDOW_CLOSED,
    ActionState.CANCELLED: StateReason.IGNORE_MODE,
    ActionState.BLOCKED: StateReason.POLICY_DENIED,
    ActionState.REJECTED: StateReason.OWNER_REJECTED,
    ActionState.DELIVERY_UNCERTAIN: StateReason.AMBIGUOUS,
    ActionState.SUCCEEDED: StateReason.SENT,
}


def view(**changes: Any) -> ApprovalCardView:
    base = ApprovalCardView(
        action_id=41,
        account_id=7,
        conversation_id=9,
        state=ActionState.AWAITING_APPROVAL,
        reason=None,
        origin=ActionOrigin.AGENT_PROPOSAL,
        proactive=False,
        version=1,
        text=TEXT,
        draft_flags=None,
        expires_at=NOW + timedelta(hours=1),
        executed_at=None,
        error_class=None,
        approval_token="AbCdEf1234",
        revision_turn_id=None,
        card_message_id=None,
        card_state=None,
        contact_name="Synthetic Contact",
        excerpts=("Are you free tonight?",),
        window_end=NOW + timedelta(hours=20),
        timezone="UTC",
        control_chat_id=7000000001,
        sending_enabled=True,
        card_token="CardTok123",
    )
    return replace(base, **changes)


def in_state(state: ActionState, **changes: Any) -> ApprovalCardView:
    live = state is ActionState.AWAITING_APPROVAL
    defaults: dict[str, Any] = {
        "state": state,
        "reason": REASONS.get(state),
        "approval_token": "AbCdEf1234" if live else None,
        "executed_at": NOW if state is ActionState.SUCCEEDED else None,
    }
    return view(**{**defaults, **changes})


def shown(card_text: str) -> str:
    """The card as Telegram receives it (after the gateway's sanitizer)."""
    [part] = render_control_message(card_text)
    return part


def buttons(markup: Any) -> list[dict[str, Any]]:
    if markup is None:
        return []
    dumped = markup.model_dump(exclude_none=True)
    return [button for row in dumped["inline_keyboard"] for button in row]


def ops(markup: Any) -> list[str]:
    return [
        parse(button["callback_data"]).op for button in buttons(markup) if "callback_data" in button
    ]


def copies(markup: Any) -> list[str]:
    return [button["copy_text"]["text"] for button in buttons(markup) if "copy_text" in button]


_SENT_WORD = re.compile(r"(?i)(?<!not )\bsent\b")


@pytest.mark.parametrize("state", list(ActionState))
def test_rg20_card_labels_never_show_pending_blocked_or_uncertain_work_as_sent(
    state: ActionState,
) -> None:
    text = shown(render_card(in_state(state)).text)
    if state is ActionState.SUCCEEDED:
        assert "Sent ✓ 12:00" in text
        return
    assert "Sent ✓" not in text
    assert _SENT_WORD.search(text) is None, text
    expected = {
        ActionState.AWAITING_APPROVAL: "Draft — awaiting your approval",
        ActionState.REVISING: "Editing",
        ActionState.APPROVED: "Sending…",
        ActionState.EXECUTING: "Sending…",
        ActionState.FAILED: "Not sent — Telegram did not send it",
        ActionState.DELIVERY_UNCERTAIN: "Delivery uncertain — please check the chat",
        ActionState.OUTDATED: "Outdated — a newer message arrived",
        ActionState.EXPIRED: "Expired — the 24-hour reply window closed",
        ActionState.REJECTED: "Rejected",
        ActionState.CANCELLED: "Cancelled — the chat is set to Ignore",
        ActionState.BLOCKED: "Not sendable — a required check did not pass",
    }.get(state)
    if expected is not None:
        assert expected in text.replace("<b>", "").replace("</b>", "")
    # Every card says what it is: never confusable with a sent message.
    assert "📝" in text


def test_a_revising_card_says_revising_once_the_owner_answered() -> None:
    asked = shown(render_card(in_state(ActionState.REVISING)).text)
    assert "Send the new text, or tell me what to change" in asked
    answered = shown(render_card(in_state(ActionState.REVISING, revision_turn_id=5)).text)
    assert "Revising…" in answered


def test_a_blocked_sending_disabled_card_says_not_sendable_and_offers_copy() -> None:
    card = render_card(in_state(ActionState.BLOCKED, reason=StateReason.SENDING_DISABLED))
    text = shown(card.text)
    assert "Not sendable</b> — sending is turned off" in text
    assert "Sending is off; you can copy the text to send it yourself." in text
    assert copies(card.keyboard) == [TEXT]
    assert ops(card.keyboard) == []


def test_card_content_is_escaped_and_bidi_made_visible() -> None:
    hostile = 'Tom & Jerry <b>bold</b> <a href="https://x.example">link</a> <script>‮abc'
    card = render_card(
        view(
            text=hostile,
            contact_name="Eve​ <i>x</i>",
            excerpts=("<code>q</code> & ⁦z",),
        )
    )
    text = shown(card.text)
    # Content never becomes markup: the tags it carries arrive escaped.
    assert "<b>bold" not in text
    assert "<a href" not in text
    assert "<i>x" not in text
    assert "<code>q" not in text
    assert "\\u003cb&gt;bold\\u003c/b&gt;" in text
    assert "&lt;script&gt;" in text
    # One escape only: the owner sees "&", exactly what will be sent.
    assert "Tom &amp; Jerry" in text
    assert "&amp;amp;" not in text
    # Invisible and bidi characters are visible, never applied.
    for hidden in ("‮", "​", "⁦"):
        assert hidden not in text
    assert "\\u202e" in text
    assert "\\u200b" in text
    assert "\\u2066" in text
    # The card's own markup still works, and the copy button has the exact text.
    assert text.startswith("📝 <b>Draft reply</b>")
    assert "<blockquote>" in text


def test_card_preserves_entity_looking_literals_in_every_untrusted_field() -> None:
    literal = "&lt;b&gt;literal&lt;/b&gt; &amp; &#60; &#x3c; &quot;"
    card = render_card(view(text=literal, contact_name=literal, excerpts=(literal,)))
    text = shown(card.text)
    assert html.unescape(text).count(literal) == 3
    assert "<b>literal</b>" not in text


def test_line_breaks_in_the_text_and_the_excerpts_reach_telegram_as_line_breaks() -> None:
    """FR-DRF-03 (the exact text) and FR-DRF-06 ("copy the text above"): a
    line break is shown as one, never as a visible `\\u000a`. Hidden and bidi
    characters stay visible escapes, and a name stays on one line."""
    draft = "Hi Alex,\nSee you Monday.\n\nBest"
    card = render_card(
        view(text=draft, excerpts=("Line one\nline two",), contact_name="Alex\nSent ✓ 18:05")
    )
    text = shown(card.text)
    assert "\\u000a" not in text.replace("Alex\\u000aSent", "")
    assert "<blockquote>Hi Alex,\nSee you Monday.\n\nBest</blockquote>" in text
    # Every line of the contact's words is marked as theirs, so a line break
    # in them can never start what looks like one of the card's own lines.
    assert "› <i>Line one</i>\n› <i>line two</i>" in text  # noqa: RUF001
    # A name is one line: a line break in it stays visible.
    assert "To: <b>Alex\\u000aSent ✓ 18:05</b>" in text
    # The copy button carries the exact text, line breaks included.
    assert copies(card_keyboard(in_state(ActionState.BLOCKED, text=draft))) == [draft]
    # Still made visible: a carriage return, a tab, bidi and zero-width characters.
    tricky = shown(render_card(view(text="a\r\nb\tc\n‮d​")).text)
    assert "a\\u000d\nb\\u0009c\n\\u202ed\\u200b" in tricky
    # Content shaped like a tag across a line break still never becomes markup.
    split_tag = shown(render_card(view(text='<a\nhref="https://x.example">x</a>')).text)
    assert "<a" not in split_tag
    # A long multi-line text: the message above and the card's preview keep its lines.
    long_text = "\n\n".join(
        f"Synthetic paragraph {n}: " + " ".join(["words"] * 60) for n in range(20)
    )
    long_card = render_card(view(text=long_text))
    assert long_card.full_text is not None
    above = render_control_message(long_card.full_text)
    assert all("\\u000a" not in part for part in above)
    assert "words\n\nSynthetic paragraph 1: words" in above[0]
    preview = shown(long_card.text)
    assert "\\u000a" not in preview
    assert "words\n\nSynthetic paragraph 1: words" in preview
    # Over 256 characters the owner copies the text shown: it is the exact text.
    blocked = shown(render_card(in_state(ActionState.BLOCKED, text=long_text)).text)
    assert "Copy the text above" in blocked
    assert copies(card_keyboard(in_state(ActionState.BLOCKED, text=long_text))) == []


def test_a_card_is_one_gateway_chunk_however_heavy_its_content() -> None:
    """QA B2 (#120): escaping can make content several times longer, and an
    edit keeps only the gateway's first chunk — so the whole card, buttons'
    state included, must always be one chunk."""
    heavy = ("&" * 300,) * 3
    card = render_card(view(excerpts=heavy))
    chunks = render_control_message(card.text)
    assert len(chunks) == 1, [len(chunk) for chunk in chunks]
    [text] = chunks
    # The short draft stays on its card; the contact's words are shortened.
    assert card.full_text is None
    assert f"<blockquote>{TEXT}</blockquote>" in text
    assert "› <i>&amp;&amp;" in text  # noqa: RUF001
    assert "Draft — awaiting your approval" in text
    assert ops(card.keyboard) == ["ok", "ed", "rj"]
    # The worst content allowed: a long text of escapes, a name of escapes
    # and three 300-character excerpts of escapes, in every state.
    worst = {
        "contact_name": "‮" * 128,
        "excerpts": ("\U000e0001" * 300,) * 3,
    }
    for text_value in ("&" * 5000, "\U000e0001" * 5000, "x" * 3900):
        for state in ActionState:
            rendered = render_card(in_state(state, text=text_value, **worst))
            parts = render_control_message(rendered.text)
            assert len(parts) == 1, (state, len(text_value), [len(part) for part in parts])
            assert len(parts[0]) <= CARD_BUDGET
            assert "📝" in parts[0]
            if rendered.full_text is not None:
                assert "Full text in the message above" in parts[0]


def test_an_approved_card_with_sending_off_says_so_and_never_sending() -> None:
    """Spec §3.3 `ok` row, decisions R17, invariant 11: with `actions.send`
    off nothing can be sent, so the approved card never says "Sending…"."""
    off = in_state(ActionState.APPROVED, sending_enabled=False)
    card = render_card(off)
    text = shown(card.text)
    assert "Approved — sending is off" in text
    assert "Sending…" not in text
    assert card.keyboard is None
    assert "📝" in text
    # Sending on: the F0 label, as before.
    assert "Sending…" in shown(render_card(in_state(ActionState.APPROVED)).text)


def test_sendable_until_is_the_earlier_of_max_age_and_window_close_in_local_time() -> None:
    # Only an autonomous action is bounded by our window estimate (P2-O20).
    earlier_window = view(
        origin=ActionOrigin.DELEGATION_AUTO,
        expires_at=NOW + timedelta(hours=3),
        window_end=NOW + timedelta(hours=2),
        timezone="Asia/Tashkent",
    )
    assert earlier_window.sendable_until == NOW + timedelta(hours=2)
    text = shown(render_card(earlier_window).text)
    assert "Approve by 2026-10-28 19:00 +05" in text  # 14:00 UTC, Tashkent
    # An owner-approved card: the approval period alone, Telegram decides the window.
    for origin in (ActionOrigin.OWNER_DICTATED, ActionOrigin.AGENT_PROPOSAL):
        owner = replace(earlier_window, origin=origin)
        assert owner.sendable_until == NOW + timedelta(hours=3)
    earlier_expiry = view(expires_at=NOW + timedelta(minutes=30))
    assert "Approve by 2026-10-28 12:30 UTC" in shown(render_card(earlier_expiry).text)
    # Explicit timezone override (previews, tests).
    assert "Approve by 2026-10-28 13:30 CET" in shown(
        render_card(earlier_expiry, timezone="Europe/Berlin").text
    )
    # Only a live card is "sendable until".
    assert "Approve by" not in shown(render_card(in_state(ActionState.APPROVED)).text)


def test_keyboards_per_state() -> None:
    awaiting = card_keyboard(in_state(ActionState.AWAITING_APPROVAL))
    assert ops(awaiting) == ["ok", "ed", "rj"]
    assert {parse(b["callback_data"]).token for b in buttons(awaiting)} == {"AbCdEf1234"}
    revising = card_keyboard(in_state(ActionState.REVISING))
    assert ops(revising) == ["cx"]
    assert parse(buttons(revising)[0]["callback_data"]).token == "CardTok123"
    for state in (ActionState.OUTDATED, ActionState.EXPIRED):
        agent = card_keyboard(in_state(state))
        assert ops(agent) == ["rd"]
        assert copies(agent) == [TEXT]
        assert parse(buttons(agent)[0]["callback_data"]).token == "CardTok123"
        dictated = card_keyboard(in_state(state, origin=ActionOrigin.OWNER_DICTATED))
        assert ops(dictated) == []
        assert copies(dictated) == [TEXT]
        requested = card_keyboard(in_state(state, redraft_requested=True))
        assert ops(requested) == []
        assert copies(requested) == [TEXT]
    for state in (ActionState.BLOCKED, ActionState.FAILED):
        assert ops(card_keyboard(in_state(state))) == []
        assert copies(card_keyboard(in_state(state))) == [TEXT]
    for state in (
        ActionState.SUCCEEDED,
        ActionState.DELIVERY_UNCERTAIN,
        ActionState.REJECTED,
        ActionState.CANCELLED,
        ActionState.APPROVED,
        ActionState.EXECUTING,
    ):
        assert card_keyboard(in_state(state)) is None, state


def test_copy_button_only_for_1_to_256_characters() -> None:
    fits = "x" * COPY_TEXT_MAX
    assert copies(card_keyboard(in_state(ActionState.BLOCKED, text=fits))) == [fits]
    # UTF-16 counting: 128 astral characters are 256 units and still fit.
    astral = "😀" * 128
    assert copies(card_keyboard(in_state(ActionState.BLOCKED, text=astral))) == [astral]
    for too_long in ("x" * (COPY_TEXT_MAX + 1), "😀" * 129):
        blocked = in_state(ActionState.BLOCKED, text=too_long)
        assert copies(card_keyboard(blocked)) == []
        assert "Copy the text above" in shown(render_card(blocked).text)
    for nothing in ("", "   ", None):
        assert copies(card_keyboard(in_state(ActionState.BLOCKED, text=nothing))) == []
    assert (
        "(The text is no longer stored.)"
        in render_card(in_state(ActionState.BLOCKED, text=None)).text
    )


def test_a_long_draft_moves_its_full_text_above_the_card() -> None:
    long_text = ("Synthetic paragraph & more. " * 160).strip()  # ~4480 characters
    card = render_card(view(text=long_text, excerpts=tuple("e" * 300 for _ in range(3))))
    assert card.full_text is not None
    # Whole, in one message or (over Telegram's limit) split on spaces.
    parts = " ".join(render_control_message(card.full_text))
    assert long_text.replace("&", "&amp;").split() == parts.split()[5:]
    text = shown(card.text)
    assert f"Full text in the message above ({len(long_text)} characters)" in text
    assert long_text[:PREVIEW_CHARS].strip().replace("&", "&amp;")[:500] in text
    assert long_text not in text
    # Still one control message, buttons included.
    assert len(render_control_message(card.text)) == 1
    assert ops(card.keyboard) == ["ok", "ed", "rj"]
    # A short draft stays on its card.
    assert render_card(view()).full_text is None


def test_callback_data_fits_64_bytes() -> None:
    for state in ActionState:
        markup = card_keyboard(
            in_state(state, card_token=new_token())
            if state is not ActionState.AWAITING_APPROVAL
            else view(approval_token=new_token())
        )
        for button in buttons(markup):
            if "callback_data" in button:
                data = button["callback_data"]
                assert len(data.encode()) <= MAX_CALLBACK_DATA_BYTES
                assert parse(data).kind == "act"


def test_warnings_come_from_draft_flags_and_never_for_dictated_words() -> None:
    flags = {"makes_commitment": True, "sensitive_topic": True}
    text = shown(render_card(view(draft_flags=flags)).text)
    assert "Contains a commitment (date, amount or promise) — check it" in text
    assert "Sensitive topic" in text
    dictated = shown(render_card(view(draft_flags=flags, origin=ActionOrigin.OWNER_DICTATED)).text)
    assert "commitment" not in dictated
    assert "Sensitive" not in dictated
    assert "Message you dictated" in dictated
    assert "Assist draft" in shown(render_card(view(proactive=True)).text)


# ------------------------------------------------- P2-O20: Telegram decides
@pytest.mark.parametrize("origin", [ActionOrigin.OWNER_DICTATED, ActionOrigin.AGENT_PROPOSAL])
def test_an_owner_approved_card_with_a_closed_window_warns_and_stays_approvable(
    origin: ActionOrigin,
) -> None:
    """Invariant 8 as amended (owner decision 2026-10-30): the card is
    presented normally with one factual line; Approve stays."""
    closed = view(origin=origin, window_open=False, window_end=NOW - timedelta(hours=1))
    rendered = render_card(closed)
    text = shown(rendered.text)
    assert (
        "⚠️ Synthetic Contact hasn't written in the last 24 hours, so Telegram may not allow "
        "this send." in text
    )
    assert ops(rendered.keyboard) == ["ok", "ed", "rj"]
    assert "Approve by 2026-10-28 13:00 UTC" in text  # max age only
    # Open window: no warning.
    assert "hasn't written" not in shown(render_card(view(origin=origin)).text)
    # A finished card never warns.
    assert "hasn't written" not in shown(
        render_card(in_state(ActionState.REJECTED, origin=origin, window_open=False)).text
    )


def test_an_autonomous_card_never_shows_the_owner_window_warning() -> None:
    auto = view(origin=ActionOrigin.DELEGATION_AUTO, window_open=False)
    assert "hasn't written" not in shown(render_card(auto).text)


def test_telegram_refusing_the_window_gives_a_not_sent_card_with_copy_ready_text() -> None:
    """PEER_ID_INVALID (TF §12 LV-08) ends `expired`/`window_closed` with the
    Telegram error recorded: the card says plainly it was not sent and why,
    offers the text to copy, and never says sent or uncertain."""
    refused = in_state(
        ActionState.EXPIRED, reason=StateReason.WINDOW_CLOSED, error_class="window_closed"
    )
    rendered = render_card(refused)
    text = shown(rendered.text)
    assert (
        "<b>Not sent</b> — Telegram didn't allow this send "
        "(the contact hasn't written in the last 24 hours)" in text
    )
    assert "Nothing was sent and it will not be retried. Copy the text to send it yourself." in text
    assert TEXT in text
    assert copies(rendered.keyboard) == [TEXT]
    assert "Sent ✓" not in text
    assert "uncertain" not in text.lower()
    # Our own (autonomous) pre-check expiry keeps the old wording.
    estimate = in_state(ActionState.EXPIRED, reason=StateReason.WINDOW_CLOSED)
    assert "Telegram didn't allow" not in shown(render_card(estimate).text)


def test_pf02_a_card_without_excerpts_has_no_answering_section() -> None:
    """A dictated or standalone card answers no message: nothing to quote."""
    text = shown(render_card(view(excerpts=())).text)
    assert "Answering" not in text
    assert "›" not in text  # noqa: RUF001
    assert "Answering (their words):" in shown(render_card(view()).text)
