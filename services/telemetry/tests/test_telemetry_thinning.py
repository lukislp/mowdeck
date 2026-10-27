"""Position downsampling (ADR 0053): the pure policy, the nightly window (DST), the retention
round over the memory store, settings, and the heatmap on thinned data."""

import itertools
import math
import random
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from mowdeck_contracts.v1 import GeoReference, MapArea, MowerCapabilities, MowerMap
from mowdeck_control.geo import to_wgs84
from pydantic import ValidationError
from telemetry_support import make_settings

from telemetry.adapters.memory import MemoryStore
from telemetry.application.context import TelemetryConfig
from telemetry.application.heatmap import Layer
from telemetry.application.ports import RetentionTarget
from telemetry.application.queries import Queries
from telemetry.application.retention import Retention
from telemetry.domain.models import DirectoryEntry, HistoryRow, PositionRow, SnapshotRow
from telemetry.domain.thinning import (
    ThinPolicy,
    floor_to_grid,
    in_window,
    keep_set,
    slice_end,
    thinned_join_seconds,
)

ACCOUNT, MOWER = uuid4(), uuid4()
REF = GeoReference(origin_lat=48.1, origin_lon=11.5, rotation_deg=30)
BERLIN = ZoneInfo("Europe/Berlin")
BASE = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)  # on the 5 minute grid and the 6 hour boundary


def point(
    t: float,
    x: float = 0.0,
    y: float = 0.0,
    *,
    blades: bool | None = False,
    fix: str = "fixed",
    mower: UUID = MOWER,
    base: datetime = BASE,
) -> PositionRow:
    lat, lon = to_wgs84(REF, x, y)
    return PositionRow(
        account_id=ACCOUNT,
        mower_id=mower,
        observed_at=base + timedelta(seconds=t),
        lat=lat,
        lon=lon,
        rtk_fix=fix,
        rtk_source="base_station",
        blades_on=blades,
    )


def a_day() -> list[PositionRow]:
    """Docked for 20 min (a row every 10 s), a 10 minute mowing run at 0.5 m/s with a row every
    second, docked again for 20 min."""
    rows = [point(t) for t in range(0, 1200, 10)]
    rows += [point(1200 + s, x=0.5 * s, blades=True) for s in range(600)]
    rows += [point(1800 + t, x=300.0) for t in range(0, 1200, 10)]
    return rows


def thinned(
    rows: list[PositionRow], policy: ThinPolicy | None = None, **kw: object
) -> set[datetime]:
    return keep_set(rows, BASE, BASE + timedelta(hours=6), policy or ThinPolicy(), **kw)  # type: ignore[arg-type]


# --- the policy --------------------------------------------------------------------------------


def test_thinning_keeps_one_row_per_bin_and_the_edges() -> None:
    rows = a_day()
    kept = thinned(rows)
    times = sorted(int((at - BASE).total_seconds()) for at in kept)
    run = [t for t in times if 1200 <= t < 1800]
    assert 1200 in run and 1799 in run  # the first and the last row of the job
    assert len(run) <= 600 // 30 + 3
    assert all(b - a <= 31 for a, b in itertools.pairwise(run))  # joins stay possible
    idle = [t for t in times if t < 1200 or t >= 1800]
    # docked: 5 minute bins (4 + 4) plus the edges next to the job and of the data
    assert len(idle) <= 8 + 4
    assert 1190 in idle and 1800 in idle  # the rows beside the blade change survive
    assert len(kept) < len(rows) / 8


