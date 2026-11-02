"""Who is watching an account live (public demo sandboxes, ADR 0076).

In a demo installation Realtime announces on the core NATS subject :data:`ACCOUNT_PRESENCE`
how many live sockets each of its instances holds per account: when the first socket of an
account opens, when the last one closes, and every ``interval_seconds`` for every account that
still has sockets (the keep-alive). The Device-Gateway pauses the simulated mowers of an account
nobody watches and resumes them as soon as a watcher appears, so a sandbox whose visitor left
costs nothing but its database rows.

Core NATS (not a stream): a missed message is healed by the next keep-alive, and nothing of it
needs to survive a restart. Outside demo mode nothing publishes it.
"""

from typing import Final
from uuid import UUID

from pydantic import Field

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime

ACCOUNT_PRESENCE: Final = "presence.accounts"
"""Core NATS subject of :class:`AccountPresence` (Realtime publishes, the Device-Gateway
listens)."""


class AccountWatchers(ContractModel):
    """Live sockets of one account on the announcing Realtime instance."""

    account_id: UUID
    watchers: int = Field(ge=0, description="Open authenticated sockets; 0: the last one closed.")


class AccountPresence(ContractModel):
    """Payload of :data:`ACCOUNT_PRESENCE`: the counts of one Realtime instance."""

    instance: str = Field(min_length=1, max_length=128, description="The announcing instance.")
    accounts: list[AccountWatchers] = Field(max_length=10_000)
    full: bool = Field(
        default=False,
        description=(
            "True for the keep-alive: ``accounts`` lists every watched account of the instance, "
            "so an account missing from it has no watcher there."
        ),
    )
    interval_seconds: float = Field(
        gt=0, description="When the next keep-alive comes at the latest."
    )
    at: UtcDateTime
