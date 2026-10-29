"""Past mowing days of a simulated mower, for the public demo (ADR 0068).

:func:`simulated_history` runs a :class:`~mowdeck_control.simulator.SimulatedMower` offline on a
manual clock: one job over every area of its garden per day, from the dock and back to the dock
and charged, and returns the states it reported, oldest first. Fed to Telemetry's ingestion they
give a fresh demo installation tracks, coverage, heatmaps and health totals as if the mower had
worked for a while.

Two things are added that the live simulator keeps constant, so the signal heatmaps show
something: the Wi-Fi signal falls with the distance from the house (a router next to the dock)
and the mobile signal varies a little; near the first no-go zone (a tree, a pond) the RTK fix
drops to float. Everything is deterministic for the same arguments.
"""

import math
import random
from collections.abc import Mapping
from dataclasses import fields, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import (
    MowerActivity,
    MowerMap,
    MowerState,
    NetworkKind,
    RtkFixStatus,
    SignalStatus,
    StartMow,
)

from mowdeck_control.simulator import GARDENS, SimulatedMower, SimulationConfig
from mowdeck_control.testing import ManualClock

MAX_DAYS = 31
"""Upper bound of ``days`` (Telemetry keeps the detailed positions longer, but a demo needs no
more)."""

_STEP_S = 10.0
"""Simulated seconds between two reported states while the mower drives."""
_CHARGE_STEP_S = 120.0
"""Simulated seconds between two reported states while it charges."""
_JOB_LIMIT_S = 6 * 3600.0
"""A job that has not ended after this long is cut off (never with the built-in gardens)."""
_ROUTER_OFFSET_M = (-3.0, -2.0)
"""Where the Wi-Fi router stands, relative to the dock."""
_SHADOW_RADIUS_M = 3.0
"""Around the first no-go zone's centre the RTK fix is float."""
_ANGLES = (0, 45, 90, 135)
"""The mowing angle turns from day to day, like a schedule with angle rotation."""


def _config(options: Mapping[str, Any]) -> SimulationConfig:
    """The simulation config of the live mower's connection options (unknown keys, such as the
    links the Device-Gateway passes, are ignored); real time, since the clock is manual, and
    nothing that would interrupt the day's job."""
    names = {f.name for f in fields(SimulationConfig)}
    known = {k: v for k, v in options.items() if k in names}
    known["time_scale"] = 1.0
    known["stuck_after_seconds"] = None
    known["battery_percent"] = 100.0  # every past day starts charged
    known["start_delay_s"] = None
    known.pop("bridge_devices", None)
    known.pop("drive_speed_range", None)
    return replace(SimulationConfig(), **known)


def _centre(points: list[tuple[float, float]]) -> tuple[float, float]:
    return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))


class _Radio:
    """The signals and the RTK shadow of one garden."""

    def __init__(self, garden: MowerMap, seed: int) -> None:
        dock = garden.dock
        assert dock is not None  # noqa: S101 - every simulated garden has a dock
        self.router = (dock.x + _ROUTER_OFFSET_M[0], dock.y + _ROUTER_OFFSET_M[1])
        zones = garden.no_go_zones
        self.shadow = _centre(list(zones[0].boundary)) if zones else None
        self.random = random.Random(seed)  # noqa: S311 - not security

    def wifi(self, x: float, y: float) -> int:
        distance = max(1.0, math.dist((x, y), self.router))
        value = -38 - 26 * math.log10(distance) + self.random.gauss(0, 1.5)
        return round(max(-95.0, min(-30.0, value)))

    def mobile(self, x: float, y: float) -> int:
        value = -84 + 3 * math.sin(x / 4) + 2 * math.cos(y / 5) + self.random.gauss(0, 1.0)
        return round(max(-120.0, min(-60.0, value)))

    def fix(self, x: float, y: float) -> RtkFixStatus:
        if self.shadow is not None and math.dist((x, y), self.shadow) < _SHADOW_RADIUS_M:
            return RtkFixStatus.FLOAT
        return RtkFixStatus.FIXED


def _with_signal(state: MowerState, radio: _Radio) -> MowerState:
    x, y = float(state.raw.get("x", 0.0)), float(state.raw.get("y", 0.0))
    signal = SignalStatus(
        wifi_dbm=radio.wifi(x, y), mobile_dbm=radio.mobile(x, y), network=NetworkKind.WIFI
    )
    return state.model_copy(update={"signal": signal})


async def _one_day(
    mower_id: UUID,
    account_id: UUID,
    config: SimulationConfig,
    start: datetime,
    angle: int,
    radio: _Radio,
) -> list[MowerState]:
    clock = ManualClock(start)
    mower = SimulatedMower(mower_id=mower_id, account_id=account_id, config=config, clock=clock)
    garden = GARDENS[config.garden](config.origin)
    await mower.connect()
    try:
        states = [_with_signal(mower.snapshot(), radio)]
        command = StartMow(
            area_ids=[a.id for a in garden.areas],
            pattern={"name": "stripes", "angle_deg": angle},
        )
        outcome = await mower.execute(command)
        if outcome.status != "executed":  # pragma: no cover - the gardens always start
            return states
        elapsed = 0.0
        left_dock = False
        while elapsed < _JOB_LIMIT_S:
            activity = states[-1].activity
            charging = left_dock and activity is MowerActivity.CHARGING
            step = _CHARGE_STEP_S if charging else _STEP_S
            x, y = float(states[-1].raw.get("x", 0.0)), float(states[-1].raw.get("y", 0.0))
            fix = radio.fix(x, y)
            if fix is not mower.snapshot().rtk_fix:
                mower.set_rtk(fix)
            clock.advance(step)
            elapsed += step
            state = mower.tick(step)
            states.append(_with_signal(state, radio))
            if state.activity is MowerActivity.MOWING:
                left_dock = True
            if left_dock and state.activity is MowerActivity.DOCKED:
                break
        return states
    finally:
        await mower.disconnect()


async def simulated_history(
    *,
    mower_id: UUID,
    account_id: UUID,
    options: Mapping[str, Any] | None = None,
    end: datetime,
    days: int = 7,
    start_hour: int = 9,
) -> list[MowerState]:
    """The states of ``days`` past mowing days before ``end`` (oldest first).

    Args:
        mower_id: The mower the states belong to.
        account_id: Its account.
        options: The mower's simulated connection options (``garden``, ``origin_lat``, ...).
        end: Nothing is reported at or after this time (normally "now").
        days: How many days back, one job per day (1 to :data:`MAX_DAYS`).
        start_hour: Hour of day (UTC) the daily job starts.
    """
    if not 1 <= days <= MAX_DAYS:
        msg = f"days must be within 1..{MAX_DAYS}"
        raise ValueError(msg)
    if end.tzinfo is None:
        msg = "end must be timezone-aware"
        raise ValueError(msg)
    config = _config(options or {})
    radio = _Radio(GARDENS[config.garden](config.origin), seed=mower_id.int % 10_000)
    result: list[MowerState] = []
    first_day = end.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    for back in range(days, 0, -1):
        start = first_day - timedelta(days=back)
        day = await _one_day(
            mower_id, account_id, config, start, _ANGLES[back % len(_ANGLES)], radio
        )
        result.extend(s for s in day if s.observed_at < end)
    return result
