"""Keeping OpenFGA in line with the database (the database is the source of truth).

The unit is the **subject** (``user:<id>`` or ``api_token:<id>``): its desired tuples are
computed from the database (memberships, owner and admin flags, role assignments expanded to
``grant_*`` relations, token grants), its current tuples are read from OpenFGA, and only the
difference is written. That is idempotent and order independent, so the same function serves
every change, the reconcile endpoint, the CLI and the periodic full reconcile.

Consistency (see ``UnitOfWork``): the difference is written **inside** the database transaction,
right before the commit. If OpenFGA cannot be written the transaction rolls back and the request
fails with 503, so nothing is half applied in the normal outage case. The rare case "OpenFGA
written, commit failed" is healed by an immediate reconcile from the committed state, and any
remaining drift by the periodic reconcile.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from uuid import UUID

from mowdeck_authz import (
    GRANT_RELATIONS,
    PLATFORM_OBJECT,
    ObjectRef,
    RelationshipTuple,
    Subject,
    TupleBackend,
    TupleWriter,
    account,
    api_token,
    grant_relation,
    user,
)
from mowdeck_authz.writer import SupportsInvalidate

from identity.application.ports import Clock, Tx
from identity.domain.errors import AuthzUnavailable
from identity.domain.models import (
    AccountStatus,
    LevelType,
    Role,
    Status,
    SubjectType,
)
from identity.domain.permissions import Catalogue

_log = logging.getLogger("identity.sync")

LEVEL_TYPES: tuple[str, ...] = tuple(level.value for level in LevelType)
IDENTITY_ACCOUNT_RELATIONS = frozenset({"owner", "admin", "member"})
"""Relations of a subject on an account that this module manages (``support`` is separate)."""


def subject_of(subject_type: SubjectType, subject_id: UUID) -> Subject:
    """The OpenFGA subject of a user or an API token."""
    return user(subject_id) if subject_type is SubjectType.USER else api_token(subject_id)


def level_object(level_type: LevelType, level_id: UUID) -> ObjectRef:
    """The OpenFGA object of a level."""
    return ObjectRef(level_type.value, str(level_id))


def platform_tuple(account_id: UUID) -> RelationshipTuple:
    """``account:<id>#platform@platform:main`` (operators manage accounts through it)."""
    return RelationshipTuple(PLATFORM_OBJECT.as_subject(), "platform", account(account_id))


@dataclass(slots=True)
class SyncResult:
    """What a reconcile changed."""

    writes: int = 0
    deletes: int = 0
    changed_subjects: set[str] = field(default_factory=set)

    def merge(self, other: "SyncResult") -> None:
        """Add ``other`` to this result."""
        self.writes += other.writes
        self.deletes += other.deletes
        self.changed_subjects |= other.changed_subjects


