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

from assistant.accounts import connections, service
from assistant.accounts.models import Account
from assistant.conversations.visibility import ConversationPanel
from assistant.core.clock import Clock
from assistant.observability.status import StatusReport
from assistant.telegram.gateway import ControlChat, OutboundGateway, SendResult
from assistant.telegram.render import escape

#: The version of the consent text and policy currently in force.
POLICY_VERSION = service.POLICY_VERSION

#: Named in the consent, because consent has to say who the data goes to.
AI_PROCESSORS = list(service.AI_PROCESSORS)

#: Answers that count as agreement to a consent question. Anything else declines.
AGREEMENT_WORDS = frozenset({"yes", "ha", "да", "y", "ok", "okay"})

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
    "/status — what I can see: connection, rights, coverage, gaps, consent\n"
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
    agreed = message.text.strip().lower() in AGREEMENT_WORDS
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


MANAGED_CONSENT_TEXT = (
    "<b>AI for your chats with other people</b>\n\n"
    "To summarize the chats you included and help you with them, I need to send "
    "their text to <b>{processors}</b>. Nothing is sent anywhere else, and it is "
    "not used to train any model. Until you agree, nothing from those chats is "
    "sent to any AI provider.\n\n"
    "Reply <b>yes</b> to agree, or anything else to decline. "
    "You can revoke this at any time with <code>/privacy revoke</code>."
)


async def handle_managed_consent_request(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    message: IncomingMessage,
) -> HandlerResult:
    """`/privacy managed` — ask for consent to process managed chats (FR-ONB-05b)."""
    target = ControlChat(account_id=account.id, chat_id=message.chat_id)
    state = await service.active_consent(session, account.id, kind=service.CONSENT_AI_MANAGED)
    if state.granted:
        result = await gateway.send_text(
            target, "You have already agreed. Send <code>/privacy revoke</code> to withdraw."
        )
        return HandlerResult(True, "managed_consent", result, note="already_granted")
    if await connections.connection_for_account(session, account.id) is None:
        result = await gateway.send_text(
            target,
            "I am not connected to your account yet. Connect me in your Telegram settings "
            "(Chat Automation, or Telegram Business → Chatbots in older apps) first; "
            "I will ask you then.",
        )
        return HandlerResult(True, "managed_consent", result, note="not_connected")
    await service.set_pending_input(
        session,
        account_id=account.id,
        kind=service.PENDING_MANAGED_CONSENT,
        clock=clock,
        ttl=service.MANAGED_CONSENT_TTL,
        prompt_message_id=message.telegram_message_id,
    )
    result = await gateway.send_text(
        target, MANAGED_CONSENT_TEXT.format(processors=", ".join(AI_PROCESSORS))
    )
    return HandlerResult(True, "managed_consent", result, note="requested")


async def handle_managed_consent_reply(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    message: IncomingMessage,
    account: Account,
) -> HandlerResult:
    """The owner's answer to the managed-conversation consent question."""
    target = ControlChat(account_id=account.id, chat_id=message.chat_id)
    agreed = message.text.strip().lower() in AGREEMENT_WORDS
    await service.clear_pending_input(session, account_id=account.id)
    if not agreed:
        result = await gateway.send_text(
            target,
            "Understood — nothing from your chats with other people will be sent to an "
            "AI provider. Send <code>/privacy managed</code> if you change your mind.",
        )
        return HandlerResult(True, "managed_consent", result, note="declined")
    await service.grant_consent(
        session,
        account,
        kind=service.CONSENT_AI_MANAGED,
        policy_version=POLICY_VERSION,
        processors=AI_PROCESSORS,
        clock=clock,
    )
    result = await gateway.send_text(
        target,
        "Thank you — recorded. From now on I may send the text of the chats you "
        f"included to {', '.join(AI_PROCESSORS)} when I work on them for you.",
    )
    return HandlerResult(True, "managed_consent", result, note="granted")


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
    managed = await service.active_consent(session, account.id, kind=service.CONSENT_AI_MANAGED)
    connected = await connections.connection_for_account(session, account.id) is not None

    def describe(state: service.ConsentState) -> str:
        if not state.granted:
            return "<b>not granted</b> — nothing is sent to an AI provider"
        return (
            f"granted (policy {state.policy_version}, "
            f"processor: {', '.join(state.processors) or 'none named'})"
        )

    text = (
        "<b>Privacy</b>\n\n"
        f"AI for this chat: {describe(consent)}\n"
        f"AI for your chats with other people: {describe(managed)}\n\n"
        "In this chat I store your messages and my replies for 90 days, your "
        "settings, and a content-free record of what I did.\n\n"
        "Messages from anyone who is not you are discarded — I keep no record "
        "of what they said.\n\n"
        "Your messages are never used to train a model."
    )
    if connected and not managed.granted:
        text += "\n\nSend <code>/privacy managed</code> to decide about your other chats."
    if consent.granted or managed.granted:
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


