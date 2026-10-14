"""Pure rules: write decisions, heartbeat de-duplication, bins and downsampling."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import MowerActivity, RtkFixStatus
from telemetry_support import T0, make_state, seconds

from telemetry.domain.models import HistoryRow, LatestRow, PositionRow
from telemetry.domain.rules import (
    bin_count,
    bin_edges,
    bin_start,
    decide,
    downsample,
    history_row,
    parse_step,
    position_fingerprint,
    position_row,
    raw_points,
    state_fingerprint,
)

MOWER = uuid4()
ACCOUNT = uuid4()


def latest_of(state_args: dict[str, object] | None = None, **fields: object) -> LatestRow:
    """The latest row after ``make_state(**state_args)`` was fully stored."""
    state = make_state(MOWER, ACCOUNT, **(state_args or {}))  # type: ignore[arg-type]
    values: dict[str, object] = {
        "mower_id": MOWER,
        "account_id": ACCOUNT,
        "observed_at": state.observed_at,
        "received_at": state.observed_at,
        "activity": state.activity.value,
        "battery_percent": state.battery_percent,
        "state": {},
        "history_written_at": state.observed_at,
        "history_fingerprint": state_fingerprint(state),
        "position_fingerprint": position_fingerprint(state),
        **fields,
    }
    return LatestRow(**values)  # type: ignore[arg-type]


def test_telemetry_first_state_writes_everything() -> None:
    decision = decide(
        make_state(MOWER, ACCOUNT, position=(48.1, 11.5)), None, heartbeat_seconds=300
    )
    assert (decision.latest, decision.history, decision.position) == (True, True, True)
    no_position = decide(make_state(MOWER, ACCOUNT), None, heartbeat_seconds=300)
    assert no_position.position is False


def test_telemetry_older_observed_at_is_ignored() -> None:
    current = latest_of()
    older = make_state(MOWER, ACCOUNT, at=T0 - seconds(1), battery=10, position=(1.0, 2.0))
    decision = decide(older, current, heartbeat_seconds=300)
    assert decision.nothing


def test_telemetry_unchanged_heartbeat_is_not_stored_in_history() -> None:
    current = latest_of()
    heartbeat = make_state(MOWER, ACCOUNT, at=T0 + seconds(10))
    decision = decide(heartbeat, current, heartbeat_seconds=300)
    assert (decision.latest, decision.history, decision.position) == (True, False, False)


def test_telemetry_unchanged_state_is_stored_once_per_heartbeat_interval() -> None:
    current = latest_of()
    just_before = make_state(MOWER, ACCOUNT, at=T0 + seconds(299))
    due = make_state(MOWER, ACCOUNT, at=T0 + seconds(300))
    assert decide(just_before, current, heartbeat_seconds=300).history is False
    assert decide(due, current, heartbeat_seconds=300).history is True


@pytest.mark.parametrize(
    "change",
    [
        {"battery": 89},
        {"activity": MowerActivity.MOWING},
        {"rtk_fix": RtkFixStatus.FLOAT},
        {"charging": True},
        {"blade_height_mm": 40},
        {"speed_mps": 0.5},
        {"error_code": "E1"},
    ],
)
def test_telemetry_a_significant_change_is_always_stored(change: dict[str, object]) -> None:
    current = latest_of()
    changed = make_state(MOWER, ACCOUNT, at=T0 + seconds(10), **change)  # type: ignore[arg-type]
    assert decide(changed, current, heartbeat_seconds=300).history is True


def test_telemetry_position_is_stored_when_it_differs_from_the_last_one() -> None:
    current = latest_of({"position": (48.1, 11.5)})
    same = make_state(MOWER, ACCOUNT, at=T0 + seconds(10), position=(48.1, 11.5))
    moved = make_state(MOWER, ACCOUNT, at=T0 + seconds(10), position=(48.1000005, 11.5))
    fix_changed = make_state(
        MOWER, ACCOUNT, at=T0 + seconds(10), position=(48.1, 11.5), rtk_fix=RtkFixStatus.FLOAT
    )
    gone = make_state(MOWER, ACCOUNT, at=T0 + seconds(10))
    assert decide(same, current, heartbeat_seconds=300).position is False
    assert decide(moved, current, heartbeat_seconds=300).position is True
    assert decide(fix_changed, current, heartbeat_seconds=300).position is True
    assert decide(gone, current, heartbeat_seconds=300).position is False


def test_telemetry_standing_mower_writes_positions_on_signal_change_and_heartbeat() -> None:
    """A docked mower keeps its position; its signal and the heartbeat still add samples."""
    docked = make_state(MOWER, ACCOUNT, position=(48.1, 11.5)).model_copy(
        update={"raw": {"wifi_rssi": -31, "mnet_rssi": -77}}
    )
    current = latest_of(
        {"position": (48.1, 11.5)}, position_fingerprint=position_fingerprint(docked)
    )
    within_bucket = docked.model_copy(
        update={"observed_at": T0 + seconds(10), "raw": {"wifi_rssi": -33, "mnet_rssi": -77}}
    )
    next_bucket = docked.model_copy(
        update={"observed_at": T0 + seconds(10), "raw": {"wifi_rssi": -36, "mnet_rssi": -77}}
    )
    heartbeat = docked.model_copy(update={"observed_at": T0 + seconds(300)})
    assert decide(within_bucket, current, heartbeat_seconds=300).position is False
    assert decide(next_bucket, current, heartbeat_seconds=300).position is True
    assert decide(heartbeat, current, heartbeat_seconds=300).position is True
    # No position at all: the heartbeat writes no position row.
    gone = make_state(MOWER, ACCOUNT, at=T0 + seconds(300))
    assert decide(gone, current, heartbeat_seconds=300).position is False


def test_telemetry_rows_are_built_from_the_state() -> None:
    state = make_state(MOWER, ACCOUNT, position=(48.1, 11.5), error_code="E1")
    row = history_row(state)
    assert (row.mower_id, row.account_id, row.error_code) == (MOWER, ACCOUNT, "E1")
    position = position_row(state)
    assert position is not None and (position.lat, position.lon) == (48.1, 11.5)
    assert position_row(make_state(MOWER, ACCOUNT)) is None
    assert position_fingerprint(make_state(MOWER, ACCOUNT)) is None


def test_telemetry_parse_step() -> None:
    assert parse_step("5s") == timedelta(seconds=5)
    assert parse_step(" 2H ") == timedelta(hours=2)
    assert parse_step("1d") == timedelta(days=1)
    for bad in ("", "5", "0s", "5x", "-1s", "1.5m", "m"):
        with pytest.raises(ValueError, match="step must"):
            parse_step(bad)


def test_telemetry_bins_align_to_the_date_bin_origin() -> None:
    step = timedelta(minutes=15)
    at = datetime(2026, 10, 9, 12, 7, 31, tzinfo=UTC)
    assert bin_start(at, step) == datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    assert bin_start(datetime(2000, 1, 1, tzinfo=UTC), step) == datetime(2000, 1, 1, tzinfo=UTC)
    assert bin_start(datetime(1999, 12, 31, 23, 59, tzinfo=UTC), step) == datetime(
        1999, 12, 31, 23, 45, tzinfo=UTC
    )


def test_telemetry_bin_edges_and_count() -> None:
    step = timedelta(minutes=10)
    start = datetime(2026, 10, 9, 12, 5, tzinfo=UTC)
    end = datetime(2026, 10, 9, 12, 30, tzinfo=UTC)
    edges = bin_edges(start, end, step)
    assert edges == [datetime(2026, 10, 9, 12, m, tzinfo=UTC) for m in (0, 10, 20)]
    assert bin_count(start, end, step) == len(edges)
    assert bin_count(end, start, step) == 0
    exact = datetime(2026, 10, 9, 12, 40, tzinfo=UTC)
    assert bin_count(datetime(2026, 10, 9, 12, 0, tzinfo=UTC), exact, step) == 4


def history_of(at: datetime, **fields: object) -> HistoryRow:
    values: dict[str, object] = {
        "account_id": ACCOUNT,
        "mower_id": MOWER,
        "observed_at": at,
        "activity": "mowing",
        "battery_percent": 80,
        "charging": False,
        "rtk_fix": "fixed",
        "connection": "simulated",
        **fields,
    }
    return HistoryRow(**values)  # type: ignore[arg-type]


def test_telemetry_downsample_bins_rows_and_positions() -> None:
    base = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    rows = [
        history_of(base + seconds(1), battery_percent=80, activity="docked", charging=True),
        history_of(base + seconds(4), battery_percent=75, activity="mowing", error_code="E7"),
        history_of(base + seconds(6), battery_percent=70, rtk_fix="float"),
        history_of(base + seconds(40), battery_percent=60),
    ]
    positions = [
        PositionRow(ACCOUNT, MOWER, base + seconds(2), 1.0, 2.0, "fixed", "base_station"),
        PositionRow(ACCOUNT, MOWER, base + seconds(3), 3.0, 4.0, "fixed", "base_station"),
    ]
    points = downsample(rows, positions, seconds(5))
    assert [p.at for p in points] == [base, base + seconds(5), base + seconds(40)]
    first, second, third = points
    assert (first.activity, first.battery_percent, first.battery_min, first.battery_max) == (
        "mowing",
        78,  # (80 + 75) / 2 = 77.5, rounded half up
        75,
        80,
    )
    assert first.charging is True and first.error_code == "E7" and first.samples == 2
    assert (first.lat, first.lon) == (3.0, 4.0)  # last position of the bin
    assert (second.rtk_fix, second.samples, second.lat) == ("float", 1, None)
    assert third.battery_percent == 60  # empty bins in between are left out


def test_telemetry_raw_points_are_single_samples() -> None:
    base = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    points = raw_points([history_of(base), history_of(base + seconds(1), battery_percent=70)])
    assert [(p.samples, p.battery_min, p.battery_max) for p in points] == [(1, 80, 80), (1, 70, 70)]
