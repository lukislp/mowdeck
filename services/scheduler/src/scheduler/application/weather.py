"""Where a mower is and what the weather there does to a run.

Location, in this order: the coordinates of the mower's site (Fleet, answered with the target),
the geo origin of the mower's map (``device.mower.<id>.map``), the last position the mower
reported (``telemetry.mower.<id>``). Without any of them the rain pause reports
``no_location`` and the run goes ahead.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from mowdeck_contracts.v1 import TargetMower

from scheduler.application.ports import Store, WeatherProvider
from scheduler.domain.models import MowerPlace, MowerStatus
from scheduler.domain.rain import Forecast, RainDecision, RainSettings, Verdict, decide

LocationSource = Literal["site", "map", "position"]


@dataclass(frozen=True, slots=True)
class Location:
    """Coordinates of a mower and where they came from."""

    latitude: float
    longitude: float
    source: LocationSource


@dataclass(frozen=True, slots=True)
class WeatherAnswer:
    """A rain decision with the location and forecast behind it."""

    decision: RainDecision
    location: Location | None
    forecast: Forecast | None
    provider: str
    settings: RainSettings
    """The rain pause settings the decision applied (echoed so clients need no defaults)."""


class WeatherService:
    """Locates mowers and asks the weather provider."""

    def __init__(self, store: Store, provider: WeatherProvider) -> None:
        self.store = store
        self.provider = provider

    async def locate(self, mower_id: UUID, target: TargetMower | None = None) -> Location | None:
        """The best known location of a mower (see the module docstring)."""
        if target is not None and target.latitude is not None and target.longitude is not None:
            return Location(target.latitude, target.longitude, "site")
        async with self.store.transaction() as tx:
            place = await tx.get_place(mower_id)
            status = await tx.get_status(mower_id)
        return self.locate_from(target, place, status)

    @staticmethod
    def locate_from(
        target: TargetMower | None, place: MowerPlace | None, status: MowerStatus | None
    ) -> Location | None:
        """:meth:`locate` from facts already read."""
        if target is not None and target.latitude is not None and target.longitude is not None:
            return Location(target.latitude, target.longitude, "site")
        if place is not None and place.latitude is not None and place.longitude is not None:
            return Location(place.latitude, place.longitude, "map")
        if status is not None and status.latitude is not None and status.longitude is not None:
            return Location(status.latitude, status.longitude, "position")
        return None

    async def decide(
        self, settings: RainSettings, location: Location | None, at: datetime
    ) -> WeatherAnswer:
        """The rain decision at ``at`` for ``location``."""
        name = self.provider.name
        if not settings.enabled:
            return WeatherAnswer(decide(settings, None, at), location, None, name, settings)
        if location is None:
            decision = RainDecision(Verdict.NO_LOCATION, "no location known, running anyway")
            return WeatherAnswer(decision, None, None, name, settings)
        forecast = await self.provider.forecast(location.latitude, location.longitude)
        return WeatherAnswer(decide(settings, forecast, at), location, forecast, name, settings)
