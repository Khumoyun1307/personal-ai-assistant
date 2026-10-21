"""Error vocabulary shared across layers.

`TransientError` lets an adapter tell a domain loop "try this again later"
without the domain importing the adapter's exception types (§5.2). The inbox
treats it like a database blip: the row stays pending and no attempt is
counted (ARCHITECTURE §6.2).
"""

from __future__ import annotations


class TransientError(Exception):
    """A failure that says nothing about the work itself: network, 5xx, 429.

    Raised by adapters; never counted toward dead-lettering.
    """
