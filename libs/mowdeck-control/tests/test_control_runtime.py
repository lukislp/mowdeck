"""State broadcaster, timers, manual test clock, safety rules and the package boundary."""

import asyncio
import logging
import tomllib
from pathlib import Path
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    ConnectionKind,
    ManualDrive,
    MowerActivity,
    MowerCapabilities,
    MowerState,
)
from mowdeck_control import AsyncioTimerFactory, StateBroadcaster, decide_manual_blade
from mowdeck_control.testing import DEFAULT_START, ManualClock, ManualTimers
from mowdeck_control.timers import TimerCallback

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _state(battery: int) -> MowerState:
    return MowerState(
        mower_id=uuid4(),
        account_id=uuid4(),
        observed_at=DEFAULT_START,
        activity=MowerActivity.IDLE,
        battery_percent=battery,
        connection=ConnectionKind.SIMULATED,
    )


async def test_broadcaster_fan_out_and_drop_oldest() -> None:
    broadcaster = StateBroadcaster(maxsize=2)
    first = broadcaster.subscribe(initial=_state(1))
    second = broadcaster.subscribe()
    assert broadcaster.subscriber_count == 2
    for battery in (2, 3, 4):
        broadcaster.publish(_state(battery))
    assert [(await anext(first)).battery_percent for _ in range(2)] == [3, 4]
    assert [(await anext(second)).battery_percent for _ in range(2)] == [3, 4]
    assert first.__aiter__() is first
    await first.aclose()
    await first.aclose()
    assert broadcaster.subscriber_count == 1
    with pytest.raises(StopAsyncIteration):
        await anext(first)
    broadcaster.close()
    with pytest.raises(StopAsyncIteration):
        await anext(second)
    third = broadcaster.subscribe()
    broadcaster.publish(_state(9))
    assert (await anext(third)).battery_percent == 9
    with pytest.raises(ValueError, match="maxsize"):
        StateBroadcaster(maxsize=0)


async def test_asyncio_timer_factory_fires_and_cancels(caplog: pytest.LogCaptureFixture) -> None:
    factory = AsyncioTimerFactory()
    fired: list[str] = []

    async def mark() -> None:
        fired.append("fired")

    async def broken() -> None:
        raise RuntimeError("timer broke")

    factory(0, mark)
    cancelled = factory(0, mark)
    cancelled.cancel()
    with caplog.at_level(logging.ERROR, logger="mowdeck_control.timers"):
        factory(0, broken)
        for _ in range(5):
            await asyncio.sleep(0)
    assert fired == ["fired"]
    assert "timer callback failed" in caplog.text


async def test_manual_clock_and_timers() -> None:
    clock = ManualClock()
    assert clock() == DEFAULT_START
    timers = ManualTimers(clock)
    order: list[int] = []

    def make(n: int) -> TimerCallback:
        async def callback() -> None:
            order.append(n)

        return callback

    timers(2.0, make(2))
    timers(1.0, make(1))
    dropped = timers(1.5, make(99))
    dropped.cancel()
    assert timers.pending == 2
    assert await timers.advance(0.5) == 0
    assert await timers.advance(2.0) == 2
    assert order == [1, 2]
    assert timers.pending == 0
    with pytest.raises(ValueError, match="timezone"):
        ManualClock(DEFAULT_START.replace(tzinfo=None))


def test_blade_rule_sensor_stop() -> None:
    caps = MowerCapabilities(manual_drive=True, manual_drive_blades=True)
    frame = ManualDrive(linear=0, angular=0, blade_on=True)
    decision = decide_manual_blade(frame, caps, MowerActivity.ERROR)
    assert not decision.blade_on
    assert "sensor stop" in (decision.forced_off_reason or "")
    assert decide_manual_blade(frame, caps, MowerActivity.PAUSED).blade_on


def test_package_never_imports_pymammotion() -> None:
    for path in (PACKAGE_ROOT / "src").rglob("*.py"):
        text = path.read_text("utf-8")
        assert "import pymammotion" not in text, path
        assert "from pymammotion" not in text, path


def test_package_is_apache_and_depends_only_on_contracts() -> None:
    project = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    assert project["license"] == "Apache-2.0"
    assert project["dependencies"] == ["mowdeck-contracts"]
    assert project["entry-points"]["mowdeck.connections"]["simulated"] == (
        "mowdeck_control.simulator:create_simulated_connection"
    )
    assert "Apache License" in (PACKAGE_ROOT / "LICENSE").read_text("utf-8")