def local_time(value: datetime | None, timezone: str) -> str:
    """A timestamp in the owner's timezone, for presentation only (AGENTS §9)."""
    if value is None:
        return "—"
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError, KeyError:
        zone = ZoneInfo("UTC")
    return value.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")


TIER_TEXT = {
    "none": "no permissions (disabled or no rights)",
    "observe_only": "observe only (no reply right)",
    "can_reply": "can reply",
    "extended": "can reply, plus rights I do not use",
}


def render_status(report: StatusReport, *, visibility: str) -> str:
    """The visibility report (FR-ONB-08): what I can see, and what is wrong.

    Every line comes from `StatusReport`, so the `get_status` tool and this
    command cannot disagree. The coverage caveat is never swallowed
    (§6 invariant 11).
    """
    tz = report.timezone
    lines = [
        "<b>Status</b>",
        "",
        f"Account: {escape(report.account_status)}",
        f"Autonomy: {'paused' if report.autonomy.paused else 'active'}",
        f"Timezone: {escape(tz)}",
        "",
        "<b>Connection</b>",
    ]
    conn = report.connection
    if not conn.present:
        lines.append("  Not connected. Add me under Settings → Chat Automation.")
    else:
        tier = TIER_TEXT.get(conn.tier or "", conn.tier or "unknown")
        lines += [
            f"  State: {escape(conn.status or 'unknown')}",
            f"  Rights: {escape(tier)}",
            f"  Connected since: {local_time(conn.connected_at, tz)}",
        ]
    cov = report.coverage
    lines += ["", "<b>Coverage</b>"]
    if cov.conversations == 0:
        lines.append("  No managed chats observed yet.")
    else:
        modes = ", ".join(f"{count} {mode}" for mode, count in sorted(cov.by_mode.items()))
        lines += [
            f"  Chats observed: {cov.conversations} ({escape(modes)})",
            f"  Observing since: {local_time(cov.coverage_started_at, tz)}",
        ]
    lines.append(f"  {escape(visibility)}")
    if cov.latest_gap is not None:
        gap = cov.latest_gap
        lines.append(
            f"  Latest gap ({escape(gap.certainty)}): {local_time(gap.at_risk_from, tz)} → "
            f"{local_time(gap.at_risk_to, tz)}"
        )
    consent = report.consent
    lines += [
        "",
        "<b>AI consent</b>",
        f"  This chat: {'granted' if consent.control_chat else 'not granted'}",
        f"  Included chats: {'granted' if consent.managed_chats else 'not granted'}",
    ]
    if report.autonomy.conversations_paused:
        detail = ", ".join(
            f"{count} {reason}"
            for reason, count in sorted(report.autonomy.conversations_paused.items())
        )
        lines += ["", "<b>Autonomy per chat</b>", f"  Paused: {escape(detail)}"]
    inbox = report.inbox
    lines += ["", "<b>Inbox</b>"]
    if inbox.pending or inbox.failed:
        lines.append(f"  pending: {inbox.pending}, failed: {inbox.failed}")
    else:
        lines.append("  nothing pending")
    if inbox.poller_heartbeat_age_seconds is not None:
        lines.append(f"  Last poll: {inbox.poller_heartbeat_age_seconds} s ago")
    lines += [
        "",
        "<b>AI spend this month</b>",
        f"  ${escape(report.budget.month_to_date_usd)}"
        + (
            f" of ${escape(report.budget.monthly_limit_usd)}"
            if report.budget.monthly_limit_usd
            else ""
        ),
    ]
    if inbox.failed:
        lines += ["", f"⚠️ {inbox.failed} update(s) could not be processed."]
    if report.coverage_caveat:
        lines += ["", escape(report.coverage_caveat)]
    return "\n".join(lines)


