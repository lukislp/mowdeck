"""App wiring: settings, the database requirement, the track source and the resolver."""

from datetime import timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import PositionTrackRequest
from mowdeck_contracts.v1.subjects import TELEMETRY_POSITIONS_QUERY
from mowdeck_service_kit.bus import InMemoryBus
from planner_support import T0, make_settings

from planner.adapters.bus.telemetry import BusTrackSource
from planner.domain.errors import Unavailable
from planner.main import build_app, default_principal_resolver


def test_planner_needs_a_database_url_without_a_store() -> None:
    with pytest.raises(ValueError, match="MOWDECK_DATABASE_URL"):
        build_app(make_settings())


def test_planner_settings_become_the_config() -> None:
    config = make_settings(planner_coverage_max_hours=3, planner_overlap_m=0.1).to_config()
    assert config.coverage_max_range == timedelta(hours=3)
    assert config.overlap_m == 0.1


def test_planner_default_resolver_needs_the_jwks_url() -> None:
    assert default_principal_resolver({}) is None
    assert default_principal_resolver({"MOWDECK_AUTH_JWKS_URL": "http://identity/jwks"})


async def test_planner_track_source_fails_without_a_bus_or_responder() -> None:
    request = PositionTrackRequest(
        account_id=uuid4(), mower_id=uuid4(), start=T0 - timedelta(hours=1), end=T0
    )
    with pytest.raises(Unavailable, match="not connected"):
        await BusTrackSource(None).track(request)
    bus = InMemoryBus()
    with pytest.raises(Unavailable, match="did not answer"):
        await BusTrackSource(bus, 0.1).track(request)

    async def garbage(message):  # type: ignore[no-untyped-def]
        await message.respond(b"{}")

    await bus.subscribe(TELEMETRY_POSITIONS_QUERY, garbage)
    with pytest.raises(Unavailable, match="invalid track"):
        await BusTrackSource(bus, 0.5).track(request)
