"""All use cases of the Scheduler, wired from their ports."""

from dataclasses import dataclass

from mowdeck_authz import Authorizer
from mowdeck_service_kit.entitlements import Entitlements

from scheduler.application.access import Access
from scheduler.application.context import SchedulerConfig
from scheduler.application.ingest import Ingest
from scheduler.application.ports import (
    Clock,
    EventPublisher,
    FleetGateway,
    RunObserver,
    Store,
    WeatherProvider,
)
from scheduler.application.runner import Runner
from scheduler.application.schedules import ScheduleService
from scheduler.application.weather import WeatherService


@dataclass(frozen=True, slots=True)
class Services:
    """The use cases, created once at startup."""

    schedules: ScheduleService
    runner: Runner
    ingest: Ingest
    weather: WeatherService
    access: Access
    config: SchedulerConfig


def build_services(
    *,
    store: Store,
    authorizer: Authorizer,
    fleet: FleetGateway,
    provider: WeatherProvider,
    events: EventPublisher,
    config: SchedulerConfig,
    clock: Clock,
    observer: RunObserver | None = None,
    entitlements: Entitlements | None = None,
) -> Services:
    """Wire the use cases."""
    access = Access(authorizer)
    weather = WeatherService(store, provider)
    return Services(
        schedules=ScheduleService(store, access, fleet, weather, config, clock, entitlements),
        runner=Runner(store, fleet, access, weather, events, config, clock, observer),
        ingest=Ingest(store),
        weather=weather,
        access=access,
        config=config,
    )