def test_thinning_keeps_rows_next_to_a_gap_a_lost_fix_and_error_instants() -> None:
    rows = [point(t) for t in range(0, 200, 10)]
    rows += [point(t) for t in range(1000, 1100, 10)]  # a 13 min silence in between
    kept = thinned(rows)
    assert BASE + timedelta(seconds=190) in kept and BASE + timedelta(seconds=1000) in kept
    fix = [point(t, fix="fixed" if t < 505 else "none") for t in range(300, 700, 10)]
    kept_fix = thinned(fix)
    assert BASE + timedelta(seconds=500) in kept_fix and BASE + timedelta(seconds=510) in kept_fix
    quiet = [point(t) for t in range(300, 700, 10)]
    error_at = BASE + timedelta(seconds=470)
    assert error_at not in thinned(quiet)
    assert error_at in thinned(quiet, error_times=[error_at])


def test_thinning_treats_moving_without_blades_as_active_and_standing_as_idle() -> None:
    transit = [point(s, x=1.5 * s) for s in range(120)]  # driving at 1.5 m/s, blades off
    parked = [point(s) for s in range(120)]
    assert len(thinned(transit)) > len(thinned(parked))
    assert len(thinned(parked)) <= 5  # one bin plus the first and the last row


def test_thinning_policy_and_helpers_validate() -> None:
    with pytest.raises(ValueError, match="at least as large"):
        ThinPolicy(active_seconds=60, idle_seconds=30)
    with pytest.raises(ValueError, match="divide"):
        ThinPolicy(active_seconds=7)
    assert thinned_join_seconds(ThinPolicy(), 30) == 65
    assert thinned_join_seconds(ThinPolicy(), 120) == 120
    assert floor_to_grid(BASE + timedelta(seconds=299)) == BASE
    assert slice_end(BASE + timedelta(hours=1), BASE + timedelta(days=1)) == BASE + timedelta(
        hours=6
    )
    assert slice_end(BASE + timedelta(hours=5, minutes=55), BASE + timedelta(hours=7)) == (
        BASE + timedelta(hours=6)
    )
    assert slice_end(BASE, BASE + timedelta(hours=2)) == BASE + timedelta(hours=2)


# --- the nightly window and DST ----------------------------------------------------------------


def test_window_follows_local_time() -> None:
    # Winter (UTC+1): the window 1 to 5 local is 00:00 to 04:00 UTC.
    assert not in_window(datetime(2026, 1, 14, 23, 30, tzinfo=UTC), BERLIN, 1, 5)  # 00:30 local
    assert in_window(datetime(2026, 1, 15, 0, 30, tzinfo=UTC), BERLIN, 1, 5)  # 01:30 local
    assert in_window(datetime(2026, 1, 15, 3, 59, tzinfo=UTC), BERLIN, 1, 5)  # 04:59 local
    assert not in_window(datetime(2026, 1, 15, 4, 0, tzinfo=UTC), BERLIN, 1, 5)  # 05:00 local
    # Summer (UTC+2): the same window is 23:00 to 03:00 UTC.
    assert not in_window(datetime(2026, 7, 14, 22, 30, tzinfo=UTC), BERLIN, 1, 5)  # 00:30 local
    assert in_window(datetime(2026, 7, 14, 23, 30, tzinfo=UTC), BERLIN, 1, 5)  # 01:30 local
    assert not in_window(datetime(2026, 7, 15, 3, 0, tzinfo=UTC), BERLIN, 1, 5)  # 05:00 local


def open_utc_hours(first: datetime, hours: int, start: int, end: int) -> list[int]:
    """The UTC hours (as offsets from ``first``) in which the Berlin window is open."""
    return [h for h in range(hours) if in_window(first + timedelta(hours=h), BERLIN, start, end)]