async def handle_status(
    gateway: OutboundGateway,
    account: Account,
    message: IncomingMessage,
    report: StatusReport,
    *,
    visibility: str,
) -> HandlerResult:
    """`/status` — must work even when the model is unreachable."""
    result = await gateway.send_text(
        ControlChat(account_id=account.id, chat_id=message.chat_id),
        render_status(report, visibility=visibility),
    )
    return HandlerResult(True, "status", result)


PANEL_UNKNOWN = "I don't have that chat. Only chats included under Chat Automation appear here."


def render_panel(panel: ConversationPanel, *, timezone: str) -> str:
    name = panel.display_name or "(no name)"
    handle = f" @{escape(panel.username)}" if panel.username else ""
    counts = ", ".join(
        f"{count} {direction.replace('_', ' ')}"
        for direction, count in sorted(panel.counts_by_direction.items())
    )
    window = (
        f"open until about {local_time(panel.window_closes_at, timezone)}"
        if panel.window_open
        else "closed (no contact message in the last 24 h)"
    )
    lines = [
        f"<b>{escape(name)}</b>{handle}",
        "",
        f"Mode: {escape(panel.mode)}",
        f"First seen: {local_time(panel.first_seen_at, timezone)}",
        f"Observing since: {local_time(panel.coverage_started_at, timezone)}",
        f"Messages: {panel.total}" + (f" ({escape(counts)})" if counts else ""),
    ]
    if panel.deleted or panel.expired:
        lines.append(f"Deleted: {panel.deleted}, expired: {panel.expired}")
    lines += [
        f"Last from contact: {local_time(panel.last_contact_msg_at, timezone)}",
        f"Reply window: {window} (approximate)",
    ]
    if panel.availability != "active":
        lines.append("⚠️ The chat may be paused or excluded; my last action there failed.")
    if panel.autonomy_paused_reason == "ingest_error":
        lines.append("⚠️ A message here could not be processed; automation is paused.")
    if panel.unresolved_gaps:
        lines.append(f"⚠️ {panel.unresolved_gaps} coverage gap(s): some messages may be missing.")
    return "\n".join(lines)


async def handle_conversation_panel(
    gateway: OutboundGateway,
    account: Account,
    message: IncomingMessage,
    panel: ConversationPanel | None,
) -> HandlerResult:
    """`/start bizChat<id>` from "Manage Bot" (TF §12 LV-10).

    Unknown, foreign and malformed ids all get the same reply, so the panel
    reveals nothing about chats that are not this owner's (AGENTS §6.7).
    """
    target = ControlChat(account_id=account.id, chat_id=message.chat_id)
    if panel is None:
        result = await gateway.send_text(target, PANEL_UNKNOWN)
        return HandlerResult(True, "panel", result, note="unknown_chat")
    result = await gateway.send_text(target, render_panel(panel, timezone=account.timezone))
    return HandlerResult(True, "panel", result, note="shown")


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
    "AGREEMENT_WORDS",
    "AI_PROCESSORS",
    "HELP_TEXT",
    "MANAGED_CONSENT_TEXT",
    "POLICY_VERSION",
    "REFUSAL_TEXT",
    "STOP_WORDS",
    "HandlerResult",
    "IncomingMessage",
    "apply_setting",
    "describe_unknown_command",
    "handle_consent_reply",
    "handle_help",
    "handle_managed_consent_reply",
    "handle_managed_consent_request",
    "handle_privacy",
    "handle_revoke",
    "handle_settings",
    "handle_start",
    "handle_status",
    "is_stop_word",
    "parse_command",
    "send_refusal",
]
