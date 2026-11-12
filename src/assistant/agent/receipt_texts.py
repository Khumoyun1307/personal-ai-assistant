"""Owner-visible wording for the deterministic draft receipt (H7).

When the runtime cannot let the model's own answer stand (an artifact changed,
was never read, or the run did not finish), the final answer is built from the
fresh observation alone. These are plain sentences from the shared control-chat
catalogue (`core/control_texts.json`, keys `receipt_*`), in the owner's locale:
no action ids, versions, enum names or timestamps. The model's context keeps its
own content-free English receipts (`agent/context.py`); this module is only for
what the owner reads.

The contact's display name, as the approval cards already show it in the control
chat, identifies a draft when several are reported at once. It is read only for
that sentence and never reaches logs or the model.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence

from assistant.actions.activity import ActionReceipt
from assistant.agent.locales import SupportedLocale
from assistant.agent.owner_texts import CONTACT_FALLBACK
from assistant.core.control_texts import CATALOGUE, text

_FINAL_WITH_REASON = {
    "cancelled": "receipt_cancelled",
    "expired": "receipt_expired",
    "outdated": "receipt_outdated",
    "failed": "receipt_failed",
    "blocked": "receipt_blocked",
}
_PLAIN = {
    "approved": "receipt_approved",
    "executing": "receipt_executing",
    "succeeded": "receipt_succeeded",
    "delivery_uncertain": "receipt_uncertain",
    "rejected": "receipt_rejected",
}


def _reason(code: str | None, locale: SupportedLocale) -> str:
    """` <plain reason>` when the catalogue has one for the code, else nothing."""
    key = f"receipt_reason_{code}"
    return " " + text(key, locale) if code is not None and key in CATALOGUE else ""


def _awaiting_key(receipt: ActionReceipt) -> str:
    shown_now = (
        receipt.presentation == "shown"
        and receipt.presented_version == receipt.version
        and receipt.presented_state == receipt.state
    )
    if shown_now:
        return "receipt_awaiting_shown"
    if receipt.presentation == "uncertain":
        return "receipt_awaiting_uncertain"
    if receipt.presentation == "not_delivered":
        return "receipt_awaiting_not_delivered"
    return "receipt_awaiting_pending"


def receipt_line(receipt: ActionReceipt, locale: SupportedLocale, name: str | None = None) -> str:
    """One plain sentence naming the exact current state of one draft.

    `name` is given only when several drafts are reported; an empty string
    means none was available, and the fixed fallback word is used.
    """
    who = "" if name is None else f"{html.escape(name, quote=False) or CONTACT_FALLBACK[locale]}: "
    state = receipt.state
    reason = receipt.state_reason or receipt.warning_reason
    if state == "awaiting_approval":
        return text(_awaiting_key(receipt), locale, who=who)
    if state == "outdated" and receipt.can_approve_anyway:
        return text("receipt_outdated_warned", locale, who=who, reason=_reason(reason, locale))
    if state in _PLAIN:
        return text(_PLAIN[state], locale, who=who, reason=_reason(reason, locale))
    if state in _FINAL_WITH_REASON:
        return text(_FINAL_WITH_REASON[state], locale, who=who, reason=_reason(reason, locale))
    return text("receipt_not_final", locale, who=who)


def receipt_lines(
    receipts: Sequence[ActionReceipt],
    locale: SupportedLocale,
    names: Mapping[int, str | None],
) -> list[str]:
    several = len(receipts) > 1
    return [
        receipt_line(r, locale, (names.get(r.conversation_id) or "") if several else None)
        for r in receipts
    ]


def no_action_text(result: str, reason: str | None, locale: SupportedLocale) -> str:
    """The real question or failure of a tool result that stored no artifact."""
    if result == "needs_input":
        key = f"receipt_needs_{reason}"
        return text(key if key in CATALOGUE else "receipt_needs_more", locale)
    if result in {"proposal_conflict", "idempotency_conflict", "existing_draft_conflict"}:
        return text("receipt_conflict", locale)
    key = f"receipt_detail_{reason}"
    detail = text(key if key in CATALOGUE else "receipt_detail_default", locale)
    return text("receipt_not_applied", locale, detail=detail)
