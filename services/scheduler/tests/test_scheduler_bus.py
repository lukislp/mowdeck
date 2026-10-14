"""Bus adapters: the state and map handlers, the Fleet gateway, the event publisher, the app's
startup and shutdown, and the ``fleet.commands.issue`` round trip on the in-memory bus."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from mowdeck_contracts.v1 import (
    CommandRequest,
    IssueCommandRequest,
    MowerActivity,
    PrincipalKind,
    PrincipalRef,
    ScheduleDisabled,
    StartMow,
)
from mowdeck_contracts.v1.subjects import FLEET_COMMANDS_ISSUE, device_subject, telemetry_subject
from mowdeck_service_kit.bus import InMemoryBus
from scheduler_support import harness, make_settings

from scheduler.adapters.bus.fleet import BusFleetGateway
from scheduler.adapters.bus.publisher import BusEventPublisher
from scheduler.adapters.memory import MemoryStore
from scheduler.adapters.weather.open_meteo import OpenMeteoProvider
from scheduler.adapters.weather.static import StaticWeatherProvider
from scheduler.domain.errors import Unavailable
from scheduler.main import build_app, default_principal_resolver, make_provider


async def test_scheduler_bus_states_and_maps_are_remembered() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = uuid4()
        await h.state(mower_id, owner.account_id, MowerActivity.MOWING, position=(52.0, 13.0))
        await h.map(mower_id, owner.account_id, areas=("x",), origin=(0.0, 0.0))
        await h.bus.publish(telemetry_subject(mower_id), b"{not json")
        await h.bus.publish(telemetry_subject(uuid4()), b"{}")
        await h.bus.publish(device_subject(mower_id, "capabilities"), b"{}")
        await h.bus.publish(device_subject(mower_id, "map"), b"[]")
        other = uuid4()
        snapshot = h.bus.messages(device_subject(mower_id, "map"))[0].data
        await h.bus.publish(device_subject(other, "map"), snapshot)  # wrong mower in subject
        status = h.store.data.status[mower_id]
        place = h.store.data.places[mower_id]
    assert status.activity == "mowing" and (status.latitude, status.longitude) == (52.0, 13.0)
    assert place.area_ids == ["x"] and place.latitude is None  # (0, 0) is no location
    assert other not in h.store.data.places
    assert len(h.bus.dropped) >= 3


async def test_scheduler_bus_state_subject_must_match() -> None:
    async with harness() as h:
        owner = await h.owner()
        await h.state(uuid4(), owner.account_id, MowerActivity.IDLE)
        message = h.bus.messages("telemetry.mower.*")[0]
        await h.bus.publish(telemetry_subject(uuid4()), message.data)
        await h.bus.publish("telemetry.mower.not-a-uuid", message.data)
    assert len(h.store.data.status) == 1


async def test_scheduler_issue_round_trip_on_the_in_memory_bus() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        viewer = await h.member(owner, "viewer")
        gateway = BusFleetGateway(h.bus)

        def request(subject: Any) -> IssueCommandRequest:
            return IssueCommandRequest(
                on_behalf_of=PrincipalRef(subject=subject, kind=PrincipalKind.USER),
                account_id=owner.account_id,
                mower_id=mower_id,
                command=CommandRequest(command=StartMow(area_ids=["front"])),
                schedule_id=uuid4(),
            )

        accepted = await gateway.issue(request(owner.principal.subject))
        refused = await gateway.issue(request(viewer.principal.subject))
        resolved = await gateway.resolve(owner.account_id, "mower", mower_id)
    assert accepted.accepted is not None and refused.error is not None
    assert refused.error.code.value == "forbidden"
    assert [c.command_id for c in h.commands] == [accepted.accepted.command_id]
    assert resolved.found and resolved.mowers[0].mower_id == mower_id


async def test_scheduler_fleet_gateway_failures_are_unavailable() -> None:
    with pytest.raises(Unavailable):
        await BusFleetGateway(None).resolve(uuid4(), "mower", uuid4())
    bus = InMemoryBus()
    with pytest.raises(Unavailable, match="did not answer"):
        await BusFleetGateway(bus, 0.1).resolve(uuid4(), "mower", uuid4())

    async def garbage(message: Any) -> None:
        await message.respond(b'{"accepted": null, "error": null}')

    await bus.subscribe(FLEET_COMMANDS_ISSUE, garbage)
    with pytest.raises(Unavailable, match="invalid reply"):
        await BusFleetGateway(bus).issue(
            IssueCommandRequest(
                on_behalf_of=PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
                account_id=uuid4(),
                mower_id=uuid4(),
                command=CommandRequest(command=StartMow(area_ids=["a"])),
            )
        )
    await bus.close()


async def test_scheduler_publisher_without_and_with_a_broken_bus() -> None:
    event = ScheduleDisabled(
        schedule_id=uuid4(),
        account_id=uuid4(),
        name="x",
        reason="y",
        owner=PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
        disabled_at=datetime.now(UTC),
    )
    await BusEventPublisher(None).schedule_disabled(event)  # logged only
    bus = InMemoryBus()
    await bus.close()
    await BusEventPublisher(bus).schedule_disabled(event)  # failure logged, not raised


async def test_scheduler_app_startup_shutdown_and_wiring() -> None:
    settings = make_settings(scheduler_weather_provider="open_meteo")
    async with httpx.AsyncClient() as http:
        assert isinstance(make_provider(settings, http), OpenMeteoProvider)
        assert isinstance(make_provider(make_settings(), http), StaticWeatherProvider)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        build_app(make_settings())
    async with harness(background_tasks=True, scheduler_tick_seconds=0.1) as h:
        assert len(h.app.state.subscriptions) == 4  # states, maps, grants, plans
        assert (await h.client.get("/metrics")).status_code == 200
    assert default_principal_resolver({}) is None
    assert MemoryStore().data.schedules == {}
    assert make_settings().to_config().max_permission_skips == 3