def test_window_on_dst_nights() -> None:
    # Spring forward 2026-03-29 (01:00 UTC): local 02:00 jumps to 03:00. From local midnight
    # (23:00 UTC) the window 1 to 5 is open for 00:00 to 03:00 UTC, i.e. four real hours (01:00
    # CET to 05:00 CEST), plus the 23:00 UTC instant (local 00:00 is closed).
    spring = open_utc_hours(datetime(2026, 3, 28, 23, tzinfo=UTC), 12, 1, 5)
    assert spring == [1, 2, 3]  # 00:00, 01:00 and 02:00 UTC
    # Fall back 2026-10-25 (01:00 UTC): local 03:00 repeats as 02:00. 01:00 to 05:00 local is five
    # real hours here: 23:00 UTC (01:00 CEST) up to 03:00 UTC (04:00 CET).
    fall = open_utc_hours(datetime(2026, 10, 24, 22, tzinfo=UTC), 12, 1, 5)
    assert fall == [1, 2, 3, 4, 5]
    # A window around the skipped hour (2 to 3) is simply never entered in spring ...
    assert open_utc_hours(datetime(2026, 3, 28, 23, tzinfo=UTC), 12, 2, 3) == []
    # ... and entered twice in autumn (02:xx CEST and 02:xx CET).
    assert open_utc_hours(datetime(2026, 10, 24, 22, tzinfo=UTC), 12, 2, 3) == [2, 3]
    assert in_window(BASE, BERLIN, 3, 3)  # equal: always


def test_window_may_wrap_midnight() -> None:
    for hour, expected in ((21, False), (22, True), (23, True), (0, True), (4, True), (5, False)):
        at = datetime(2026, 1, 15, hour, 0, tzinfo=UTC)
        assert in_window(at, ZoneInfo("UTC"), 22, 5) is expected


# --- the retention round over the memory store -------------------------------------------------


async def seed(store: MemoryStore, rows: list[PositionRow]) -> None:
    async with store.transaction() as tx:
        for r in rows:
            await tx.add_position(r)
        await tx.put_directory(DirectoryEntry(MOWER, ACCOUNT, BASE), replace=True)


def retention(
    store: MemoryStore, now: datetime, ticks: list[float] | None = None, **config: object
) -> Retention:
    values: dict[str, object] = {"thinning_window_start_hour": 0, "thinning_window_end_hour": 0}
    values.update(config)
    clock = iter(ticks or [])
    return Retention(
        store,
        TelemetryConfig(**values),  # type: ignore[arg-type]
        lambda: now,
        monotonic=(lambda: next(clock)) if ticks else (lambda: 0.0),
    )


def remaining(store: MemoryStore) -> set[datetime]:
    return {at for (_, at) in store.data.positions}


async def test_round_thins_old_rows_but_never_the_detail_window() -> None:
    store = MemoryStore()
    old = a_day()
    recent_base = BASE + timedelta(days=100)
    recent = [point(t, base=recent_base) for t in range(0, 1200, 10)]
    await seed(store, old + recent)
    now = recent_base + timedelta(hours=1)  # BASE is 100 days old, the recent rows are new
    report = await retention(store, now).thin_once()
    assert report is not None and report.complete
    assert report.deleted > 0 and report.scanned == len(old)
    assert {r.observed_at for r in recent} <= remaining(store)  # untouched
    assert len(remaining(store)) == len(recent) + len(old) - report.deleted
    assert store.data.thinned_until[MOWER] == floor_to_grid(now - timedelta(days=90))


async def test_round_is_idempotent_and_picks_up_where_it_stopped() -> None:
    store = MemoryStore()
    await seed(store, a_day())
    now = BASE + timedelta(days=120)
    first = await retention(store, now).thin_once()
    snapshot = remaining(store)
    second = await retention(store, now).thin_once()
    assert first is not None and first.deleted > 0
    assert second is not None and second.deleted == 0 and second.slices == 0
    assert remaining(store) == snapshot
    # later the watermark moves on: new rows that aged into the thinning range are processed
    late = [point(t, base=BASE + timedelta(days=31)) for t in range(0, 1200, 10)]
    await seed(store, late)
    third = await retention(store, BASE + timedelta(days=125)).thin_once()
    assert third is not None and third.deleted > 0 and third.mowers == 1


