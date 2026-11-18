"""Operator reconciliation of priced admissions (I16): `python -m assistant admissions`.

- `list --account ID`: unresolved and overrun attempts (ids, status, month,
  USD, fixed codes; no content).
- `settle --account ID --id N --input-tokens X --output-tokens Y`: settle a
  `billing_uncertain` (or stuck `dispatched`) attempt with the usage read
  from the provider's usage dashboard, exactly once.
- `review-overrun --account ID --id N`: acknowledge a recorded bound overrun
  so admission resumes; the true charge stays recorded.

No provider or Telegram call; nothing here reads content.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.core.clock import Clock
from assistant.db.uow import transaction
from assistant.llm.admission import CallOutcome, review_bound_overrun, settle_call
from assistant.llm.models import LlmAdmission
from assistant.llm.port import Usage

EXIT_OK = 0
EXIT_REFUSED = 1


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    admissions = sub.add_parser(
        "admissions", help="reconcile priced LLM admissions (I16, ARCHITECTURE §14.3)"
    )
    commands = admissions.add_subparsers(dest="admissions_command", required=True)
    listing = commands.add_parser("list", help="show unresolved and overrun attempts")
    listing.add_argument("--account", type=int, required=True)
    settle = commands.add_parser("settle", help="settle an uncertain attempt with known usage")
    settle.add_argument("--account", type=int, required=True)
    settle.add_argument("--id", type=int, required=True)
    settle.add_argument("--input-tokens", type=int, required=True)
    settle.add_argument("--output-tokens", type=int, required=True)
    review = commands.add_parser("review-overrun", help="acknowledge a bound overrun")
    review.add_argument("--account", type=int, required=True)
    review.add_argument("--id", type=int, required=True)


async def run_admissions(
    factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    args: argparse.Namespace,
    out: Callable[[str], None] = print,
) -> int:
    command = args.admissions_command
    if command == "list":
        async with factory() as session:
            rows = await session.scalars(
                select(LlmAdmission)
                .where(
                    LlmAdmission.account_id == args.account,
                    or_(
                        LlmAdmission.status.in_(("reserved", "dispatched", "billing_uncertain")),
                        LlmAdmission.error_code == "bound_exceeded",
                    ),
                )
                .order_by(LlmAdmission.id)
            )
            for row in rows:
                out(
                    f"{row.id} {row.status} {row.month_start:%Y-%m} reserved={row.reserved_usd} "
                    f"charged={row.charged_usd} code={row.error_code}"
                )
        return EXIT_OK
    if command == "settle" and min(args.input_tokens, args.output_tokens) < 0:
        out("refused: negative usage")
        return EXIT_REFUSED
    async with transaction(factory) as uow:
        if command == "settle":
            target = await uow.session.get(LlmAdmission, args.id)
            if (
                target is None
                or target.account_id != args.account
                or target.status
                not in (
                    "dispatched",
                    "billing_uncertain",
                )
            ):
                out("refused: not an uncertain attempt of this account")
                return EXIT_REFUSED
            changed = await settle_call(
                uow.session,
                account_id=args.account,
                admission_id=args.id,
                outcome=CallOutcome.COMPLETED,
                usage=Usage(input_tokens=args.input_tokens, output_tokens=args.output_tokens),
                actual_model=None,
                now=clock.now(),
                purpose="reconciliation",
            )
        else:
            changed = await review_bound_overrun(
                uow.session, account_id=args.account, admission_id=args.id
            )
    if not changed:
        out("unchanged")
        return EXIT_REFUSED
    out("settled" if command == "settle" else "reviewed")
    return EXIT_OK
