"""`decide`: the application's verdict on an owner-voice action (ARCHITECTURE §9.2).

The model proposes; this decides (AGENTS §6 invariant 1). `decide` is total and
pure: for any context built from valid enum values it returns a `Decision`, and
it reads nothing but its argument.
"""

from assistant.actions.states import ActionState, StateReason
from assistant.policy.context import Decision, Outcome, Phase, PolicyContext
from assistant.policy.rules import RULES

_WAIT_RULE = "inbox_drained"

type ExecutionTarget = tuple[ActionState, StateReason]

#: Execution failure -> state (S04.1 spec §3.2), first matching row wins. A row
#: with `None` means "leave the action as it is". The order puts the facts the
#: owner can act on first: someone else already holds the action, then the chat
#: moved on (outdated), then time ran out (expired), then a named block.
_EXECUTION_TARGETS: tuple[tuple[frozenset[str], ExecutionTarget | None], ...] = (
    (frozenset({"already_claimed"}), None),
    (frozenset({"takeover"}), (ActionState.OUTDATED, StateReason.TAKEOVER)),
    (
        frozenset({"new_contact_message"}),
        (ActionState.OUTDATED, StateReason.NEW_CONTACT_MESSAGE),
    ),
    (frozenset({"source_deleted"}), (ActionState.OUTDATED, StateReason.SOURCE_DELETED)),
    (frozenset({"window_closed"}), (ActionState.EXPIRED, StateReason.WINDOW_CLOSED)),
    (frozenset({"max_age"}), (ActionState.EXPIRED, StateReason.MAX_AGE)),
    (frozenset({"consent_missing"}), (ActionState.BLOCKED, StateReason.CONSENT_REVOKED)),
    (frozenset({"sending_disabled"}), (ActionState.BLOCKED, StateReason.SENDING_DISABLED)),
    (frozenset({"too_long", "empty_text"}), (ActionState.BLOCKED, StateReason.TOO_LONG)),
    # Only autonomous actions carry this rule (rules.autonomy_not_paused).
    (frozenset({"autonomy_paused"}), (ActionState.CANCELLED, StateReason.AUTONOMY_PAUSED)),
)
_OTHERWISE: ExecutionTarget = (ActionState.BLOCKED, StateReason.POLICY_DENIED)


def decide(ctx: PolicyContext) -> Decision:
    """Evaluate every applicable rule, in order, and derive the outcome.

    Proposal phase: `REQUIRE_APPROVAL` when everything passes, else `DENY`
    (never `ALLOW_AUTO` in P2). Execution phase: `ALLOW_AUTO` means "may send
    now" for every origin — an owner-approved action already carries its
    approval; `WAIT` only when `inbox_drained` is the sole failure, because a
    pending update cannot make another failed check pass and not sending is
    always safe; anything else `DENY`.
    """
    checks = tuple(check for rule in RULES if (check := rule(ctx)) is not None)
    failed = [check for check in checks if not check.passed]
    if ctx.phase is Phase.PROPOSAL:
        outcome = Outcome.DENY if failed else Outcome.REQUIRE_APPROVAL
    elif not failed:
        outcome = Outcome.ALLOW_AUTO
    elif all(check.name == _WAIT_RULE for check in failed):
        outcome = Outcome.WAIT
    else:
        outcome = Outcome.DENY
    return Decision(outcome, checks)


def execution_target(decision: Decision) -> ExecutionTarget | None:
    """The state and reason Tx A moves an approved action to after a failed check.

    For execution-phase decisions (S04.4 applies it). Returns `None` when the
    action must stay as it is: the decision allows sending (`ALLOW_AUTO`), it
    only waits for ingestion (`WAIT`: the job re-defers), or another worker
    already claimed the action (`already_claimed`, whatever else failed). Callers
    tell these apart by `decision.outcome`. Otherwise the first matching row of
    the spec §3.2 table wins, and any failure it does not name blocks with
    `policy_denied`; the failed checks themselves are kept in the stored decision.
    """
    if decision.outcome is not Outcome.DENY:
        return None
    failed = {check.reason for check in decision.checks if not check.passed}
    for reasons, target in _EXECUTION_TARGETS:
        if failed & reasons:
            return target
    return _OTHERWISE
