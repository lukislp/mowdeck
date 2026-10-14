"""The mower's own computed mow path: the route it planned for the chosen areas and pattern.

Mowers that plan their route on board (Mammotion LUBA) can hand the planned route back as a
list of polylines in the map's local metres (the frame of :class:`~mowdeck_contracts.v1.MowerMap`).
MowDeck shows it on the map in two ways:

* **while mowing**: the Device-Gateway publishes the path of the running job as the device
  snapshot ``device.mower.<id>.path`` (:class:`~mowdeck_contracts.v1.MowerPathSnapshot`) with the
  mower's progress along it, and clears it (``path = None``) when the job ended;
* **as a preview** before a start: ``POST /api/v1/fleet/mowers/{id}/path-preview`` asks the
  mower to plan the route without starting it (:class:`PathPreviewRequest` through Fleet to
  the gateway as :class:`PathPreviewGatewayRequest`, answered with :class:`PathPreviewReply`).

Ids of the vendor (line and route hashes) are 64-bit numbers; they travel as strings so
JavaScript clients do not round them.
"""

from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime
from mowdeck_contracts.v1.maps import LocalPoint
from mowdeck_contracts.v1.principal import PrincipalRef

MowPathKind = Literal["mow", "edge", "transfer", "other"]
"""What a polyline of a path is: fill stripes (``mow``), laps along the area edge (``edge``),
driving between areas with blades off (``transfer``) or a part the adapter cannot tell."""

MAX_PATH_POINTS = 50_000
"""Most points a path may carry in total (a large garden plans a few thousand)."""

Polyline = Annotated[list[LocalPoint], Field(min_length=1)]
"""Points of one polyline in local metres, in driving order."""


class MowPathProgress(ContractModel):
    """Where the mower is along its path."""

    index: int = Field(
        ge=0,
        description="Points done so far, counted over all polylines in order: the points with "
        "a flat index below this value are mowed.",
    )
    direction: int = Field(
        default=0, ge=0, le=255, description="The vendor's traversal direction flag (0 or 1)."
    )


class MowPath(ContractModel):
    """The route a mower planned, in the local metres of its map."""

    path_hash: str = Field(
        min_length=1, max_length=32, description="The vendor's id of the planned route."
    )
    polylines: list[Polyline] = Field(
        default_factory=list, description="The route as polylines in driving order."
    )
    kinds: list[MowPathKind] | None = Field(
        default=None, description="The kind of every polyline (same length), when known."
    )
    points_total: int = Field(ge=0, description="Number of points over all polylines.")
    progress: MowPathProgress | None = Field(
        default=None, description="Progress of the running job; None for a preview."
    )
    area_ids: list[str] = Field(
        default_factory=list, description="The map areas the route covers, in order."
    )
    planned_at: UtcDateTime = Field(description="When MowDeck received the route (UTC).")

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        total = sum(len(line) for line in self.polylines)
        if total != self.points_total:
            msg = f"points_total is {self.points_total} but the polylines hold {total} points"
            raise ValueError(msg)
        if total > MAX_PATH_POINTS:
            msg = f"a path holds at most {MAX_PATH_POINTS} points"
            raise ValueError(msg)
        if self.kinds is not None and len(self.kinds) != len(self.polylines):
            msg = "kinds must name every polyline"
            raise ValueError(msg)
        if self.progress is not None and self.progress.index > total:
            msg = "progress.index is beyond the last point"
            raise ValueError(msg)
        return self


class PathPreviewRequest(ContractModel):
    """Body of ``POST /api/v1/fleet/mowers/{id}/path-preview``: the areas and pattern of a
    start whose route the mower should plan without starting."""

    area_ids: list[str] = Field(min_length=1, max_length=64, description="Map area ids, in order.")
    pattern: dict[str, Any] | None = Field(
        default=None, description="Pattern settings as for `StartMow.pattern`."
    )


class PathPreviewGatewayRequest(ContractModel):
    """Request on ``gateway.mower.<id>.path.preview`` (internal, Fleet to Device-Gateway)."""

    mower_id: UUID
    account_id: UUID = Field(description="The mower's account as Fleet knows it.")
    area_ids: list[str] = Field(min_length=1, max_length=64)
    pattern: dict[str, Any] | None = None
    requested_by: PrincipalRef


class PathPreviewErrorCode(StrEnum):
    """Why no preview was given."""

    NOT_FOUND = "not_found"
    """The gateway does not hold the mower."""
    FORBIDDEN = "forbidden"
    """The account of the request does not own the mower."""
    UNSUPPORTED = "unsupported"
    """The mower's connection cannot plan a route without starting."""
    REJECTED = "rejected"
    """The mower refused: a job runs, unknown areas, or a pattern it cannot run."""
    UNAVAILABLE = "unavailable"
    """The mower is not connected."""
    TIMEOUT = "timeout"
    """The mower did not hand back its route in time."""
    FAILED = "failed"
    """The mower answered without a route."""


class PathPreviewError(ContractModel):
    """The error of a :class:`PathPreviewReply`."""

    code: PathPreviewErrorCode
    detail: str = Field(max_length=500)


class PathPreviewReply(ContractModel):
    """Reply to :class:`PathPreviewGatewayRequest`: exactly one of ``path`` or ``error``."""

    path: MowPath | None = None
    error: PathPreviewError | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.path is None) == (self.error is None):
            msg = "exactly one of path or error must be given"
            raise ValueError(msg)
        return self
