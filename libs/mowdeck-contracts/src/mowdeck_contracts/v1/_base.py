"""Shared building blocks for all v1 contract models."""

from datetime import UTC, datetime
from typing import Annotated, Final

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, StringConstraints


def _to_utc(value: datetime) -> datetime:
    try:
        return value.astimezone(UTC)
    except OverflowError as exc:  # e.g. 0001-01-01T00:00:00+01:00: before year 1 in UTC
        msg = "timestamp out of range in UTC"
        raise ValueError(msg) from exc


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

PROVIDER_ID_PATTERN: Final = r"^[a-z][a-z0-9_-]{1,31}$"
"""Provider ids: lower case, stable, used as the entry point name and stored with every linked
account (``linked_accounts.provider``). Defined here so ``mower`` and ``error_lexicon`` use it
without importing ``providers`` (which imports ``error_lexicon``)."""


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
