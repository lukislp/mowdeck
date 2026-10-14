"""Relationship tuples: ``<object>#<relation>@<subject>``, optionally with a condition."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Self

from mowdeck_authz.refs import ObjectRef, Subject

ConditionValue = str | int | float | bool


@dataclass(frozen=True, slots=True)
class TupleCondition:
    """A named condition of the model with the parameters stored on the tuple."""

    name: str
    context: tuple[tuple[str, ConditionValue], ...] = ()

    @classmethod
    def of(cls, name: str, context: Mapping[str, ConditionValue] | None = None) -> Self:
        """Build from a mapping (stored sorted, so equal contexts compare equal)."""
        return cls(name, tuple(sorted((context or {}).items())))

    def context_dict(self) -> dict[str, ConditionValue]:
        """The stored parameters as a dict."""
        return dict(self.context)


def rfc3339(moment: datetime) -> str:
    """Format a timezone-aware datetime as RFC 3339 in UTC (``2026-10-09T12:00:00Z``)."""
    if moment.tzinfo is None:
        msg = "timestamps must be timezone aware"
        raise ValueError(msg)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def not_expired(expires_at: datetime) -> TupleCondition:
    """Condition ``not_expired`` of the model, used for time-limited support access."""
    return TupleCondition.of("not_expired", {"expires_at": rfc3339(expires_at)})


@dataclass(frozen=True, slots=True)
class RelationshipTuple:
    """One tuple. Its identity in OpenFGA is ``(subject, relation, object)``; see :meth:`key`."""

    subject: Subject
    relation: str
    object: ObjectRef
    condition: TupleCondition | None = field(default=None, compare=True)

    def __str__(self) -> str:
        text = f"{self.object}#{self.relation}@{self.subject}"
        return f"{text} (with {self.condition.name})" if self.condition else text

    def key(self) -> tuple[str, str, str]:
        """``(subject, relation, object)`` as strings: the identity OpenFGA uses."""
        return (str(self.subject), self.relation, str(self.object))

    @classmethod
    def parse(
        cls, subject: str, relation: str, obj: str, condition: TupleCondition | None = None
    ) -> Self:
        """Build from the string forms used by OpenFGA."""
        return cls(Subject.parse(subject), relation, ObjectRef.parse(obj), condition)

    def condition_context(self) -> dict[str, Any] | None:
        """The condition parameters, or None without a condition."""
        return self.condition.context_dict() if self.condition else None
