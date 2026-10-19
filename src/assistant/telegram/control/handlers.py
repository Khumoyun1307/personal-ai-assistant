"""Control-chat command handlers (ARCHITECTURE §6.7).

Every reply goes through `OutboundGateway`. These handlers never call
`Message.answer()`, `reply()` or any other aiogram shortcut — those auto-inject
a `business_connection_id` and would send **as the owner** (AGENTS §7). An AST
test scans this package to keep that true.

Handlers are plain async functions over our own types, not aiogram models, so
they can be tested without constructing Telegram objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts import service
from assistant.accounts.models import Account
from assistant.core.clock import Clock
from assistant.telegram.gateway import ControlChat, OutboundGateway, SendResult
from assistant.telegram.render import escape

#: The version of the consent text and policy currently in force.
POLICY_VERSION = "v1"

#: Named in the consent, because consent has to say who the data goes to.
AI_PROCESSORS = ["OpenAI"]

#: Stop words treated as `/pause` before any model is called (§6.7). Matched
#: against the whole message, so an ordinary sentence containing "stop" does
#: not trip it.
# The Uzbek forms deliberately include both the modifier letter turned comma
# (the correct character) and the ASCII apostrophe people type on phone
# keyboards. Both have to match.
STOP_WORDS: frozenset[str] = frozenset(
    {"stop", "стоп", "to'xta", "toxta", "toʻxta", "тўхта", "pause", "пауза"}
)


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """A control-chat message, reduced to what handlers need."""

    account_id: int
    chat_id: int
    telegram_message_id: int
    text: str
    from_id: int
    #: When Telegram delivered it to us (`inbound_updates.received_at`), for
    #: the acknowledgement-latency metric. None outside the inbox (tests).
    received_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """What a handler did, so the caller can record and assert on it."""

    handled: bool
    command: str
    send: SendResult | None = None
    note: str | None = None


def is_stop_word(text: str) -> bool:
    """Whole-message match only — never a substring."""
    return text.strip().lower().strip("!.") in STOP_WORDS


def parse_command(text: str) -> tuple[str, str]:
    """Split `/cmd arg` into `("cmd", "arg")`. Non-commands give `("", text)`.

    Handles the `/cmd@botname` form Telegram adds in groups.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return "", stripped
    head, _, rest = stripped.partition(" ")
    command = head[1:].split("@", 1)[0].lower()
    return command, rest.strip()


HELP_TEXT = (
    "<b>What I can do</b>\n\n"
    "/start — set up or re-run onboarding\n"
    "/help — this message\n"
    "/status — what I am doing and whether anything is wrong\n"
    "/settings — language and timezone\n"
    "/privacy — what I store, who sees it, and how to delete it\n\n"
    "Send me anything else and I will answer it.\n"
    "Send <b>stop</b> at any time to pause everything."
)


async def handle_start(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    message: IncomingMessage,
) -> HandlerResult:
    """`/start` — create the account, record where to reach the owner, ask for consent."""
    account, created = await service.get_or_create_account(
        session, telegram_user_id=message.from_id, clock=clock
    )
    await service.set_control_chat(session, account, message.chat_id)

    consent = await service.active_consent(session, account.id, kind=service.CONSENT_AI_CONTROL)
    target = ControlChat(account_id=account.id, chat_id=message.chat_id)

    if consent.granted:
        result = await gateway.send_text(
            target,
            "You are already set up. /help shows what I can do.",
        )
        return HandlerResult(True, "start", result, note="already_onboarded")

    # Consent is asked for explicitly, naming the processor, before anything
    # is sent to it (§6 invariant 9).
    await service.set_pending_input(
        session,
        account_id=account.id,
        kind="onboarding_consent",
        clock=clock,
        prompt_message_id=message.telegram_message_id,
    )
    result = await gateway.send_text(
        target,
        "<b>Before we start</b>\n\n"
        "To answer you, I send the text of this chat to "
        f"<b>{', '.join(AI_PROCESSORS)}</b>. Nothing is sent anywhere else, and "
        "your messages are not used to train any model.\n\n"
        "See /privacy for what I store and how to delete it.\n\n"
        "Reply <b>yes</b> to agree, or anything else to decline. "
        "You can revoke this later at any time.",
    )
    return HandlerResult(True, "start", result, note="created" if created else "consent_requested")


