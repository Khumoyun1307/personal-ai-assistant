"""Ports the agent needs from adapters (ARCHITECTURE §5.2).

The agent may not import `telegram/` (import-linter contract). It reaches the
owner's control chat through `ControlChannel`, defined in the shared kernel
(`core/channel.py`) because the domain alert service needs it too.
"""

from __future__ import annotations

from assistant.core.channel import ControlChannel, Delivery

__all__ = ["ControlChannel", "Delivery"]
