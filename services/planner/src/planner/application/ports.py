"""Ports: what the use cases need from the outside world, as protocols.

Two stores implement :class:`Store`: PostgreSQL (``adapters.db``) and memory
(``adapters.memory``, tests). The position track comes from Telemetry over the bus
(:class:`TrackSource`). Authorization questions go to ``mowdeck_authz.Authorizer`` directly.
"""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from mowdeck_contracts.v1 import PositionTrackReply, PositionTrackRequest

from planner.domain.models import MowerFacts, Template


class Clock(Protocol):
    """Returns the current time (timezone-aware)."""

    def __call__(self) -> datetime:
        """The current time."""
        ...


class Tx(Protocol):
    """One transaction: committed together or not at all."""

    async def get_facts(self, mower_id: UUID) -> MowerFacts | None:
        """The facts of a mower (any account; callers compare the account)."""
        ...

    async def put_map(
        self, mower_id: UUID, account_id: UUID, observed_at: datetime, value: dict[str, object]
    ) -> bool:
        """Store a map unless a newer one is stored; True if stored."""
        ...

    async def put_capabilities(
        self, mower_id: UUID, account_id: UUID, observed_at: datetime, value: dict[str, object]
    ) -> bool:
        """Store capabilities unless newer ones are stored; True if stored."""
        ...

    async def list_templates(self, account_id: UUID) -> list[Template]:
        """Templates of the account, by name."""
        ...

    async def get_template(self, account_id: UUID, name: str) -> Template | None:
        """One template by name."""
        ...

    async def count_templates(self, account_id: UUID) -> int:
        """Number of templates of the account."""
        ...

    async def put_template(self, template: Template) -> None:
        """Insert or replace the template with this account and name."""
        ...

    async def delete_template(self, account_id: UUID, name: str) -> bool:
        """Delete a template; False if there was none."""
        ...

    async def purge_account(self, account_id: UUID) -> int:
        """Delete the account's templates and the facts of its mowers (demo purge,
        ADR 0076); returns the rows deleted."""
        ...


class Store(Protocol):
    """Opens transactions."""

    def transaction(self) -> AbstractAsyncContextManager[Tx]:
        """A transaction; rolled back when the block raises."""
        ...


class TrackSource(Protocol):
    """The position track of a mower (Telemetry, ``tracks.positions.query``)."""

    async def track(self, request: PositionTrackRequest) -> PositionTrackReply:
        """Ask; raises ``Unavailable`` when nobody answers."""
        ...