async def handle_consent_reply(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    message: IncomingMessage,
    account: Account,
) -> HandlerResult:
    """The owner's answer to the consent question."""
    target = ControlChat(account_id=account.id, chat_id=message.chat_id)
    agreed = message.text.strip().lower() in {"yes", "ha", "да", "y", "ok", "okay"}
    await service.clear_pending_input(session, account_id=account.id)

    if not agreed:
        result = await gateway.send_text(
            target,
            "No problem — nothing will be sent to an AI provider.\n"
            "/settings and /privacy still work. Run /start again to change your mind.",
        )
        return HandlerResult(True, "consent", result, note="declined")

    await service.grant_consent(
        session,
        account,
        kind=service.CONSENT_AI_CONTROL,
        policy_version=POLICY_VERSION,
        processors=AI_PROCESSORS,
        clock=clock,
    )
    account.status = "active"
    result = await gateway.send_text(
        target, "Done. Ask me anything, or see /help.\n\nSend <b>stop</b> to pause."
    )
    return HandlerResult(True, "consent", result, note="granted")


async def handle_help(
    gateway: OutboundGateway, account: Account, message: IncomingMessage
) -> HandlerResult:
    result = await gateway.send_text(
        ControlChat(account_id=account.id, chat_id=message.chat_id), HELP_TEXT
    )
    return HandlerResult(True, "help", result)


async def handle_privacy(
    session: AsyncSession,
    gateway: OutboundGateway,
    account: Account,
    message: IncomingMessage,
    *,
    policy_url: str | None = None,
) -> HandlerResult:
    """`/privacy` — say plainly what is stored and who sees it (FR-PRV-01)."""
    consent = await service.active_consent(session, account.id, kind=service.CONSENT_AI_CONTROL)
    state = (
        f"granted (policy {consent.policy_version}, "
        f"processor: {', '.join(consent.processors) or 'none named'})"
        if consent.granted
        else "<b>not granted</b> — nothing is sent to an AI provider"
    )
    text = (
        "<b>Privacy</b>\n\n"
        f"AI consent: {state}\n\n"
        "In this chat I store your messages and my replies for 90 days, your "
        "settings, and a content-free record of what I did.\n\n"
        "Messages from anyone who is not you are discarded — I keep no record "
        "of what they said.\n\n"
        "Your messages are never used to train a model."
    )
    if consent.granted:
        text += "\n\nSend <code>/privacy revoke</code> to stop all AI processing."
    if policy_url:
        text += f'\n\nFull policy: <a href="{escape(policy_url)}">{escape(policy_url)}</a>'
    result = await gateway.send_text(
        ControlChat(account_id=account.id, chat_id=message.chat_id), text
    )
    return HandlerResult(True, "privacy", result)


async def handle_revoke(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    message: IncomingMessage,
) -> HandlerResult:
    """`/privacy revoke` — consent is revocable at any time (FR-PRV-02).

    Takes effect at once: the consent wrapper reads consent on every model
    call, so queued work re-checks and stops (§6 invariant 9).
    """
    revoked = 0
    for kind in (service.CONSENT_AI_CONTROL, service.CONSENT_AI_MANAGED):
        revoked += await service.revoke_consent(session, account, kind=kind, clock=clock)
    target = ControlChat(account_id=account.id, chat_id=message.chat_id)
    if not revoked:
        result = await gateway.send_text(target, "There was no AI consent to revoke.")
        return HandlerResult(True, "revoke", result, note="nothing_to_revoke")
    result = await gateway.send_text(
        target,
        "AI consent revoked. Nothing more will be sent to an AI provider.\n"
        "/settings, /status and /privacy still work. Send /start to agree again.",
    )
    return HandlerResult(True, "revoke", result, note="revoked")


