"""Test doubles: an in-memory authorizer that evaluates the real model, and allow/deny stubs.

``InMemoryAuthorizer`` is also a :class:`~mowdeck_authz.writer.TupleBackend`, so code that
writes tuples through a :class:`~mowdeck_authz.writer.TupleWriter` can be tested against it and
the effect checked with the same object::

    authz = InMemoryAuthorizer()
    writer = TupleWriter(authz, owner="identity")
    await writer.write([RelationshipTuple(user(lukas), "owner", account(a))])
    assert await authz.check(user(lukas), "can_view", account(a))

The contract test ``test_authz_fake_matches_model.py`` runs every assertion of
``infra/openfga/model.fga.yaml`` through this fake, so it cannot drift from the model.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from mowdeck_authz.authorizer import CheckItem, CheckLike, Context, as_check_item
from mowdeck_authz.evaluator import ModelEvaluator, TupleIndex
from mowdeck_authz.model import load_model
from mowdeck_authz.refs import ObjectRef, Subject
from mowdeck_authz.tuples import RelationshipTuple, rfc3339

__all__ = ["AllowAll", "DenyAll", "InMemoryAuthorizer", "ManualClock", "StaticAuthorizer"]


class ManualClock:
    """A monotonic clock for cache tests: ``clock()`` returns ``now``; ``advance()`` moves it."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self.now += seconds


class InMemoryAuthorizer:
    """Authorizer and tuple backend over an in-memory tuple set and the packaged model.

    ``now`` supplies ``current_time`` for conditions (like ``OpenFgaAuthorizer``). Writes are
    validated against the model's type restrictions, so a tuple OpenFGA would reject (for example
    ``users.manage`` granted to an API token) raises ``ValueError`` here too. Unknown relations
    in checks raise ``ValueError`` (OpenFGA would answer with a validation error, which the real
    authorizer turns into a deny) so typos surface in tests.
    """

    def __init__(
        self,
        tuples: Iterable[RelationshipTuple] = (),
        *,
        model: Mapping[str, Any] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.evaluator = ModelEvaluator(model if model is not None else load_model())
        self.tuples = TupleIndex()
        self._now = now
        self.checks: list[CheckItem] = []
        """Every check asked, in order (handy for asserting what a handler checked)."""
        for item in tuples:
            self.add(item)

    # --- Tuple management ----------------------------------------------------------------

    def add(self, *items: RelationshipTuple) -> None:
        """Add tuples (validated against the model); duplicates are ignored."""
        for item in items:
            self.evaluator.validate_tuple(item)
            self.tuples.add(item)

    def remove(self, *items: RelationshipTuple) -> None:
        """Remove tuples; missing ones are ignored."""
        for item in items:
            self.tuples.remove(item)

    async def read(
        self,
        obj: ObjectRef | str,
        *,
        subject: Subject | None = None,
        relation: str | None = None,
    ) -> list[RelationshipTuple]:
        """:class:`~mowdeck_authz.writer.TupleBackend` read."""
        return [
            item
            for item in self.tuples
            if (item.object == obj if isinstance(obj, ObjectRef) else item.object.type == obj)
            and (subject is None or item.subject == subject)
            and (relation is None or item.relation == relation)
        ]

    async def write(
        self, writes: Sequence[RelationshipTuple], deletes: Sequence[RelationshipTuple]
    ) -> None:
        """:class:`~mowdeck_authz.writer.TupleBackend` write (all or nothing, idempotent)."""
        for item in writes:
            self.evaluator.validate_tuple(item)
        self.remove(*deletes)
        for item in writes:
            self.tuples.add(item)

    # --- Authorizer -------------------------------------------------------------------------

    def _context(self, context: Context | None) -> dict[str, Any]:
        return {"current_time": rfc3339(self._now()), **(context or {})}

    async def check(
        self,
        subject: Subject,
        relation: str,
        obj: ObjectRef,
        *,
        context: Context | None = None,
    ) -> bool:
        """Evaluate the model."""
        self.checks.append(CheckItem(subject, relation, obj, context))
        return self.evaluator.check(self.tuples, subject, relation, obj, self._context(context))

    async def check_many(self, items: Sequence[CheckLike]) -> list[bool]:
        """Evaluate each item."""
        results = []
        for raw in items:
            item = as_check_item(raw)
            results.append(
                await self.check(item.subject, item.relation, item.object, context=item.context)
            )
        return results

    async def list_objects(
        self,
        subject: Subject,
        relation: str,
        object_type: str,
        *,
        context: Context | None = None,
    ) -> list[str]:
        """Objects with tuples of ``object_type`` for which the check holds."""
        return self.evaluator.list_objects(
            self.tuples, subject, relation, object_type, self._context(context)
        )

    async def ping(self) -> bool:
        """Always ready."""
        return True

    def invalidate(self) -> None:
        """Nothing is cached."""

    def invalidate_subject(self, subject: Subject | str) -> None:
        """Nothing is cached."""


class StaticAuthorizer:
    """Answers every question with the same value (for tests that are not about authz)."""

    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.checks: list[CheckItem] = []

    async def check(
        self,
        subject: Subject,
        relation: str,
        obj: ObjectRef,
        *,
        context: Context | None = None,
    ) -> bool:
        """The fixed answer."""
        self.checks.append(CheckItem(subject, relation, obj, context))
        return self.allowed

    async def check_many(self, items: Sequence[CheckLike]) -> list[bool]:
        """The fixed answer for each item."""
        self.checks.extend(as_check_item(item) for item in items)
        return [self.allowed] * len(items)

    async def list_objects(
        self,
        subject: Subject,
        relation: str,
        object_type: str,
        *,
        context: Context | None = None,
    ) -> list[str]:
        """Always empty: a static stub knows no objects."""
        return []

    async def ping(self) -> bool:
        """Always ready."""
        return True


def AllowAll() -> StaticAuthorizer:  # noqa: N802 - reads like a class at call sites
    """An authorizer that allows everything."""
    return StaticAuthorizer(allowed=True)


def DenyAll() -> StaticAuthorizer:  # noqa: N802 - reads like a class at call sites
    """An authorizer that denies everything."""
    return StaticAuthorizer(allowed=False)
