"""The demo gardens and the offline history of a simulated mower (ADR 0065)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import MowerActivity, RtkFixStatus
from mowdeck_control import SimulatedMower, SimulationConfig, point_in_polygon
from mowdeck_control.history import MAX_DAYS, simulated_history
from mowdeck_control.simulator import GARDENS
from mowdeck_control.testing import ManualClock

END = datetime(2026, 6, 15, 18, 0, tzinfo=UTC)


@pytest.mark.parametrize("name", sorted(GARDENS))
def test_every_garden_is_a_usable_map(name: str) -> None:
    garden = GARDENS[name]((48.1, 11.5))
    assert garden.dock is not None
    assert garden.areas
    assert garden.reference.origin_lat == 48.1
    for zone in garden.no_go_zones:
        centre = (
            sum(p[0] for p in zone.boundary) / len(zone.boundary),
            sum(p[1] for p in zone.boundary) / len(zone.boundary),
        )
        assert any(point_in_polygon(centre, a.boundary) for a in garden.areas), zone.id


def test_the_garden_option_selects_the_map() -> None:
    mower = SimulatedMower(config=SimulationConfig(garden="orchard"), clock=ManualClock())
    assert mower.snapshot().position is not None
    with pytest.raises(ValueError, match="unknown garden"):
        SimulationConfig(garden="jungle")


async def test_history_is_one_finished_job_per_day_before_the_end() -> None:
    mower_id, account_id = uuid4(), uuid4()
    states = await simulated_history(
        mower_id=mower_id,
        account_id=account_id,
        options={"garden": "family", "links": {"ble_bridge": {}}, "time_scale": 50},
        end=END,
        days=2,
    )
    assert states
    assert all(s.mower_id == mower_id and s.account_id == account_id for s in states)
    times = [s.observed_at for s in states]
    assert times == sorted(times)
    assert times[0] >= END - timedelta(days=3)
    assert times[-1] < END
    days = {t.date() for t in times}
    assert len(days) == 2
    for day in days:
        of_day = [s for s in states if s.observed_at.date() == day]
        activities = {s.activity for s in of_day}
        assert MowerActivity.MOWING in activities
        assert of_day[-1].activity is MowerActivity.DOCKED
    mowing = [s for s in states if s.raw.get("blade_on")]
    assert mowing


async def test_history_varies_the_signals_and_the_fix() -> None:
    states = await simulated_history(
        mower_id=uuid4(), account_id=uuid4(), options={"garden": "family"}, end=END, days=1
    )
    wifi = {s.signal.wifi_dbm for s in states if s.signal is not None}
    mobile = {s.signal.mobile_dbm for s in states if s.signal is not None}
    assert len(wifi) > 5
    assert len(mobile) > 3
    assert all(-95 <= w <= -30 for w in wifi if w is not None)
    fixes = {s.rtk_fix for s in states}
    assert {RtkFixStatus.FIXED, RtkFixStatus.FLOAT} <= fixes


async def test_history_is_deterministic() -> None:
    mower_id, account_id = uuid4(), uuid4()
    first = await simulated_history(mower_id=mower_id, account_id=account_id, end=END, days=1)
    again = await simulated_history(mower_id=mower_id, account_id=account_id, end=END, days=1)
    assert [s.model_dump() for s in first] == [s.model_dump() for s in again]


@pytest.mark.parametrize("days", [0, MAX_DAYS + 1])
async def test_history_rejects_a_bad_day_count(days: int) -> None:
    with pytest.raises(ValueError, match="days"):
        await simulated_history(mower_id=uuid4(), account_id=uuid4(), end=END, days=days)


async def test_history_needs_an_aware_end() -> None:
    with pytest.raises(ValueError, match="timezone"):
        await simulated_history(
            mower_id=uuid4(), account_id=uuid4(), end=datetime(2026, 6, 15), days=1
        )
