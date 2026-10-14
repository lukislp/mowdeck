"""The position track of a mower over core NATS: ``tracks.positions.query``.

Services do not call each other over HTTP. The Planner computes the mowed coverage (and the
gaps to re-mow) from the position track Telemetry stores, so Telemetry answers a
:class:`PositionTrackRequest` with a :class:`PositionTrackReply`. The account comes from the
caller (the Planner takes it from the verified token and has checked ``can_view`` on the mower);
Telemetry filters by it like every query. The track is an encoded polyline of ``(lat, lon)``
with seven decimals (about one centimetre), so tens of thousands of points fit one message.
"""

from typing import Final, Self
from uuid import UUID

from pydantic import Field, model_validator

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime
from mowdeck_contracts.v1.spot import decode_polyline

TRACK_PRECISION: Final = 7
"""Decimal places of the encoded latitude and longitude."""
MAX_TRACK_POINTS: Final = 50_000
"""Most points one reply carries."""


class PositionTrackRequest(ContractModel):
    """Request on ``telemetry.positions.query``: the positions of one mower in a time range."""

    account_id: UUID
    mower_id: UUID
    start: UtcDateTime
    end: UtcDateTime
    limit: int = Field(default=20_000, ge=1, le=MAX_TRACK_POINTS)

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.start >= self.end:
            msg = "start must be before end"
            raise ValueError(msg)
        return self


class PositionTrackReply(ContractModel):
    """Answer: the newest ``limit`` positions of the range, oldest first, or an error."""

    mower_id: UUID
    count: int = Field(default=0, ge=0)
    track: str = Field(
        default="",
        description="Encoded polyline of (lat, lon) with seven decimals, oldest first.",
    )
    first_at: UtcDateTime | None = None
    last_at: UtcDateTime | None = None
    truncated: bool = False
    error: str | None = Field(
        default=None, max_length=500, description="Why no track could be read, if so."
    )

    def points(self) -> list[tuple[float, float]]:
        """The decoded ``(lat, lon)`` pairs."""
        return decode_polyline(self.track, TRACK_PRECISION) if self.track else []
