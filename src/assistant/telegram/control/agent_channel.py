"""`ControlChannel` over `OutboundGateway` (ARCHITECTURE §5.2, §6.7).

The agent reaches the owner only through this adapter, which can only build
`ControlChat` targets — there is no way to name a business chat from here, so
the agent cannot speak as the owner (§6 invariant 2).
"""

from __future__ import annotations

from assistant.agent.ports import Delivery
from assistant.telegram.gateway import ControlChat, OutboundGateway


class GatewayControlChannel:
    def __init__(self, gateway: OutboundGateway) -> None:
        self._gateway = gateway

    async def send(self, *, account_id: int, chat_id: int, text: str) -> Delivery:
        result = await self._gateway.send_text(
            ControlChat(account_id=account_id, chat_id=chat_id), text
        )
        return Delivery(
            ok=result.ok,
            message_id=result.message_id,
            error=None if result.ok else result.error_class.value,
        )

    async def typing(self, *, account_id: int, chat_id: int) -> None:
        await self._gateway.chat_action(ControlChat(account_id=account_id, chat_id=chat_id))
