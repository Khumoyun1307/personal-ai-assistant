"""Error vocabulary shared across layers.

`TransientError` lets an adapter tell a domain loop "try this again later"
without the domain importing the adapter's exception types (§5.2). The inbox
treats it like a database blip: the row stays pending and no attempt is
counted (ARCHITECTURE §6.2).

`NotInAccountError` is the one way to say "this id is not the account's": the
agent's tool executor answers it "not found" and treats any other
`LookupError` (a `KeyError`, an `IndexError`, a missing time zone) as the
bug it is (ARCHITECTURE §8.3, M03 fix round C2).
"""

from __future__ import annotations


class TransientError(Exception):
    """A failure that says nothing about the work itself: network, 5xx, 429.

    Raised by adapters; never counted toward dead-lettering.
    """


class NotInAccountError(LookupError):
    """An id that is not this account's: unknown, or another account's. One
    answer for both (§6 invariant 12). A `LookupError`, so callers that catch
    that keep working."""
