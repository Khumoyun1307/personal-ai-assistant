"""The named policy rules (ARCHITECTURE §9.3; S04.1 spec §3.1).

Each rule is a pure function of the `PolicyContext`. It returns `None` when it
does not apply to the context's phase, action kind or origin (and is then left
out of `Decision.checks`), otherwise a `CheckResult` whose reason is `PASSED` or
one of the failure codes declared for it in `REASON_CODES`. Rules read no clock,
environment or globals: everything they need is a fact the loader put in the
context, so a decision can be replayed from its stored input.
"""

from collections.abc import Callable, Mapping
from types import MappingProxyType

from assistant.actions.states import ActionKind, ActionOrigin, ActionState
from assistant.core.delivery import MAX_TEXT_LENGTH
from assistant.core.reply_window import is_open
from assistant.policy.context import CheckResult, Phase, PolicyContext

type Rule = Callable[[PolicyContext], CheckResult | None]

#: The reason of every passing check.
PASSED = "ok"

#: Failure codes per rule, in rule order. A code belongs to exactly one rule,
#: so explanations and the execution mapping key on the code alone.
REASON_CODES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "account_active": ("account_inactive",),
        "connection_active": (
            "no_connection",
            "connection_stale",
            "connection_disabled",
            "no_reply_right",
        ),
        "conversation_owned": ("not_owned", "bot_chat"),
        "conversation_available": ("chat_ignored", "chat_possibly_paused"),
        "window_open": ("window_closed",),
        "no_takeover_since": ("takeover",),
        "not_stale": ("source_deleted", "new_contact_message"),
        "not_already_executed": ("already_claimed",),
        "not_expired": ("max_age",),
        "inbox_drained": ("inbox_pending", "barrier_pending"),
        "target_is_assistant_sent": ("not_assistant_message",),
        "length_ok": ("empty_text", "too_long"),
        "consent_valid": ("consent_missing",),
        "sending_enabled": ("sending_disabled",),
        "autonomy_not_paused": ("autonomy_paused",),
        "delegation_active": ("no_delegation",),
    }
)

_TARGETED_KINDS = frozenset({ActionKind.EDIT_MESSAGE, ActionKind.DELETE_OWN})
_TEXT_KINDS = frozenset({ActionKind.SEND_MESSAGE, ActionKind.EDIT_MESSAGE})


def _check(name: str, failure: str | None) -> CheckResult:
    """A result for `name`: passed when there is no failure code."""
    return CheckResult(name, failure is None, PASSED if failure is None else failure)


def _first(*conditions: tuple[bool, str]) -> str | None:
    """The code of the first failing condition; earlier codes say more to the owner."""
    return next((code for failing, code in conditions if failing), None)


def _execution_only(ctx: PolicyContext) -> bool:
    return ctx.phase is not Phase.EXECUTION


def account_active(ctx: PolicyContext) -> CheckResult:
    return _check("account_active", _first((ctx.account.status != "active", "account_inactive")))


def connection_active(ctx: PolicyContext) -> CheckResult:
    # Most specific first: a missing row, then a connection Telegram no longer
    # accepts (reconnect), then one switched off or not yet confirmed, and only
    # on a live connection the missing reply right (rights are meaningless on a
    # disabled one; fail closed, AGENTS §6.7).
    connection = ctx.connection
    return _check(
        "connection_active",
        _first(
            (not connection.exists, "no_connection"),
            (connection.status == "stale", "connection_stale"),
            (not connection.is_enabled or connection.status != "active", "connection_disabled"),
            (not connection.can_reply, "no_reply_right"),
        ),
    )


def conversation_owned(ctx: PolicyContext) -> CheckResult:
    return _check(
        "conversation_owned",
        _first(
            (ctx.conversation.account_id != ctx.account.account_id, "not_owned"),
            (ctx.conversation.is_bot, "bot_chat"),
        ),
    )


def conversation_available(ctx: PolicyContext) -> CheckResult:
    # An owner retry overrides only "possibly paused" (PRD §12.2); Ignore always wins.
    return _check(
        "conversation_available",
        _first(
            (ctx.conversation.mode == "ignore", "chat_ignored"),
            (
                ctx.conversation.availability == "possibly_paused" and not ctx.action.owner_retry,
                "chat_possibly_paused",
            ),
        ),
    )


def window_open(ctx: PolicyContext) -> CheckResult:
    # Anchored on the contact's last message only; the owner's messages never
    # extend it (TF C-03, conservative until LV-08's reopen half is observed).
    open_now = is_open(ctx.conversation.last_contact_msg_at, ctx.now)
    return _check("window_open", _first((not open_now, "window_closed")))


