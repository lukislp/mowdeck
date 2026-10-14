"""Writing tuples: only Identity and Fleet do this.

Kept apart from :class:`~mowdeck_authz.authorizer.Authorizer` on purpose: services that only
ask questions never import a write path.

Tuple ownership (design document, section 1.4):

* ``identity``: ``account#owner``, ``#admin``, ``#member``, ``#support``, ``#platform``;
  every ``grant_*`` on any level; ``platform#operator_admin``.
* ``fleet``: ``site#account``; ``group#account``, ``#parent``; ``mower#account``, ``#parent``.

The ownership check is a guard against mistakes, not a security boundary: OpenFGA pre-shared
keys are all equivalent.

Writes are idempotent (``on_duplicate: ignore``, ``on_missing: ignore``) and every successful
change is passed to the ``audit`` hook, so callers can write the audit log the plan requires.
"""

import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from openfga_sdk import ReadRequestTupleKey
from openfga_sdk.client.models import ClientWriteRequest
from openfga_sdk.client.models.write_conflict_opts import (
    ClientWriteRequestOnDuplicateWrites,
    ClientWriteRequestOnMissingDeletes,
    ConflictOptions,
)

from mowdeck_authz._client import from_tuple_key, to_client_tuple
from mowdeck_authz.errors import TupleOwnershipError
from mowdeck_authz.openfga import FgaConnection
from mowdeck_authz.permissions import GRANT_RELATIONS, grant_relation
from mowdeck_authz.refs import ACCOUNT, GROUP, MOWER, PLATFORM, SITE, ObjectRef, Subject
from mowdeck_authz.tuples import RelationshipTuple

_log = logging.getLogger("mowdeck_authz.writer")

TupleOwner = Literal["identity", "fleet"]

_IDENTITY_ACCOUNT_RELATIONS = frozenset({"owner", "admin", "member", "support", "platform"})
_FLEET_RELATIONS: dict[str, frozenset[str]] = {
    SITE: frozenset({"account"}),
    GROUP: frozenset({"account", "parent"}),
    MOWER: frozenset({"account", "parent"}),
}
_GRANT_LEVELS = frozenset({ACCOUNT, SITE, GROUP, MOWER})

MAX_TUPLES_PER_WRITE = 100
"""OpenFGA's default ``OPENFGA_MAX_TUPLES_PER_WRITE``; larger changes are split."""


def tuple_owner(object_type: str, relation: str) -> TupleOwner | None:
    """Which service owns tuples ``<object_type>#<relation>`` (None if nobody may write them)."""
    if object_type == ACCOUNT and relation in _IDENTITY_ACCOUNT_RELATIONS:
        return "identity"
    if object_type in _GRANT_LEVELS and relation in GRANT_RELATIONS:
        return "identity"
    if object_type == PLATFORM and relation == "operator_admin":
        return "identity"
    if relation in _FLEET_RELATIONS.get(object_type, frozenset()):
        return "fleet"
    return None


@dataclass(frozen=True, slots=True)
class TupleChange:
    """A successful change, as passed to the audit hook."""

    writes: tuple[RelationshipTuple, ...]
    deletes: tuple[RelationshipTuple, ...]
    owner: TupleOwner | None
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_empty(self) -> bool:
        """True if nothing was written or deleted."""
        return not self.writes and not self.deletes


AuditHook = Callable[[TupleChange], Awaitable[None] | None]
"""Called after every successful, non-empty change. Exceptions propagate to the caller."""


class TupleBackend(Protocol):
    """Where tuples live: OpenFGA, or the in-memory fake in tests."""

    async def read(
        self,
        obj: ObjectRef | str,
        *,
        subject: Subject | None = None,
        relation: str | None = None,
    ) -> list[RelationshipTuple]:
        """Tuples on ``obj`` (an object, or a type name for all objects of that type)."""
        ...

    async def write(
        self, writes: Sequence[RelationshipTuple], deletes: Sequence[RelationshipTuple]
    ) -> None:
        """Apply in one transaction; existing writes and missing deletes are ignored."""
        ...


class SupportsInvalidate(Protocol):
    """A decision cache to clear after a change (``OpenFgaAuthorizer`` is one)."""

    def invalidate(self) -> None:
        """Drop everything."""
        ...

    def invalidate_subject(self, subject: Subject | str) -> None:
        """Drop the entries of one subject."""
        ...


_IGNORE_CONFLICTS = ConflictOptions(
    on_duplicate_writes=ClientWriteRequestOnDuplicateWrites.IGNORE,
    on_missing_deletes=ClientWriteRequestOnMissingDeletes.IGNORE,
)


