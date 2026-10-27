"""F0 fakes preserve explicit targets and after-commit effects."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.db.uow import discard_after_commit, run_after_commit
from tests.fakes.approvals import RecordingApprovalPresenter
from tests.fakes.sender import FakeTelegramSender, network_error


async def test_recording_presenter_waits_for_commit_and_discards_rollback() -> None:
    presenter = RecordingApprovalPresenter()
    session = AsyncSession()
    presenter.present_after_commit(session, 1)
    presenter.sync_after_commit(session, 2)
    assert presenter.presented == presenter.synced == []
    await run_after_commit(session, None)  # type: ignore[arg-type]
    assert presenter.presented == [1]
    assert presenter.synced == [2]
    presenter.present_after_commit(session, 3)
    discard_after_commit(session)
    await run_after_commit(session, None)  # type: ignore[arg-type]
    assert presenter.presented == [1]
    await session.close()


async def test_sender_fake_records_business_context_on_send_edit_and_delete() -> None:
    sender = FakeTelegramSender()
    await sender.send_message(chat_id=7, text="Synthetic", business_connection_id="synthetic")
    await sender.edit_message_text(
        chat_id=7, message_id=8, text="Synthetic", business_connection_id="synthetic"
    )
    await sender.delete_business_messages(business_connection_id="synthetic", message_ids=[8, 9])
    assert sender.sends[0]["business_connection_id"] == "synthetic"
    assert sender.edits[0]["business_connection_id"] == "synthetic"
    assert sender.deletes == [{"business_connection_id": "synthetic", "message_ids": [8, 9]}]
    sender.fail_with = network_error()
    with pytest.raises(type(sender.fail_with)):
        await sender.delete_business_messages(business_connection_id="synthetic", message_ids=[10])