async def test_round_outside_the_window_or_disabled_or_locked_out_does_nothing() -> None:
    store = MemoryStore()
    await seed(store, a_day())
    now = datetime(2026, 5, 20, 10, 0, tzinfo=UTC)  # 10:00 UTC, window 1 to 5
    closed = retention(store, now, thinning_window_start_hour=1, thinning_window_end_hour=5)
    assert await closed.thin_once() is None
    assert await retention(store, now, thinning_enabled=False).thin_once() is None
    store.lock_held_elsewhere = True
    assert await retention(store, now).thin_once() is None
    store.lock_held_elsewhere = False
    assert len(remaining(store)) == len(a_day())


async def test_round_stops_at_its_time_budget_and_resumes() -> None:
    store = MemoryStore()
    rows = [r for day in range(4) for r in a_day_at(day)]
    await seed(store, rows)
    now = BASE + timedelta(days=94)  # thin up to BASE + 4 days: 16 slices of 6 hours
    # monotonic: began=0, then 0 for the first slice, 100 (over a 50 s budget) afterwards
    budget = {"thinning_budget": timedelta(seconds=50)}
    cut = await retention(store, now, ticks=[0.0, 0.0, 100.0, 100.0], **budget).thin_once()
    assert cut is not None and not cut.complete and cut.slices == 1
    done = await retention(store, now).thin_once()
    assert done is not None and done.complete and done.slices > 0
    assert done.slices + cut.slices == 4 * 4  # four days of four 6 hour slices, each exactly once


def a_day_at(day: int) -> list[PositionRow]:
    base = BASE + timedelta(days=day)
    return [point(t, base=base) for t in range(0, 1200, 10)]


async def test_round_stops_when_the_night_ends() -> None:
    store = MemoryStore()
    await seed(store, a_day())
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1  # the window check, the limit and the first slice see 04:59, then it is 05:00
        return datetime(2026, 5, 20, 4 if calls <= 3 else 5, 59 if calls <= 3 else 0, tzinfo=UTC)

    config = TelemetryConfig(thinning_window_start_hour=1, thinning_window_end_hour=5)
    report = await Retention(store, config, clock).thin_once()
    assert report is not None and report.slices == 1 and not report.complete
    assert await Retention(store, config, clock).thin_once() is None  # 05:00 is outside [1, 5)


async def test_positions_are_deleted_after_the_horizon_and_zero_keeps_them_forever() -> None:
    store = MemoryStore()
    await seed(store, [point(0), point(60, base=BASE + timedelta(days=100))])
    now = BASE + timedelta(days=900)
    forever = await retention(store, now, retention_positions=timedelta(0)).run_once()
    assert forever is not None and RetentionTarget.POSITIONS not in forever
    assert len(remaining(store)) == 2
    gone = await retention(store, now).run_once()  # default: 730 days
    assert gone is not None and sum(gone.values()) == 2 and not remaining(store)


async def test_error_history_instants_survive_in_the_memory_store() -> None:
    store = MemoryStore()
    rows = [point(t) for t in range(300, 700, 10)]
    await seed(store, rows)
    at = BASE + timedelta(seconds=470)
    store.data.history[(MOWER, at)] = HistoryRow(
        account_id=ACCOUNT,
        mower_id=MOWER,
        observed_at=at,
        activity="error",
        battery_percent=50,
        charging=False,
        rtk_fix="fixed",
        connection="simulated",
        error_code="E42",
    )
    await retention(store, BASE + timedelta(days=200)).thin_once()
    assert at in remaining(store)


# --- settings ----------------------------------------------------------------------------------


def test_settings_validate_the_positions_policy() -> None:
    assert make_settings(telemetry_retention_positions_days=0).to_config().retention_positions == (
        timedelta(0)
    )
    with pytest.raises(ValidationError, match="at least the detail days"):
        make_settings(telemetry_retention_positions_days=30)
    with pytest.raises(ValidationError, match="unknown time zone"):
        make_settings(telemetry_maintenance_timezone="Mars/Olympus")
    with pytest.raises(ValidationError, match="divide"):
        make_settings(telemetry_positions_thin_active_seconds=7)
    with pytest.raises(ValidationError):
        make_settings(telemetry_positions_detail_days=1)
    config = make_settings(
        telemetry_maintenance_timezone="Europe/Berlin", telemetry_thinning_window_start_hour=2
    ).to_config()
    assert config.thinning_zone == "Europe/Berlin" and config.thinning_window_start_hour == 2


