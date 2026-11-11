# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""All use cases of the Scheduler, wired from their ports."""

from dataclasses import dataclass

from mowdeck_authz import Authorizer
from mowdeck_service_kit.entitlements import Entitlements
from mowdeck_service_kit.erasure import AccountErasure
from mowdeck_service_kit.user_erasure import UserErasure

from scheduler.application.access import Access
from scheduler.application.angles import AngleService
from scheduler.application.context import SchedulerConfig
from scheduler.application.demo_purge import AccountPurge
from scheduler.application.erasure import account_erasure
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
from scheduler.application.user_erasure import user_erasure
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
    angles: AngleService
    purge: AccountPurge
    erasure: AccountErasure
    """Answers ``account.erase.scheduler`` (ADR 0082)."""
    user_erasure: UserErasure
    """Forgets a deleted user (``identity.user.deleted``, ADR 0082)."""


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
    ingest = Ingest(store)
    purge = AccountPurge(store)
    return Services(
        schedules=ScheduleService(store, access, fleet, weather, config, clock, entitlements),
        runner=Runner(store, fleet, access, weather, events, config, clock, observer),
        ingest=ingest,
        weather=weather,
        access=access,
        config=config,
        angles=AngleService(store, clock),
        purge=purge,
        erasure=account_erasure(purge, store, ingest),
        user_erasure=user_erasure(store),
    )