class AuthzSync:
    """Computes desired tuples from a transaction and writes the difference to OpenFGA."""

    def __init__(
        self,
        backend: TupleBackend,
        clock: Clock,
        *,
        caches: Iterable[SupportsInvalidate] = (),
    ) -> None:
        self.backend = backend
        self.clock = clock
        self.writer = TupleWriter(backend, owner="identity", caches=list(caches))

    # --- desired state -------------------------------------------------------------------

    async def desired(
        self, tx: Tx, subject: Subject, catalogue: Catalogue
    ) -> set[RelationshipTuple]:
        """The tuples ``subject`` should have according to the database."""
        subject_id = UUID(subject.id)
        if subject.type == "user":
            return await self._desired_for_user(tx, subject, subject_id, catalogue)
        if subject.type == "api_token":
            return await self._desired_for_token(tx, subject, subject_id, catalogue)
        msg = f"identity does not manage subjects of type {subject.type}"
        raise ValueError(msg)

    async def _desired_for_user(
        self, tx: Tx, subject: Subject, user_id: UUID, catalogue: Catalogue
    ) -> set[RelationshipTuple]:
        found = await tx.users.get(id=user_id)
        if found is None or found.status is not Status.ACTIVE:
            return set()
        wanted: set[RelationshipTuple] = set()
        roles: dict[UUID, Role | None] = {}
        for membership in await tx.memberships.find(user_id=user_id, status=Status.ACTIVE.value):
            acct = await tx.accounts.get(id=membership.account_id)
            if acct is None:
                continue
            deleting = acct.status is AccountStatus.DELETING
            if deleting and not membership.is_owner:
                continue
            obj = account(acct.id)
            wanted.add(RelationshipTuple(subject, "member", obj))
            if membership.is_owner:
                wanted.add(RelationshipTuple(subject, "owner", obj))
            if deleting:
                continue  # a locked account keeps only its owner, so it can be restored
            if membership.is_admin:
                wanted.add(RelationshipTuple(subject, "admin", obj))
            assignments = await tx.assignments.find(
                account_id=acct.id, subject_type=SubjectType.USER.value, subject_id=user_id
            )
            for assignment in assignments:
                if assignment.role_id not in roles:
                    roles[assignment.role_id] = await tx.roles.get(id=assignment.role_id)
                role = roles[assignment.role_id]
                if role is None or role.is_system:
                    continue
                wanted |= self._grants(
                    subject,
                    role.permissions,
                    assignment.level_type,
                    assignment.level_id,
                    SubjectType.USER,
                    catalogue,
                )
        return wanted

    async def _desired_for_token(
        self, tx: Tx, subject: Subject, token_id: UUID, catalogue: Catalogue
    ) -> set[RelationshipTuple]:
        token = await tx.api_tokens.get(id=token_id)
        if token is None or not token.is_usable(self.clock()):
            return set()
        acct = await tx.accounts.get(id=token.account_id)
        if acct is None or acct.status is not AccountStatus.ACTIVE:
            return set()
        wanted = {RelationshipTuple(subject, "member", account(acct.id))}
        for grant in token.token_grants:
            wanted |= self._grants(
                subject,
                grant.permissions,
                grant.level.type,
                grant.level.id,
                SubjectType.API_TOKEN,
                catalogue,
            )
        return wanted

    @staticmethod
    def _grants(
        subject: Subject,
        keys: Iterable[str],
        level_type: LevelType,
        level_id: UUID,
        subject_type: SubjectType,
        catalogue: Catalogue,
    ) -> set[RelationshipTuple]:
        obj = level_object(level_type, level_id)
        result: set[RelationshipTuple] = set()
        for key in catalogue.effective(keys, level_type, subject_type):
            if catalogue.entry(key).in_model:
                relation = grant_relation(key, level_type.value, subject_type.value)
                result.add(RelationshipTuple(subject, relation, obj))
        return result

    # --- current state and the difference ------------------------------------------------

    async def current(self, subject: Subject) -> set[RelationshipTuple]:
        """The tuples of ``subject`` that Identity manages, as stored in OpenFGA."""
        found: set[RelationshipTuple] = set()
        try:
            for object_type in LEVEL_TYPES:
                for item in await self.backend.read(object_type, subject=subject):
                    if item.relation in GRANT_RELATIONS or (
                        object_type == "account" and item.relation in IDENTITY_ACCOUNT_RELATIONS
                    ):
                        found.add(item)
        except Exception as exc:
            _log.warning("reading tuples failed", extra={"error": type(exc).__name__})
            msg = "the authorization service is unavailable; nothing was changed"
            raise AuthzUnavailable(msg) from exc
        return found

    async def reconcile(
        self,
        tx: Tx,
        subjects: Iterable[Subject],
        catalogue: Catalogue,
        *,
        extra_writes: Iterable[RelationshipTuple] = (),
    ) -> SyncResult:
        """Make OpenFGA match the database for ``subjects``; 503-style error on failure."""
        adds: list[RelationshipTuple] = []
        for item in extra_writes:
            try:
                present = await self.backend.read(
                    item.object, subject=item.subject, relation=item.relation
                )
            except Exception as exc:
                msg = "the authorization service is unavailable; nothing was changed"
                raise AuthzUnavailable(msg) from exc
            if not present:
                adds.append(item)
        deletes: list[RelationshipTuple] = []
        changed: set[str] = set()
        for subject in sorted(set(subjects), key=str):
            wanted = await self.desired(tx, subject, catalogue)
            have = await self.current(subject)
            plus = sorted(wanted - have, key=str)
            minus = sorted(have - wanted, key=str)
            if plus or minus:
                changed.add(str(subject))
            adds.extend(plus)
            deletes.extend(minus)
        if not adds and not deletes:
            return SyncResult()
        try:
            change = await self.writer.write(adds, deletes)
        except ValueError:
            raise
        except Exception as exc:
            _log.warning("writing tuples failed", extra={"error": type(exc).__name__})
            msg = "the authorization service is unavailable; nothing was changed"
            raise AuthzUnavailable(msg) from exc
        return SyncResult(len(change.writes), len(change.deletes), changed)

    async def account_of(self, obj: ObjectRef) -> UUID | None:
        """The account an object of Fleet belongs to (its ``account`` tuple), or None."""
        if obj.type == "account":
            return UUID(obj.id)
        try:
            tuples = await self.backend.read(obj, relation="account")
        except Exception as exc:
            msg = "the authorization service is unavailable"
            raise AuthzUnavailable(msg) from exc
        for item in tuples:
            if item.subject.type == "account":
                return UUID(item.subject.id)
        return None