# --- heatmaps on thinned data ------------------------------------------------------------------


def lawn_track(base: datetime) -> list[PositionRow]:
    """Eight parallel 40 m swaths, 1 row per second at 0.5 m/s, blades on; 0.8 m between rows."""
    rows: list[PositionRow] = []
    t = 0.0
    for lane in range(8):
        xs = [0.5 * s for s in range(80)]
        for x in xs if lane % 2 == 0 else reversed(xs):
            rows.append(point(t, x=x, y=lane * 0.8, blades=True, base=base))
            t += 1
        t += 4
    return rows


async def covered(store: MemoryStore, now: datetime, **config: object) -> float:
    mower_map = MowerMap(
        reference=REF,
        areas=[MapArea(id="a", boundary=[(0, 0), (50, 0), (50, 8), (0, 8)])],
        revision="r1",
    )
    async with store.transaction() as tx:
        await tx.put_snapshot(
            SnapshotRow(MOWER, "map", ACCOUNT, now, now, "r1", mower_map.model_dump(mode="json"))
        )
        caps = MowerCapabilities(cutting_width_mm=400).model_dump(mode="json")
        await tx.put_snapshot(SnapshotRow(MOWER, "capabilities", ACCOUNT, now, now, "c", caps))
    q = Queries(store, TelemetryConfig(**config), lambda: now)  # type: ignore[arg-type]
    result = await q.heatmap(
        ACCOUNT, MOWER, Layer.COVERAGE, BASE - timedelta(hours=1), BASE + timedelta(hours=1), 0.5
    )
    return float(sum(c.value for c in result.heatmap.cells)) * 0.25


async def test_heatmap_on_thinned_data_keeps_the_coverage() -> None:
    now = BASE + timedelta(days=120)
    full = MemoryStore()
    await seed(full, lawn_track(BASE))
    reference = await covered(full, now)
    assert reference > 100  # the lawn is mostly covered

    thin = MemoryStore()
    await seed(thin, lawn_track(BASE))
    report = await retention(thin, now).thin_once()
    assert report is not None and report.deleted > len(lawn_track(BASE)) * 0.5
    assert covered_ok(await covered(thin, now), reference)

    # without the wider join of thinned rows the swaths would fall apart into discs
    naive = await covered(thin, now, thinning_enabled=False)
    assert naive < reference * 0.8


def covered_ok(value: float, reference: float) -> bool:
    return math.isclose(value, reference, rel_tol=0.12)


def test_thinned_rows_stay_within_the_widened_join() -> None:
    rows = lawn_track(BASE)
    kept = sorted(keep_set(rows, BASE, BASE + timedelta(hours=1), ThinPolicy()))
    swath = [at for at in kept if (at - BASE).total_seconds() < 80]
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(swath)]
    assert max(gaps) <= thinned_join_seconds(ThinPolicy(), 30)


def test_random_tracks_never_lose_blade_edges() -> None:
    generator = random.Random(53)  # noqa: S311
    for _ in range(25):
        rows: list[PositionRow] = []
        t, blades = 0.0, False
        for _ in range(300):
            t += generator.choice([1, 1, 2, 10, 40])
            if generator.random() < 0.05:
                blades = not blades
            rows.append(point(t, x=t * 0.3, blades=blades))
        kept = keep_set(rows, BASE, BASE + timedelta(hours=6), ThinPolicy())
        for before, row, after in zip(rows, rows[1:], rows[2:], strict=False):
            if row.blades_on != before.blades_on or row.blades_on != after.blades_on:
                assert row.observed_at in kept
