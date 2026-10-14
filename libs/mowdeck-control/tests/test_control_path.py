"""The mow path: the simulator's route and progress, and the controller's preview rules."""

import asyncio
from uuid import UUID

import pytest
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandStatus,
    MowerActivity,
    PrincipalKind,
    PrincipalRef,
    StartMow,
)
from mowdeck_control import (
    CommandRejected,
    MowerController,
    MowPathSource,
    PathPreviewCapable,
    SimulatedMower,
    current_path_of,
)
from mowdeck_control.testing import ManualClock

ISSUER = PrincipalRef(subject=UUID("00000000-0000-4000-8000-0000000000aa"), kind=PrincipalKind.USER)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _start(controller: MowerController, sim: SimulatedMower, clock: ManualClock) -> None:
    envelope = CommandEnvelope.create(
        mower_id=sim.mower_id,
        account_id=sim.account_id,
        issued_by=ISSUER,
        command=StartMow(area_ids=["front"]),
        now=clock(),
    )
    assert (await controller.handle(envelope)).status is CommandStatus.EXECUTED


async def test_simulator_previews_the_route_it_then_drives() -> None:
    clock = ManualClock()
    sim = SimulatedMower(clock=clock)
    assert isinstance(sim, PathPreviewCapable)
    assert isinstance(sim, MowPathSource)
    controller = MowerController(sim, mower_id=sim.mower_id, clock=clock)
    async with controller:
        assert controller.can_preview_path
        preview = await controller.preview_path(["front"], {"name": "stripes"})
        assert preview.progress is None
        assert preview.area_ids == ["front"]
        assert preview.points_total == sum(len(line) for line in preview.polylines) > 4
        assert preview.kinds == ["mow"] * len(preview.polylines)
        assert controller.current_path() is None  # nothing runs yet

        await _start(controller, sim, clock)
        running = controller.current_path()
        assert running is not None
        assert running.path_hash == preview.path_hash
        assert running.polylines == preview.polylines
        assert running.progress is not None
        assert running.progress.index == 0

        sim.tick(60)
        later = current_path_of(sim)
        assert later is not None
        assert later.progress is not None
        assert 0 < later.progress.index <= later.points_total


async def test_path_ends_with_the_job() -> None:
    clock = ManualClock()
    sim = SimulatedMower(clock=clock)
    controller = MowerController(sim, mower_id=sim.mower_id, clock=clock)
    async with controller:
        await _start(controller, sim, clock)
        for _ in range(400):
            sim.tick(30)
            if (await sim.get_state()).activity in (
                MowerActivity.DOCKED,
                MowerActivity.CHARGING,
            ):
                break
        assert controller.current_path() is None


async def test_controller_refuses_a_preview_while_a_job_runs() -> None:
    clock = ManualClock()
    sim = SimulatedMower(clock=clock)
    controller = MowerController(sim, mower_id=sim.mower_id, clock=clock)
    async with controller:
        await _start(controller, sim, clock)
        await _settle()
        with pytest.raises(CommandRejected, match="mowing"):
            await controller.preview_path(["front"])


async def test_controller_checks_areas_and_pattern_of_a_preview() -> None:
    clock = ManualClock()
    sim = SimulatedMower(clock=clock)
    controller = MowerController(sim, mower_id=sim.mower_id, clock=clock)
    async with controller:
        with pytest.raises(CommandRejected, match="unknown area ids: nowhere"):
            await controller.preview_path(["nowhere"])
        with pytest.raises(CommandRejected, match="area"):
            await controller.preview_path([])
        with pytest.raises(CommandRejected):
            await controller.preview_path(["front"], {"name": "spiral"})


async def test_controller_without_preview_support_refuses() -> None:
    class Plain(SimulatedMower):
        preview_path = None  # type: ignore[assignment]

    clock = ManualClock()
    sim = Plain(clock=clock)
    controller = MowerController(sim, mower_id=sim.mower_id, clock=clock)
    assert current_path_of(object()) is None
    async with controller:
        assert not controller.can_preview_path
        with pytest.raises(CommandRejected, match="cannot plan"):
            await controller.preview_path(["front"])
