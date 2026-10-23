"""Ports: what the use cases need from the outside world, as protocols.

Persistence is one generic :class:`Table` per entity inside a :class:`Tx` (a database
transaction), the same shape as Identity's store. Filters are keyword arguments: plain values
compare for equality (``None`` means ``IS NULL``), :class:`Gt`, :class:`Lt` and :class:`In`
express the rest. Two stores implement it: PostgreSQL (``adapters.db``) and memory
(``adapters.memory``, tests).
"""

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from mowdeck_contracts.v1 import (
    BridgeScanReply,
    BridgeScanRequest,
    CameraSessionReply,
    CameraSessionRequest,
    CloudDiscoverReply,
    CloudDiscoverRequest,
    CommandEnvelope,
    FirmwareCheckReply,
    FirmwareCheckRequest,
    PathPreviewGatewayRequest,
    PathPreviewReply,
    PhotoReply,
    PhotoRequest,
)
from mowdeck_contracts.v1.relay import (
    RelayAttachReply,
    RelayAttachRequest,
    RelayDetachReply,
    RelayDetachRequest,
    RelayRenewReply,
    RelayRenewRequest,
)
from pydantic import BaseModel

from fleet.domain.models import (
    AuditEntry,
    BaseStation,
    CommandRecord,
    DriveSession,
    Group,
    LinkedAccount,
    Mower,
    MowerAreaName,
    MowerGroup,
    MowerLink,
    Site,
)


class Clock(Protocol):
    """Returns the current time (timezone-aware)."""

    def __call__(self) -> datetime:
        """The current time."""
        ...


@dataclass(frozen=True, slots=True)
class Gt:
    """Filter: column greater than ``value``."""

    value: Any


@dataclass(frozen=True, slots=True)
class Lt:
    """Filter: column less than ``value``."""

    value: Any


@dataclass(frozen=True, slots=True)
class In:
    """Filter: column is one of ``values``."""

    values: Sequence[Any]


class Table[T](Protocol):
    """Rows of one entity type."""

    async def add(self, item: T) -> T:
        """Insert; raises ``ConflictError`` when a unique rule is violated."""
        ...

    async def get(self, **where: Any) -> T | None:
        """The single row matching ``where``, or None."""
        ...

    async def find(
        self,
        *,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        **where: Any,
    ) -> list[T]:
        """All rows matching ``where``."""
        ...

    async def update(self, item: T) -> None:
        """Write back a changed row (by primary key); ``ConflictError`` on a unique rule."""
        ...

    async def update_where(self, values: Mapping[str, Any], **where: Any) -> int:
        """Set ``values`` on every row matching ``where``; returns the number changed."""
        ...

    async def delete(self, **where: Any) -> int:
        """Delete matching rows; returns the number deleted."""
        ...

    async def count(self, **where: Any) -> int:
        """Number of matching rows."""
        ...


class Tx(Protocol):
    """One transaction: all tables, committed together or not at all."""

    @property
    def sites(self) -> Table[Site]:
        """Sites."""
        ...

    @property
    def groups(self) -> Table[Group]:
        """Groups."""
        ...

    @property
    def mowers(self) -> Table[Mower]:
        """Mowers."""
        ...

    @property
    def mower_groups(self) -> Table[MowerGroup]:
        """Group memberships of mowers."""
        ...

    @property
    def area_names(self) -> Table[MowerAreaName]:
        """Custom names of map areas (key ``mower_id`` and ``area_id``)."""
        ...

    @property
    def commands(self) -> Table[CommandRecord]:
        """Command records."""
        ...

    @property
    def audit(self) -> Table[AuditEntry]:
        """The append-only audit log."""
        ...

    @property
    def drive_sessions(self) -> Table[DriveSession]:
        """Manual drive sessions."""
        ...

    @property
    def linked_accounts(self) -> Table[LinkedAccount]:
        """Linked vendor cloud accounts."""
        ...

    @property
    def mower_links(self) -> Table[MowerLink]:
        """Additional links of mowers (one row per mower and kind, ADR 0038)."""
        ...

    @property
    def base_stations(self) -> Table[BaseStation]:
        """RTK base stations (ADR 0047)."""
        ...

    async def try_lock(self, key: int) -> bool:
        """Take a lock held until the transaction ends; False if another holder has it."""
        ...


