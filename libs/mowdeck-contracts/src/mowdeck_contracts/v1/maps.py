"""Mower map geometry in local metres plus a geo reference.

Mowers with RTK (for example Mammotion LUBA) store their map in a device-local east-north frame
in metres around the RTK reference point, rotated by a yaw angle. MowDeck keeps that frame: all
geometry here is ``(x, y)`` in metres, and :class:`GeoReference` says how to convert it to WGS84.
The conversion itself lives in ``mowdeck_control.geo``, so this package stays pure data.

Planning (mowing patterns, route checks) works in metres; only display and telemetry convert to
latitude and longitude.
"""

from typing import Annotated, Self

from pydantic import Field, model_validator

from mowdeck_contracts.v1._base import ContractModel

LocalPoint = tuple[float, float]
"""A point ``(x, y)`` in metres in the map's local frame."""

Polygon = Annotated[list[LocalPoint], Field(min_length=3)]
"""A ring of at least three points; the closing edge back to the first point is implicit."""

MapElementId = Annotated[str, Field(min_length=1, max_length=128)]


class GeoReference(ContractModel):
    """Anchors the local frame on the earth.

    The local point ``(0, 0)`` is at ``(origin_lat, origin_lon)``. The local axes are the east
    and north axes rotated counter-clockwise by ``rotation_deg``: a local point ``(x, y)`` lies
    ``east = x cos(r) - y sin(r)`` and ``north = x sin(r) + y cos(r)`` metres from the origin.
    """

    origin_lat: float = Field(ge=-90, le=90)
    origin_lon: float = Field(ge=-180, le=180)
    rotation_deg: float = Field(default=0.0, ge=-360, le=360)


class MapArea(ContractModel):
    """A mowable area (a "zone" in the Mammotion app)."""

    id: MapElementId = Field(description="Stable id; for Mammotion the zone hash.")
    name: str | None = Field(default=None, max_length=200)
    boundary: Polygon


class MapZone(ContractModel):
    """A polygon the mower must stay out of: a no-go zone or an obstacle."""

    id: MapElementId
    name: str | None = Field(default=None, max_length=200)
    boundary: Polygon


class MapPath(ContractModel):
    """A transfer path between areas (driven with blades off)."""

    id: MapElementId
    name: str | None = Field(default=None, max_length=200)
    points: list[LocalPoint] = Field(min_length=2)


class DockPose(ContractModel):
    """Position and orientation of the charging station in the local frame."""

    x: float
    y: float
    heading_deg: float = Field(
        default=0.0, ge=0, lt=360, description="Local frame, counter-clockwise from +x."
    )


class MowerMap(ContractModel):
    """The map of one mower: areas, no-go zones, obstacles, paths and the dock."""

    reference: GeoReference
    areas: list[MapArea] = Field(default_factory=list)
    no_go_zones: list[MapZone] = Field(default_factory=list)
    obstacles: list[MapZone] = Field(default_factory=list)
    paths: list[MapPath] = Field(default_factory=list)
    dock: DockPose | None = None
    revision: str | None = Field(
        default=None, max_length=128, description="Map version; for Mammotion the map hash."
    )

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [
            element.id
            for group in (self.areas, self.no_go_zones, self.obstacles, self.paths)
            for element in group
        ]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            msg = f"map element ids must be unique, duplicates: {', '.join(duplicates)}"
            raise ValueError(msg)
        return self

    @property
    def area_ids(self) -> list[str]:
        """Ids of all areas, in map order."""
        return [area.id for area in self.areas]

    def area(self, area_id: str) -> MapArea | None:
        """Return the area with ``area_id``, or None."""
        return next((area for area in self.areas if area.id == area_id), None)

    def forbidden_zones(self) -> list[MapZone]:
        """No-go zones and obstacles together: everything the mower must stay out of."""
        return [*self.no_go_zones, *self.obstacles]
