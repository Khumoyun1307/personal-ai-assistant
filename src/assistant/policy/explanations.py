"""Owner-facing sentences for failed policy checks (FR-ACT-03; S04.1 spec §3.3).

One plain sentence (or two) per failure code: what happened and what the owner
can do. They never quote message text or names, so a card can show them as is.
English only in P2 (P2-O11); a locale layer can wrap this table later.
"""

from collections.abc import Mapping
from types import MappingProxyType

from assistant.policy.context import Decision

EXPLANATIONS: Mapping[str, str] = MappingProxyType(
    {
        "account_inactive": (
            "Your Alviro account is not active, so nothing can be sent for you. "
            "Finish setting up Alviro, or copy the text and send it yourself."
        ),
        "no_connection": (
            "Alviro is not connected to your Telegram account, so it cannot reply in your "
            "chats. Connect it in Telegram's Chat Automation settings, or copy the text and "
            "send it yourself."
        ),
        "connection_stale": (
            "Telegram no longer accepts Alviro's old connection to your account. Reconnect "
            "Alviro in Telegram's Chat Automation settings, or copy the text and send it "
            "yourself."
        ),
        "connection_disabled": (
            "Alviro's Chat Automation connection is turned off or not confirmed. Turn it on "
            "in Telegram's Chat Automation settings, or copy the text and send it yourself."
        ),
        "no_reply_right": (
            "Alviro is connected without the right to reply in your chats. Turn on "
            '"Reply to messages" in Telegram\'s Chat Automation settings, or copy the text '
            "and send it yourself."
        ),
        "not_owned": (
            "This chat does not belong to your account, so Alviro will not act in it. "
            "Nothing was sent."
        ),
        "bot_chat": (
            "This chat is with a bot, and Alviro never replies to bots for you. "
            "Reply yourself if you need to."
        ),
        "chat_ignored": (
            "This chat is set to Ignore, so Alviro will not reply in it. Change the chat's "
            "mode if you want replies here, or reply yourself."
        ),
        "chat_possibly_paused": (
            "Alviro may be paused in this chat in Telegram, so the reply was held back. "
            "Check the chat in Telegram and retry, or copy the text and send it yourself."
        ),
        "window_closed": (
            "Telegram's 24-hour reply window for this chat has closed, so a reply cannot be "
            "sent. Copy the text and send it yourself if you still want to."
        ),
        "takeover": "You replied in this chat yourself after this draft was made.",
        "source_deleted": (
            "The message this draft answers was deleted. Ask for a new draft if you still "
            "want to reply."
        ),
        "new_contact_message": (
            "A newer message arrived in this chat after the draft was shown to you. "
            "Read the chat and ask for a new draft."
        ),
        "already_claimed": (
            "This reply is already being sent or was already handled, so it will not be "
            "sent again. Check the chat to see the result."
        ),
        "max_age": (
            "This draft waited too long for approval and has expired. Ask for a new draft "
            "if you still want to reply."
        ),
        "inbox_pending": (
            "Alviro is still processing new messages from Telegram, so the reply waits until "
            "they are checked. You do not need to do anything."
        ),
        "barrier_pending": (
            "Alviro has not yet caught up with Telegram after starting, so the reply waits. "
            "You do not need to do anything."
        ),
        "not_assistant_message": (
            "Alviro can change or delete only messages it sent itself, never yours or your "
            "contact's. Make the change yourself in Telegram if you need to."
        ),
        "empty_text": (
            "The reply is empty, and Telegram does not send empty messages. Edit the draft "
            "to add text."
        ),
        "too_long": (
            "The reply is longer than Telegram allows in one message, and Alviro never "
            "splits it. Edit the draft to make it shorter."
        ),
        "consent_missing": (
            "AI permission for your managed chats is not active, so an AI-written reply "
            "cannot be sent. Turn the permission back on, or copy the text and send it "
            "yourself."
        ),
        "sending_disabled": (
            "Sending from Alviro is turned off, so this reply was not sent. Copy the text "
            "and send it yourself."
        ),
        "autonomy_paused": (
            "Automatic replies are paused, so this automatic reply was not sent. Resume "
            "automatic replies if you want them again."
        ),
        "no_delegation": (
            "No active delegation covers this chat, so Alviro will not reply automatically. "
            "Drafts still come to you for approval."
        ),
    }
)

#: For a code this build does not know (a decision stored by another version).
_FALLBACK = "A required sending check did not pass."


def explain(decision: Decision) -> tuple[str, ...]:
    """The sentences for the decision's failed checks, in check order."""
    return tuple(
        EXPLANATIONS.get(check.reason, _FALLBACK) for check in decision.checks if not check.passed
    )
