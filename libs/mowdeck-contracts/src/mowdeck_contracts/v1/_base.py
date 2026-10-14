"""Shared building blocks for all v1 contract models."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, StringConstraints


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDateTime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
"""A timezone-aware timestamp, normalised to UTC. Naive timestamps are rejected."""

DottedKey = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=128),
]
"""Lower-case dotted identifier with at least two segments, e.g. ``mower.control``."""

CorrelationId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9._:\-]{1,128}$"),
]
"""Opaque correlation identifier propagated through all services (header ``X-Correlation-ID``)."""


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


class ContractModel(BaseModel):
    """Base class of every contract model.

    Models are immutable. Unknown fields are ignored (tolerant reader), so a consumer built
    against an older minor revision of ``v1`` keeps working when a producer adds new optional
    fields.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        str_strip_whitespace=False,
        use_enum_values=False,
        validate_default=True,
    )