class Store(Protocol):
    """Opens transactions. Leaving the context commits; an exception rolls back."""

    def transaction(self) -> AbstractAsyncContextManager[Tx]:
        """A new transaction."""
        ...


class EventPublisher(Protocol):
    """Publishes Fleet's domain events (``event.mower.*``)."""

    async def publish(self, event_type: str, account_id: UUID, payload: BaseModel) -> None:
        """Publish one event; failures are logged, never raised (the change is committed)."""
        ...


class CommandPublisher(Protocol):
    """Sends commands to the Device-Gateway (``command.mower.<id>``)."""

    async def publish(self, envelope: CommandEnvelope) -> None:
        """Publish with ``msg id = command_id``; raises when the bus cannot take it."""
        ...


class CameraRelay(Protocol):
    """Asks the camera relay for, renews and ends a lease (ADR 0045). Raises the bus errors
    (``NoRespondersError`` when no relay is deployed, ``RequestTimeoutError``,
    ``BusNotConnectedError``)."""

    async def attach(self, request: RelayAttachRequest) -> RelayAttachReply:
        """``relay.camera.attach``."""
        ...

    async def renew(self, request: RelayRenewRequest) -> RelayRenewReply:
        """``relay.camera.renew``."""
        ...

    async def detach(self, request: RelayDetachRequest) -> RelayDetachReply:
        """``relay.camera.detach``."""
        ...


class CameraGateway(Protocol):
    """Asks the Device-Gateway that holds a mower for a live camera session."""

    async def request(self, request: CameraSessionRequest) -> CameraSessionReply:
        """The gateway's reply; raises when no gateway answers in time."""
        ...


class PhotoGateway(Protocol):
    """Asks the Device-Gateway that holds a mower for a still image of its camera."""

    async def request(self, request: PhotoRequest) -> PhotoReply:
        """The gateway's reply; raises when no gateway answers in time."""
        ...


class PathPreviewGateway(Protocol):
    """Asks the Device-Gateway that holds a mower to plan a route without starting."""

    async def request(self, request: PathPreviewGatewayRequest) -> PathPreviewReply:
        """The gateway's reply; raises ``RequestTimeoutError`` when no gateway answers in time
        and the other bus errors when none can be asked."""
        ...


class BridgeScanGateway(Protocol):
    """Asks the Device-Gateway that holds a mower to look for it through a Bluetooth bridge."""

    async def request(self, request: BridgeScanRequest) -> BridgeScanReply:
        """The gateway's reply; raises the bus errors when no gateway answers in time."""
        ...


class FirmwareCheckGateway(Protocol):
    """Asks the Device-Gateway that holds a mower for its firmware state (ADR 0038)."""

    async def request(self, request: FirmwareCheckRequest) -> FirmwareCheckReply:
        """The gateway's reply; raises the bus errors when no gateway answers in time."""
        ...


class CloudDiscovery(Protocol):
    """Asks the Device-Gateway for the devices of a vendor cloud account (ADR 0034)."""

    async def discover(self, request: CloudDiscoverRequest) -> CloudDiscoverReply:
        """The gateway's reply; raises when no gateway answers in time."""
        ...


class SecretCipher(Protocol):
    """Encrypts the passwords of linked accounts at rest."""

    @property
    def enabled(self) -> bool:
        """False when no key is configured (linked accounts are then unavailable)."""
        ...

    def encrypt(self, context: str, value: str) -> bytes:
        """``value`` encrypted and bound to ``context`` (the linked account id)."""
        ...

    def decrypt(self, context: str, blob: bytes) -> str:
        """Inverse of :meth:`encrypt`; ``ValueError`` for a wrong key or tampered data."""
        ...

    def needs_reseal(self, blob: bytes) -> bool:
        """True when ``blob`` was sealed with a previous key (rotation)."""
        ...
