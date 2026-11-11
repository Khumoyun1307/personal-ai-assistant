"""C2 settlement adapter over H10's application-issued read observations."""

from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions.authorship import is_ai_generated
from assistant.actions.models import Action, ContextFenceRecord
from assistant.actions.proposals import CurrentOwnerFence, UnavailableOwnerFence
from assistant.actions.request_models import TERMINAL_REQUEST_STATES, OwnerRequest
from assistant.conversations.fences import (
    ContextFence,
    check_fence,
    lock_fence_conversations,
    observe_conversations,
)
from assistant.conversations.models import Conversation, Message


class StoredOwnerProposalFence:
    """Bind read-tool observations to the selected request and exact version.

    The caller holds the account lock. All reads/settlement are short local
    transactions, in account → sorted conversations → request → action order.
    Source changes are warnings; an unissued or unrelated proof is no authority.
    """

    async def check_fence(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        run_id: int,
        fence_id: int | None,
        request_id: int,
        expected_request_version: int,
        conversation_id: int,
        requested_at: datetime,
    ) -> CurrentOwnerFence | UnavailableOwnerFence:
        run = await session.scalar(
            text(
                "SELECT id FROM app.agent_runs WHERE account_id = :account_id AND id = :run_id "
                "AND NOT sources_invalidated"
            ),
            {"account_id": account_id, "run_id": run_id},
        )
        if run is None:
            return UnavailableOwnerFence("not_found")
        records = list(
            await session.scalars(
                select(ContextFenceRecord)
                .where(
                    ContextFenceRecord.account_id == account_id,
                    ContextFenceRecord.run_id == run_id,
                )
                .order_by(ContextFenceRecord.id)
            )
        )
        selected = next((row for row in records if row.id == fence_id), None)
        if fence_id is not None and selected is None:
            return UnavailableOwnerFence("not_found")
        try:
            snapshots = [ContextFence.model_validate(row.facts) for row in records]
        except ValidationError:
            return UnavailableOwnerFence("not_found")
        if selected is not None:
            selected_facts = ContextFence.model_validate(selected.facts)
            if (
                selected_facts.request_id not in (None, request_id)
                or (
                    selected_facts.request_id is not None
                    and (
                        selected_facts.request_version != expected_request_version
                        or selected_facts.requested_at != requested_at
                    )
                )
                or conversation_id
                not in {item.conversation_id for item in selected_facts.conversations}
            ):
                return UnavailableOwnerFence("not_found")
        # Never let choosing an earlier opaque id drop a later read dependency.
        # Keep contradictory observations: settlement must warn on mixed reads.
        observations = list(dict.fromkeys(item for f in snapshots for item in f.conversations))
        missing_prior_proof = False
        if fence_id is None:
            # A literal append can retain generated text from an earlier run.
            # Preserve that artifact's dependencies before taking any locks.
            prior = await session.scalar(
                select(ContextFenceRecord)
                .join(Action, Action.context_fence_id == ContextFenceRecord.id)
                .join(OwnerRequest, OwnerRequest.action_id == Action.id)
                .where(
                    OwnerRequest.account_id == account_id,
                    OwnerRequest.id == request_id,
                    Action.account_id == account_id,
                    ContextFenceRecord.account_id == account_id,
                )
            )
            if prior is not None:
                try:
                    inherited = ContextFence.model_validate(prior.facts)
                except ValidationError:
                    return UnavailableOwnerFence("not_found")
                observations = list(dict.fromkeys([*observations, *inherited.conversations]))
            else:
                previous = await session.scalar(
                    select(Action)
                    .join(OwnerRequest, OwnerRequest.action_id == Action.id)
                    .where(
                        OwnerRequest.account_id == account_id,
                        OwnerRequest.id == request_id,
                        Action.account_id == account_id,
                    )
                )
                missing_prior_proof = previous is not None and is_ai_generated(
                    previous.authorship, version=previous.version
                )
        proof = ContextFenceRecord(
            account_id=account_id,
            run_id=run_id,
            facts=ContextFence(
                request_id=request_id,
                request_version=expected_request_version,
                requested_at=requested_at,
                conversations=observations,
            ).model_dump(mode="json"),
            created_at=min((row.created_at for row in records), default=requested_at),
        )
        session.add(proof)
        await session.flush()
        await lock_fence_conversations(
            session,
            account_id=account_id,
            conversation_id=conversation_id,
            fence_id=proof.id,
        )
        request = await session.scalar(
            select(OwnerRequest)
            .where(OwnerRequest.account_id == account_id, OwnerRequest.id == request_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            request is None
            or request.version != expected_request_version
            or request.conversation_id != conversation_id
            or request.requested_at != requested_at
            or request.state in TERMINAL_REQUEST_STATES
        ):
            return UnavailableOwnerFence("not_found")
        if fence_id is None:
            # Literal edits can request a source-only snapshot. No generated
            # proposal/rewrite accepts NULL (enforced by the strict C2 API).
            reads = await observe_conversations(
                session, account_id=account_id, conversation_ids=(conversation_id,)
            )
            observations = list(dict.fromkeys([*observations, *reads]))
            proof.facts = {
                **proof.facts,
                "conversations": [item.model_dump(mode="json") for item in observations],
            }
        checked = await check_fence(
            session,
            account_id=account_id,
            run_id=run_id,
            fence_id=proof.id,
            request_id=request_id,
            expected_request_version=expected_request_version,
            required_conversation_id=conversation_id,
        )
        if checked.reason in {"request_changed", "source_unavailable"}:
            return UnavailableOwnerFence("not_found")
        recipient = [item for item in observations if item.conversation_id == conversation_id]
        watermark = min((item.shown_through_message_id or 0 for item in recipient), default=0)
        if request.target_message_id is not None:
            target = await session.scalar(
                select(Message).where(
                    Message.account_id == account_id,
                    Message.conversation_id == conversation_id,
                    Message.id == request.target_message_id,
                    Message.direction == "contact",
                )
            )
            if target is None or target.purged_at is not None or target.id > watermark:
                return UnavailableOwnerFence("not_found")
            if target.deleted_at is not None:
                revision = await session.scalar(
                    select(Conversation.content_revision).where(
                        Conversation.account_id == account_id, Conversation.id == conversation_id
                    )
                )
                if target.deleted_at < proof.created_at or all(
                    item.content_revision == revision for item in recipient
                ):
                    return UnavailableOwnerFence("not_found")
        warning = checked.reason
        if warning is None and missing_prior_proof:
            warning = "source_unavailable"
        return CurrentOwnerFence(proof.id, watermark or None, warning)

    async def bind_action(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        action_id: int,
        version: int,
        fence: CurrentOwnerFence,
        request_id: int,
        requested_at: datetime,
    ) -> None:
        record = await session.scalar(
            select(ContextFenceRecord).where(
                ContextFenceRecord.account_id == account_id, ContextFenceRecord.id == fence.fence_id
            )
        )
        request = await session.scalar(
            select(OwnerRequest).where(
                OwnerRequest.account_id == account_id, OwnerRequest.id == request_id
            )
        )
        action = await session.scalar(
            select(Action).where(
                Action.account_id == account_id, Action.id == action_id, Action.version == version
            )
        )
        if record is None or request is None or action is None:
            raise ValueError("owner fence binding unavailable")
        facts = ContextFence.model_validate(record.facts)
        if (
            record.run_id is None
            or facts.request_id != request_id
            or facts.request_version != request.version
            or facts.requested_at != requested_at
            or action.conversation_id != request.conversation_id
        ):
            raise ValueError("owner fence binding changed")
        # _link advances the request once in this same transaction. Preserve
        # immutable read counters while binding the post-settlement identity.
        bound = ContextFenceRecord(
            account_id=account_id,
            run_id=record.run_id,
            facts=facts.model_copy(update={"request_version": request.version + 1}).model_dump(
                mode="json"
            ),
            created_at=record.created_at,
        )
        session.add(bound)
        await session.flush()
        action.context_fence_id = bound.id
        action.owner_request_id = request_id
        action.requested_at = requested_at
        action.draft_run_id = record.run_id
        action.freshness_warning = fence.warning
        action.freshness_override = False