def no_takeover_since(ctx: PolicyContext) -> CheckResult:
    # Telegram dates on both sides, so a late-ingested owner message still counts.
    # Without a base date (owner-requested actions, decisions R3) the reference
    # is the action's creation: the owner's request postdates their own replies.
    freshness = ctx.freshness
    reference = freshness.base_message_sent_at or ctx.action.created_at
    newest = freshness.newest_owner_manual_sent_at
    return _check(
        "no_takeover_since", _first((newest is not None and newest > reference, "takeover"))
    )


def not_stale(ctx: PolicyContext) -> CheckResult | None:
    if _execution_only(ctx):
        return None
    # Ingestion-order ids only: Telegram dates are never compared with server time.
    newest = ctx.freshness.newest_contact_message_id
    shown = ctx.action.shown_through_message_id
    newer_message = newest is not None and (shown is None or newest > shown)
    return _check(
        "not_stale",
        _first(
            (not ctx.freshness.base_message_present, "source_deleted"),
            (newer_message, "new_contact_message"),
        ),
    )


def not_already_executed(ctx: PolicyContext) -> CheckResult | None:
    if _execution_only(ctx):
        return None
    return _check(
        "not_already_executed",
        _first((ctx.action.state is not ActionState.APPROVED, "already_claimed")),
    )


def not_expired(ctx: PolicyContext) -> CheckResult | None:
    if _execution_only(ctx):
        return None
    expires_at = ctx.action.expires_at
    return _check(
        "not_expired", _first((expires_at is not None and ctx.now >= expires_at, "max_age"))
    )


def inbox_drained(ctx: PolicyContext) -> CheckResult | None:
    if _execution_only(ctx):
        return None
    # Pending rows are named first: they are the concrete thing being waited for.
    return _check(
        "inbox_drained",
        _first(
            (ctx.ingestion.pending_inbound > 0, "inbox_pending"),
            (not ctx.ingestion.barrier_passed, "barrier_pending"),
        ),
    )


def target_is_assistant_sent(ctx: PolicyContext) -> CheckResult | None:
    if ctx.action.kind not in _TARGETED_KINDS:
        return None
    # `None` (no such row in this conversation) fails like an owner or contact
    # message: only our own sends may be edited or deleted (RG-12, invariant 14).
    return _check(
        "target_is_assistant_sent",
        _first((ctx.freshness.target_is_assistant_sent is not True, "not_assistant_message")),
    )


def length_ok(ctx: PolicyContext) -> CheckResult | None:
    if ctx.action.kind not in _TEXT_KINDS:
        return None
    # `text_length` is in UTF-16 code units, which never undercounts Telegram's
    # own measure; replies are never split (ARCHITECTURE §6.8).
    length = ctx.action.text_length
    return _check(
        "length_ok",
        _first((length < 1, "empty_text"), (length > MAX_TEXT_LENGTH, "too_long")),
    )


def consent_valid(ctx: PolicyContext) -> CheckResult:
    return _check(
        "consent_valid",
        _first((ctx.ai_generated and not ctx.account.managed_consent_ok, "consent_missing")),
    )


def sending_enabled(ctx: PolicyContext) -> CheckResult | None:
    # Execution only (P2-T21): with sending off, drafts still become cards.
    if _execution_only(ctx):
        return None
    return _check("sending_enabled", _first((not ctx.sending_enabled, "sending_disabled")))


def autonomy_not_paused(ctx: PolicyContext) -> CheckResult | None:
    if ctx.action.origin is not ActionOrigin.DELEGATION_AUTO:
        return None
    paused = ctx.account.autonomy_paused or ctx.conversation.autonomy_paused_reason is not None
    return _check("autonomy_not_paused", _first((paused, "autonomy_paused")))


def delegation_active(ctx: PolicyContext) -> CheckResult | None:
    if ctx.action.origin is not ActionOrigin.DELEGATION_AUTO:
        return None
    # P2 has no delegations (`ctx.delegation` is always None), so this always
    # fails; M06 adds DelegationSnapshot and the real coverage check.
    return _check("delegation_active", _first((ctx.delegation is None, "no_delegation")))


#: Evaluation and reporting order (spec §3.1).
RULES: tuple[Rule, ...] = (
    account_active,
    connection_active,
    conversation_owned,
    conversation_available,
    window_open,
    no_takeover_since,
    not_stale,
    not_already_executed,
    not_expired,
    inbox_drained,
    target_is_assistant_sent,
    length_ok,
    consent_valid,
    sending_enabled,
    autonomy_not_paused,
    delegation_active,
)