async def handle_status(
    gateway: OutboundGateway,
    account: Account,
    message: IncomingMessage,
    *,
    backlog: dict[str, int] | None = None,
) -> HandlerResult:
    """`/status` — must work even when the model is unreachable."""
    counts = backlog or {}
    lines = [
        "<b>Status</b>",
        "",
        f"Account: {account.status}",
        f"Autonomy: {'paused' if account.autonomy_paused else 'active'}",
        f"Timezone: {account.timezone}",
        "",
        "<b>Inbox</b>",
    ]
    if counts:
        lines += [f"  {status}: {count}" for status, count in sorted(counts.items())]
    else:
        lines.append("  nothing pending")
    failed = counts.get("failed", 0)
    if failed:
        lines += ["", f"⚠️ {failed} update(s) could not be processed."]
    result = await gateway.send_text(
        ControlChat(account_id=account.id, chat_id=message.chat_id), "\n".join(lines)
    )
    return HandlerResult(True, "status", result)


async def handle_settings(
    gateway: OutboundGateway, account: Account, message: IncomingMessage
) -> HandlerResult:
    """`/settings` — deterministic, no model involved, so it always works."""
    text = (
        "<b>Settings</b>\n\n"
        f"Language: {account.locale_pref or 'auto'}\n"
        f"Timezone: {account.timezone}\n\n"
        "Send <code>/settings timezone Asia/Tashkent</code> or "
        "<code>/settings language uz</code> to change one."
    )
    result = await gateway.send_text(
        ControlChat(account_id=account.id, chat_id=message.chat_id), text
    )
    return HandlerResult(True, "settings", result)


async def apply_setting(
    session: AsyncSession,
    gateway: OutboundGateway,
    account: Account,
    message: IncomingMessage,
    argument: str,
) -> HandlerResult:
    """`/settings <key> <value>`. Validated, not trusted."""
    target = ControlChat(account_id=account.id, chat_id=message.chat_id)
    key, _, value = argument.partition(" ")
    key, value = key.strip().lower(), value.strip()

    if not value:
        return await handle_settings(gateway, account, message)

    if key == "timezone":
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError, ValueError, KeyError:
            result = await gateway.send_text(
                target, f"I do not know the timezone <code>{value}</code>."
            )
            return HandlerResult(True, "settings", result, note="invalid_timezone")
        account.timezone = value
        await session.flush()
        result = await gateway.send_text(target, f"Timezone set to {value}.")
        return HandlerResult(True, "settings", result, note="timezone")

    if key in {"language", "locale"}:
        if len(value) > 8 or not value.replace("-", "").isalnum():
            result = await gateway.send_text(target, "That does not look like a language code.")
            return HandlerResult(True, "settings", result, note="invalid_language")
        account.locale_pref = value
        await session.flush()
        result = await gateway.send_text(target, f"Language set to {value}.")
        return HandlerResult(True, "settings", result, note="language")

    result = await gateway.send_text(target, "I can set <b>timezone</b> or <b>language</b>.")
    return HandlerResult(True, "settings", result, note="unknown_key")


REFUSAL_TEXT = (
    "This is a personal assistant and I only work for its owner. I will not reply further."
)


async def send_refusal(
    gateway: OutboundGateway, *, chat_id: int, account_id: int = 0
) -> SendResult:
    """The fixed reply to a non-allowlisted user (§6 invariant 12).

    One fixed sentence, no model, no state. `account_id` is 0 because there is
    no account: this person is not a tenant and must never become one.
    """
    return await gateway.send_text(
        ControlChat(account_id=account_id, chat_id=chat_id), REFUSAL_TEXT
    )


def describe_unknown_command(command: str) -> str:
    return f"I do not know /{command}. Try /help."


__all__ = [
    "AI_PROCESSORS",
    "HELP_TEXT",
    "POLICY_VERSION",
    "REFUSAL_TEXT",
    "STOP_WORDS",
    "HandlerResult",
    "IncomingMessage",
    "apply_setting",
    "describe_unknown_command",
    "handle_consent_reply",
    "handle_help",
    "handle_privacy",
    "handle_revoke",
    "handle_settings",
    "handle_start",
    "handle_status",
    "is_stop_word",
    "parse_command",
    "send_refusal",
]
