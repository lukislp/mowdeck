"""Job options of a native start in the simulator: declared, recorded, start progress, stuck."""

from typing import Any

import pytest
from mowdeck_contracts.v1 import CommandStatus, MowerActivity, StartMow, StartOptions
from mowdeck_control import SimulatedMower, SimulationConfig
from mowdeck_control.simulator import SIMULATED_START_OPTIONS, STUCK_ERROR_CODE
from mowdeck_control.testing import ManualClock


async def _sim(**config: Any) -> SimulatedMower:
    sim = SimulatedMower(config=SimulationConfig(**config), clock=ManualClock())
    await sim.connect()
    return sim


async def test_simulator_declares_the_luba_job_options() -> None:
    sim = await _sim()
    caps = await sim.capabilities()
    assert caps.start_options == SIMULATED_START_OPTIONS
    assert [mode.value for mode in caps.start_options.obstacle_detection] == [
        "off",
        "standard",
        "sensitive",
    ]
    plain = await _sim(start_options=False)
    assert (await plain.capabilities()).start_options == StartOptions()


async def test_simulator_records_the_options_of_a_start() -> None:
    sim = await _sim()
    pattern = {"obstacle_detection": "sensitive", "obstacle_laps": 0, "order": "grid_first"}
    outcome = await sim.execute(StartMow(area_ids=["front"], pattern=pattern))
    assert outcome.status is CommandStatus.EXECUTED
    assert sim.job_options == pattern
    await sim.execute(StartMow(area_ids=["front"]))
    assert sim.job_options == {}, "a start without options records none"


async def test_simulator_refuses_options_it_does_not_offer() -> None:
    sim = await _sim(start_options=False)
    outcome = await sim.execute(
        StartMow(area_ids=["front"], pattern={"obstacle_detection": "standard"})
    )
    assert outcome.status is CommandStatus.REJECTED
    assert "obstacle detection" in (outcome.reason or "")
    assert sim.job_options == {}


async def test_simulator_starts_the_route_at_the_start_progress() -> None:
    full = await _sim()
    await full.execute(StartMow(area_ids=["front"]))
    halfway = await _sim()
    await halfway.execute(StartMow(area_ids=["front"], pattern={"start_progress_pct": 50}))
    state = await halfway.get_state()
    assert state.job is not None
    assert state.job.progress_percent is not None
    assert 45 <= state.job.progress_percent <= 50
    assert halfway.job_options == {"start_progress_pct": 50}
    full_path, half_path = full.current_path(), halfway.current_path()
    assert full_path is not None and half_path is not None
    # The same route, the progress index starts about halfway along it.
    assert half_path.points_total == full_path.points_total
    assert half_path.progress is not None and full_path.progress is not None
    assert full_path.progress.index == 0
    assert half_path.progress.index > full_path.points_total // 3
    # It drives to the resume point with the blades off, then mows on.
    assert state.raw["phase"] == "transfer"
    for _ in range(600):
        state = halfway.tick(1)
        if state.raw["phase"] == "mowing":
            break
    assert state.raw["phase"] == "mowing"
    assert state.job is not None
    assert (state.job.progress_percent or 0) >= 45


async def test_simulator_start_progress_zero_is_the_beginning() -> None:
    sim = await _sim()
    await sim.execute(StartMow(area_ids=["front"], pattern={"start_progress_pct": 0}))
    state = await sim.get_state()
    assert state.job is not None and state.job.progress_percent == 0
    assert sim.job_options == {}


async def _mowing_seconds_until_stuck(detection: str | None) -> float:
    sim = await _sim(stuck_after_seconds=20)
    pattern = {"obstacle_detection": detection} if detection else None
    await sim.execute(StartMow(area_ids=["front"], pattern=pattern))
    for _ in range(2000):
        state = sim.tick(1)
        if state.activity is MowerActivity.ERROR:
            assert state.error_code == STUCK_ERROR_CODE
            assert sim._job is not None
            return sim._job.mowing_s
    msg = "never stuck"
    raise AssertionError(msg)


@pytest.mark.parametrize(
    ("detection", "seconds"),
    [(None, 20), ("standard", 20), ("off", 10), ("sensitive", 40)],
)
async def test_simulator_obstacle_detection_changes_how_soon_it_gets_stuck(
    detection: str | None, seconds: float
) -> None:
    assert await _mowing_seconds_until_stuck(detection) == pytest.approx(seconds, abs=1)
