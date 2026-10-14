"""Ports: what the use cases need from the outside world, as protocols.

Persistence is one generic :class:`Table` per entity inside a :class:`Tx` (a database
transaction). Filters are keyword arguments: plain values compare for equality (``None`` means
``IS NULL``), :class:`Gt`, :class:`Ge`, :class:`Lt` and :class:`In` express the rest. Two stores
implement it: PostgreSQL (``adapters.db``) and memory (``adapters.memory``, tests).
"""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from mowdeck_contracts.v1 import ModuleDescriptor
from pydantic import BaseModel

from identity.domain.models import (
    Account,
    ApiToken,
    AuditEntry,
    CatalogueEntry,
    LoginAttempt,
    Membership,
    Role,
    RoleAssignment,
    Session,
    SigningKeyRecord,
    User,
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
class Ge:
    """Filter: column greater than or equal to ``value``."""

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
        """Insert; returns the item (with a generated id where the store assigns one).

        Raises ``ConflictError`` when a unique constraint is violated.
        """
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
        """Write back a changed row (identified by its primary key)."""
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
    def accounts(self) -> Table[Account]:
        """The accounts table."""
        ...

    @property
    def users(self) -> Table[User]:
        """The users table."""
        ...

    @property
    def memberships(self) -> Table[Membership]:
        """The memberships table."""
        ...

    @property
    def roles(self) -> Table[Role]:
        """The roles table."""
        ...

    @property
    def assignments(self) -> Table[RoleAssignment]:
        """The assignments table."""
        ...

    @property
    def sessions(self) -> Table[Session]:
        """The sessions table."""
        ...

    @property
    def api_tokens(self) -> Table[ApiToken]:
        """The api tokens table."""
        ...

    @property
    def signing_keys(self) -> Table[SigningKeyRecord]:
        """The signing keys table."""
        ...

    @property
    def audit(self) -> Table[AuditEntry]:
        """The audit table."""
        ...

    @property
    def login_attempts(self) -> Table[LoginAttempt]:
        """The login attempts table."""
        ...

    @property
    def catalogue(self) -> Table[CatalogueEntry]:
        """The catalogue table."""
        ...


class Store(Protocol):
    """Opens transactions. Leaving the context commits; an exception rolls back."""

    def transaction(self) -> AbstractAsyncContextManager[Tx]:
        """A new transaction."""
        ...


class PasswordHasher(Protocol):
    """Slow, salted password hashing (Argon2id)."""

    async def hash(self, password: str) -> str:
        """Hash a password."""
        ...

    async def verify(self, password_hash: str, password: str) -> bool:
        """True if ``password`` matches; never raises for a mismatch."""
        ...

    def needs_rehash(self, password_hash: str) -> bool:
        """True if the hash was made with other parameters than the current ones."""
        ...

    async def dummy_verify(self, password: str) -> None:
        """Spend the time of a verification (unknown users must not answer faster)."""
        ...


class KeyCipher(Protocol):
    """Encrypts private signing keys at rest."""

    def encrypt(self, kid: str, raw: bytes) -> bytes:
        """Encrypt ``raw`` bound to ``kid``."""
        ...

    def decrypt(self, kid: str, blob: bytes) -> bytes:
        """Inverse of :meth:`encrypt`; raises on a wrong key or tampering."""
        ...


class EventPublisher(Protocol):
    """Publishes domain events (``event.identity.*``)."""

    async def publish(self, event_type: str, account_id: UUID | None, payload: BaseModel) -> None:
        """Publish one event; failures are logged, never raised."""
        ...


@dataclass(frozen=True, slots=True)
class FetchedDescriptor:
    """A module descriptor fetched from one URL (None if the module did not answer)."""

    url: str
    descriptor: ModuleDescriptor | None


class ModuleSource(Protocol):
    """Fetches the module descriptors of the installation's services."""

    async def fetch(self) -> list[FetchedDescriptor]:
        """One entry per configured module URL."""
        ...