class OpenFgaTupleBackend:
    """:class:`TupleBackend` on OpenFGA, sharing a :class:`FgaConnection`."""

    def __init__(self, connection: FgaConnection) -> None:
        self.connection = connection

    async def read(
        self,
        obj: ObjectRef | str,
        *,
        subject: Subject | None = None,
        relation: str | None = None,
    ) -> list[RelationshipTuple]:
        """Read all pages of matching tuples."""
        await self.connection.connect()
        client = self.connection.client
        key = ReadRequestTupleKey(  # type: ignore[no-untyped-call]
            user=str(subject) if subject is not None else None,
            relation=relation,
            object=str(obj) if isinstance(obj, ObjectRef) else f"{obj}:",
        )
        found: list[RelationshipTuple] = []
        token: str | None = None
        while True:
            options: dict[str, Any] = {"page_size": 100}
            if token:
                options["continuation_token"] = token
            response = await client.read(key, options)
            found.extend(from_tuple_key(item.key) for item in response.tuples or [])
            token = response.continuation_token or None
            if not token:
                return found

    async def write(
        self, writes: Sequence[RelationshipTuple], deletes: Sequence[RelationshipTuple]
    ) -> None:
        """One transactional ``Write`` call with conflicts ignored."""
        await self.connection.connect()
        request = ClientWriteRequest(
            writes=[to_client_tuple(item) for item in writes] or None,
            deletes=[to_client_tuple(item) for item in deletes] or None,
        )
        await self.connection.client.write(request, {"conflict": _IGNORE_CONFLICTS})


def _unique(items: Iterable[RelationshipTuple]) -> list[RelationshipTuple]:
    seen: dict[tuple[str, str, str], RelationshipTuple] = {}
    for item in items:
        seen.setdefault(item.key(), item)
    return list(seen.values())


class TupleWriter:
    """Writes tuples for one owning service, keeps caches fresh and reports every change."""

    def __init__(
        self,
        backend: TupleBackend,
        *,
        owner: TupleOwner | None = None,
        audit: AuditHook | None = None,
        caches: Sequence[SupportsInvalidate] = (),
        max_per_write: int = MAX_TUPLES_PER_WRITE,
    ) -> None:
        """``owner=None`` disables the ownership guard (tests and the bootstrap only)."""
        self.backend = backend
        self.owner = owner
        self.audit = audit
        self.caches = list(caches)
        self.max_per_write = max_per_write

    def _check_owner(self, items: Iterable[RelationshipTuple]) -> None:
        if self.owner is None:
            return
        for item in items:
            owner = tuple_owner(item.object.type, item.relation)
            if owner != self.owner:
                msg = f"{self.owner} may not write {item} (owner: {owner or 'nobody'})"
                raise TupleOwnershipError(msg)

    async def write(
        self,
        adds: Iterable[RelationshipTuple] = (),
        deletes: Iterable[RelationshipTuple] = (),
    ) -> TupleChange:
        """Write ``adds`` and delete ``deletes``; idempotent.

        A tuple that appears in both is deleted first and written again (this is how a
        condition is changed). Large changes are split into transactions of
        ``max_per_write`` tuples; if a later chunk fails, earlier chunks stay applied and the
        exception propagates (the caller retries; writes are idempotent).
        """
        writes = _unique(adds)
        removals = _unique(deletes)
        self._check_owner([*writes, *removals])
        change = TupleChange(tuple(writes), tuple(removals), self.owner)
        if change.is_empty:
            return change
        rewritten = {item.key() for item in writes} & {item.key() for item in removals}
        if rewritten:
            await self._apply([], removals)
            await self._apply(writes, [])
        else:
            await self._apply(writes, removals)
        self._invalidate(change)
        _log.info(
            "authorization tuples changed",
            extra={"owner": self.owner, "writes": len(writes), "deletes": len(removals)},
        )
        if self.audit is not None:
            result = self.audit(change)
            if inspect.isawaitable(result):
                await result
        return change

    async def _apply(
        self, writes: Sequence[RelationshipTuple], deletes: Sequence[RelationshipTuple]
    ) -> None:
        operations = [(True, item) for item in writes] + [(False, item) for item in deletes]
        for start in range(0, len(operations), self.max_per_write):
            chunk = operations[start : start + self.max_per_write]
            await self.backend.write(
                [item for is_write, item in chunk if is_write],
                [item for is_write, item in chunk if not is_write],
            )

    def _invalidate(self, change: TupleChange) -> None:
        subjects = {item.subject for item in (*change.writes, *change.deletes)}
        hierarchy = any(s.type not in ("user", "api_token") for s in subjects)
        for cache in self.caches:
            if hierarchy:
                cache.invalidate()
            else:
                for subject in subjects:
                    cache.invalidate_subject(subject)

    async def replace_subject_grants(
        self, subject: Subject, obj: ObjectRef, permissions: Iterable[str]
    ) -> TupleChange:
        """Make the ``grant_*`` tuples of ``subject`` on ``obj`` match ``permissions`` exactly.

        ``permissions`` are permission keys (``mower.control``); they are mapped to grant
        relations and validated for the level and the subject type (``ValueError`` otherwise).
        Reads the current grants and writes only the difference. Other relations of the subject
        on the object (``member``, ``owner``) are left alone.
        """
        wanted = {grant_relation(key, obj.type, subject.type) for key in permissions}
        current = {
            item.relation
            for item in await self.backend.read(obj, subject=subject)
            if item.relation in GRANT_RELATIONS
        }
        return await self.write(
            adds=[RelationshipTuple(subject, rel, obj) for rel in sorted(wanted - current)],
            deletes=[RelationshipTuple(subject, rel, obj) for rel in sorted(current - wanted)],
        )
