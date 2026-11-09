"""Deterministic privacy context shared by every consent entry point."""

from assistant.accounts import service
from assistant.accounts.models import Account

#: Translation review is part of the stabilization wording unit (I22).
RESTORE_PRIVACY_WARNING = (
    "AI processing was blocked after a restore. Deletions and consent revocations "
    "made after the backup time may have been undone. Before agreeing again, "
    "redo deletions in /settings → Delete data (or /privacy delete for everything), "
    "and redo consent revocations with /privacy revoke. "
    "Use /start to renew consent for this control chat, then /privacy managed "
    "only if you want AI to read connected chats again."
)


def render_consent_prompt(account: Account, prompt: str) -> str:
    """Repeat the recovery warning while any restored scope is quarantined.

    The restore notice can fail delivery, so each question must explain
    undone privacy decisions before offering a way to renew consent.
    """
    if service.restore_consent_pending(account):
        return RESTORE_PRIVACY_WARNING + "\n\n" + prompt
    return prompt
