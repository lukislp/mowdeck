"""Heatmaps: the pure binning (synthetic tracks), the use case over the memory store, the
position columns the ingest fills, and the route."""

import math
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import (
    GeoReference,
    MapArea,
    MapZone,
    MowerActivity,
    MowerCapabilities,
    MowerCapabilitiesSnapshot,
    MowerMap,
    MowerMapSnapshot,
)
from mowdeck_contracts.v1.subjects import device_subject
from mowdeck_control.geo import to_wgs84
from telemetry_support import API, T0, Harness, harness, make_state, seconds

from telemetry.adapters.memory import MemoryStore
from telemetry.application.context import TelemetryConfig
from telemetry.application.heatmap import (
    MAX_CELLS,
    Cell,
    HeatmapBuilder,
    Layer,
    LegendKind,
    cell_index,
)
from telemetry.application.queries import Queries
from telemetry.domain.errors import InvalidQueryError, NoSnapshotYetError
from telemetry.domain.models import PositionRow, SnapshotRow
from telemetry.domain.rules import position_row, signal_dbm

REF = GeoReference(origin_lat=48.1, origin_lon=11.5, rotation_deg=30)
ACCOUNT = uuid4()
MOWER = uuid4()
SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]


def row(
    x: float,
    y: float,
    t: float,
    *,
    blades_on: bool | None = True,
    rtk_fix: str = "fixed",
    wifi: int | None = None,
    mnet: int | None = None,
    mower_id: UUID = MOWER,
    account_id: UUID = ACCOUNT,
) -> PositionRow:
    """A track row at local ``(x, y)`` metres, ``t`` seconds after ``T0``."""
    lat, lon = to_wgs84(REF, x, y)
    return PositionRow(
        account_id=account_id,
        mower_id=mower_id,
        observed_at=T0 + timedelta(seconds=t),
        lat=lat,
        lon=lon,
        rtk_fix=rtk_fix,
        rtk_source="base_station",
        wifi_rssi=wifi,
        mnet_rssi=mnet,
        blades_on=blades_on,
    )


def swath(
    y: float, x0: float, x1: float, step: float, dt: float, t0: float = 0
) -> list[PositionRow]:
    """Positions along ``y`` from ``x0`` to ``x1`` every ``step`` metres, ``dt`` seconds apart."""
    count = round((x1 - x0) / step)
    return [row(x0 + i * step, y, t0 + i * dt) for i in range(count + 1)]


def a_map(zones: list[MapZone] | None = None) -> MowerMap:
    return MowerMap(
        reference=REF,
        areas=[MapArea(id="lawn", boundary=SQUARE)],
        no_go_zones=zones or [],
        revision="r1",
    )


def covered_area(cells: list[Cell], cell_m: float) -> float:
    return float(sum(c.value for c in cells)) * cell_m * cell_m


# --- pure binning ------------------------------------------------------------------------------


def test_heatmap_cell_indexes_floor_negative_coordinates() -> None:
    assert cell_index(-0.3, 1.0) == -1
    assert cell_index(-1.2, 1.0) == -2
    assert cell_index(0.0, 0.5) == 0
    assert cell_index(0.49, 0.5) == 0
    assert cell_index(-0.01, 2.0) == -1
    builder = HeatmapBuilder(Layer.WIFI, REF, 0.4)
    builder.add(row(-0.3, -1.2, 0, wifi=-60))
    cells = builder.build(1.0).cells
    assert [(c.x, c.y) for c in cells] == [(-1, -2)]


def test_heatmap_straight_swath_covers_a_strip_of_the_cutting_width() -> None:
    builder = HeatmapBuilder(Layer.COVERAGE, REF, 0.4)
    builder.add_all(swath(y=0.0, x0=0.0, x1=10.0, step=0.2, dt=1.0))
    result = builder.build(0.5)
    assert result.legend.kind is LegendKind.RATIO
    assert (result.legend.min, result.legend.max) == (0.0, 1.0)
    assert result.samples == 51
    assert {c.y for c in result.cells} == {-1, 0}  # the strip y in [-0.2, 0.2]
    assert all(0 < c.value <= 1 for c in result.cells)
    inner = {c.x: c.value for c in result.cells if c.y == 0 and 1 <= c.x <= 18}
    assert set(inner.values()) == {0.4}  # 0.2 m of every 0.5 m cell
    # 10 m x 0.4 m plus the round ends
    assert covered_area(result.cells, 0.5) == pytest.approx(4.0 + math.pi * 0.04, abs=0.15)
    assert sum(c.n for c in result.cells) == 51


def test_heatmap_positions_beyond_the_join_window_are_not_joined() -> None:
    discs = 11 * math.pi * 0.04
    # sparse cloud reports: 1 m and 20 s apart are inside the default window (30 s, 10 m)
    joined = HeatmapBuilder(Layer.COVERAGE, REF, 0.4)
    joined.add_all(swath(y=0.0, x0=0.0, x1=10.0, step=1.0, dt=20.0))
    assert covered_area(joined.build(0.5).cells, 0.5) == pytest.approx(4.1, abs=0.2)
    # 31 s apart: beyond the time limit
    late = HeatmapBuilder(Layer.COVERAGE, REF, 0.4)
    late.add_all(swath(y=0.0, x0=0.0, x1=10.0, step=1.0, dt=31.0))
    assert covered_area(late.build(0.5).cells, 0.5) == pytest.approx(discs, abs=0.4)
    # 8 m in 5 s is within the default 10 m, a straight swath between both points
    far = HeatmapBuilder(Layer.COVERAGE, REF, 0.4)
    far.add_all([row(0, 0, 0), row(8, 0, 5)])
    assert covered_area(far.build(0.5).cells, 0.5) == pytest.approx(3.2 + math.pi * 0.04, abs=0.2)
    # both limits must hold: a narrower window (2 s, 3 m) leaves discs for either reason
    for dt, step in ((3.0, 1.0), (1.0, 4.0)):
        narrow = HeatmapBuilder(Layer.COVERAGE, REF, 0.4, join_seconds=2, join_m=3)
        narrow.add_all(swath(y=0.0, x0=0.0, x1=10.0 * step, step=step, dt=dt))
        area = covered_area(narrow.build(0.5).cells, 0.5)
        assert area == pytest.approx(discs, abs=0.4)


def test_heatmap_coverage_ignores_blades_off_and_unknown_rows() -> None:
    builder = HeatmapBuilder(Layer.COVERAGE, REF, 0.4)
    builder.add_all(row(x, 0, x, blades_on=False) for x in range(10))
    builder.add_all(row(x, 5, 20 + x, blades_on=None) for x in range(10))  # before migration 0007
    result = builder.build(1.0, a_map())
    assert result.cells == [] and result.samples == 0
    assert len(result.gaps) == 1 and result.gaps[0].area_m2 == pytest.approx(100.0)


def test_heatmap_reads_rows_newest_first_like_oldest_first() -> None:
    track = swath(y=3.0, x0=0.0, x1=6.0, step=0.25, dt=1.0)
    forward = HeatmapBuilder(Layer.COVERAGE, REF, 0.4)
    forward.add_all(track)
    backward = HeatmapBuilder(Layer.COVERAGE, REF, 0.4)
    backward.add_all(reversed(track))
    assert forward.build(1.0).cells == backward.build(1.0).cells


def test_heatmap_gaps_of_a_square_area_with_one_swath() -> None:
    builder = HeatmapBuilder(Layer.COVERAGE, REF, 0.4)
    builder.add_all(swath(y=5.0, x0=-1.0, x1=11.0, step=0.2, dt=0.5))
    result = builder.build(1.0, a_map())
    assert len(result.gaps) == 2
    for gap in result.gaps:
        assert gap.area_m2 == pytest.approx(48.0, abs=0.1)  # 10 m x 4.8 m on each side
        ys = [y for _, y in gap.polygon]
        assert max(ys) <= 4.81 or min(ys) >= 5.19
        assert len(gap.polygon) >= 4 and gap.polygon[0] != gap.polygon[-1]


def test_heatmap_gaps_leave_out_no_go_zones_and_small_pieces() -> None:
    zone = MapZone(id="bed", boundary=[(0.0, 0.0), (10.0, 0.0), (10.0, 4.0), (0.0, 4.0)])
    patch = MapArea(id="patch", boundary=[(20.0, 20.0), (20.4, 20.0), (20.4, 20.4), (20.0, 20.4)])
    mower_map = a_map([zone]).model_copy(update={"areas": [*a_map().areas, patch]})
    builder = HeatmapBuilder(Layer.COVERAGE, REF, 0.4)
    builder.add_all(swath(y=5.0, x0=-1.0, x1=11.0, step=0.2, dt=0.5))
    result = builder.build(1.0, mower_map)
    # above the swath 10 m x 4.8 m; below 10 m x 0.8 m between the bed and the swath; the
    # uncut 0.16 m2 patch is below the minimum gap area
    assert [g.area_m2 for g in result.gaps] == [
        pytest.approx(48.0, abs=0.1),
        pytest.approx(8.0, abs=0.1),
    ]


def test_heatmap_wifi_median_per_cell_and_dbm_legend() -> None:
    builder = HeatmapBuilder(Layer.WIFI, REF, 0.4)
    builder.add_all(
        [
            row(0.1, 0.1, 0, wifi=-70),
            row(0.5, 0.5, 1, wifi=-60),
            row(0.9, 0.9, 2, wifi=-50),
            row(0.4, 0.4, 3, wifi=None),  # no reading: left out
            row(5.5, 0.5, 4, wifi=-80),
            row(5.6, 0.6, 5, wifi=-81),
            row(9.5, 9.5, 6, wifi=None, mnet=-90),
        ]
    )
    result = builder.build(1.0)
    assert [(c.x, c.y, c.value, c.n) for c in result.cells] == [(0, 0, -60.0, 3), (5, 0, -80.5, 2)]
    assert result.legend.kind is LegendKind.DBM
    assert (result.legend.min, result.legend.max) == (-80.5, -60.0)
    assert result.samples == 5 and result.gaps == []
    mnet = HeatmapBuilder(Layer.MNET, REF, 0.4)
    mnet.add_all([row(9.5, 9.5, 6, mnet=-90), row(0, 0, 7, wifi=-40)])
    assert [(c.x, c.y, c.value) for c in mnet.build(1.0).cells] == [(9, 9, -90.0)]
    empty = HeatmapBuilder(Layer.MNET, REF, 0.4).build(1.0)
    assert empty.cells == [] and (empty.legend.min, empty.legend.max) == (None, None)


def test_heatmap_rtk_share_of_fixed_samples() -> None:
    builder = HeatmapBuilder(Layer.RTK, REF, 0.4)
    builder.add_all(
        [
            row(0.2, 0.2, 0, rtk_fix="fixed"),
            row(0.3, 0.3, 1, rtk_fix="float"),
            row(0.4, 0.4, 2, rtk_fix="none"),
            row(0.5, 0.5, 3, rtk_fix="unknown"),  # no RTK information: left out
            row(3.5, 0.5, 4, rtk_fix="fixed"),
        ]
    )
    result = builder.build(1.0)
    assert [(c.x, c.y, c.value, c.n) for c in result.cells] == [(0, 0, 0.5, 3), (3, 0, 1.0, 1)]
    assert result.legend.kind is LegendKind.FIX and result.samples == 4


def test_heatmap_coarsens_until_the_cells_fit() -> None:
    builder = HeatmapBuilder(Layer.RTK, REF, 0.4)
    builder.add_all(row(x + 0.5, y + 0.5, x * 10 + y) for x in range(10) for y in range(10))
    assert len(builder.build(1.0).cells) == 100
    coarse = builder.build(0.5, max_cells=30)
    assert coarse.cell_m == 2.0 and len(coarse.cells) == 25
    assert sum(c.n for c in coarse.cells) == 100
    cover = HeatmapBuilder(Layer.COVERAGE, REF, 0.4)
    cover.add_all(swath(y=0.0, x0=0.0, x1=40.0, step=0.2, dt=1.0))
    assert cover.build(0.5).cell_m == 0.5
    small = cover.build(0.5, max_cells=50)  # 2 x 82 cells, 2 x 42, then 2 x 22
    assert small.cell_m == 2.0 and len(small.cells) == 44  # round ends reach x -0.2 and 40.2
    assert covered_area(small.cells, 2.0) == pytest.approx(16.1, abs=0.6)


def test_heatmap_rejects_bad_parameters() -> None:
    with pytest.raises(ValueError, match="cutting_width_m"):
        HeatmapBuilder(Layer.COVERAGE, REF, 0)
    with pytest.raises(ValueError, match="cell_m"):
        HeatmapBuilder(Layer.WIFI, REF, 0.4).build(0)


def test_heatmap_signal_and_blade_columns_from_the_state() -> None:
    assert signal_dbm({"wifi_rssi": -55}, "wifi_rssi") == -55
    assert signal_dbm({"wifi_rssi": -55.6}, "wifi_rssi") == -56
    for raw in ({}, {"wifi_rssi": 0}, {"wifi_rssi": 3}, {"wifi_rssi": True}, {"wifi_rssi": "x"}):
        assert signal_dbm(raw, "wifi_rssi") is None
    assert signal_dbm({"wifi_rssi": -151}, "wifi_rssi") is None
    mowing = make_state(
        MOWER,
        ACCOUNT,
        activity=MowerActivity.MOWING,
        position=(48.1, 11.5),
        raw={"wifi_rssi": -61, "mnet_rssi": 0},
    )
    stored = position_row(mowing)
    assert stored is not None
    assert (stored.wifi_rssi, stored.mnet_rssi, stored.blades_on) == (-61, None, True)
    transfer = mowing.model_copy(update={"raw": {"blade_on": False, "mnet_rssi": -95}})
    stored = position_row(transfer)
    assert stored is not None and (stored.blades_on, stored.mnet_rssi) == (False, -95)
    docked = make_state(MOWER, ACCOUNT, position=(48.1, 11.5))
    stored = position_row(docked)
    assert stored is not None and stored.blades_on is False


# --- use case over the memory store ------------------------------------------------------------


async def seed(
    store: MemoryStore,
    rows: list[PositionRow],
    *,
    mower_map: MowerMap | None = None,
    width_mm: int | None = None,
) -> None:
    async with store.transaction() as tx:
        for r in rows:
            await tx.add_position(r)
        if mower_map is not None:
            await tx.put_snapshot(
                SnapshotRow(MOWER, "map", ACCOUNT, T0, T0, "r1", mower_map.model_dump(mode="json"))
            )
        if width_mm is not None:
            caps = MowerCapabilities(cutting_width_mm=width_mm).model_dump(mode="json")
            await tx.put_snapshot(SnapshotRow(MOWER, "capabilities", ACCOUNT, T0, T0, "c1", caps))


def queries(store: MemoryStore, now: datetime, **config: object) -> Queries:
    return Queries(store, TelemetryConfig(**config), lambda: now)  # type: ignore[arg-type]


async def test_heatmap_query_needs_a_map_and_a_valid_cell() -> None:
    store = MemoryStore()
    q = queries(store, T0 + timedelta(hours=1))
    with pytest.raises(NoSnapshotYetError) as missing:
        await q.heatmap(ACCOUNT, MOWER, Layer.WIFI, None, None, 1.0)
    assert missing.value.kind == "map"
    with pytest.raises(InvalidQueryError, match=r"cell must be one of 0\.5, 1, 2"):
        await q.heatmap(ACCOUNT, MOWER, Layer.WIFI, None, None, 3.0)
    with pytest.raises(InvalidQueryError, match="at most 31 days"):
        await q.heatmap(ACCOUNT, MOWER, Layer.WIFI, T0 - timedelta(days=40), T0, 1.0)


async def test_heatmap_query_defaults_to_seven_days_and_the_capability_width() -> None:
    store = MemoryStore()
    old = [row(x, 0, -8 * 86400 + x) for x in range(5)]  # eight days back: outside the default
    track = swath(y=0.0, x0=0.0, x1=10.0, step=0.2, dt=1.0)
    await seed(store, old + track, mower_map=a_map(), width_mm=200)
    q = queries(store, T0 + timedelta(hours=1))
    result = await q.heatmap(ACCOUNT, MOWER, Layer.COVERAGE, None, None, 0.5)
    assert result.end - result.start == timedelta(days=7)
    narrow = queries(store, T0 + timedelta(hours=1), heatmap_join_seconds=0.5)
    discs = await narrow.heatmap(ACCOUNT, MOWER, Layer.COVERAGE, None, None, 0.5)
    assert covered_area(discs.heatmap.cells, 0.5) < covered_area(result.heatmap.cells, 0.5)
    assert result.heatmap.samples == 51 and not result.truncated
    assert covered_area(result.heatmap.cells, 0.5) == pytest.approx(2.0 + math.pi * 0.01, abs=0.1)
    assert result.mower_map.reference == REF
    with pytest.raises(NoSnapshotYetError):  # another account never sees the map
        await q.heatmap(uuid4(), MOWER, Layer.COVERAGE, None, None, 0.5)


async def test_heatmap_query_truncates_at_the_row_limit_keeping_the_newest() -> None:
    store = MemoryStore()
    rows = [row(float(i), 0.5, i, wifi=-40 - i) for i in range(1500)]
    await seed(store, rows, mower_map=a_map())
    q = queries(store, T0 + timedelta(hours=1), heatmap_max_rows=1000)
    result = await q.heatmap(ACCOUNT, MOWER, Layer.WIFI, T0, T0 + timedelta(hours=1), 1.0)
    assert result.truncated and result.heatmap.samples == 1000
    assert min(c.x for c in result.heatmap.cells) == 500  # the oldest 500 were cut off
    whole = queries(store, T0 + timedelta(hours=1), heatmap_max_rows=1500)
    full = await whole.heatmap(ACCOUNT, MOWER, Layer.WIFI, T0, T0 + timedelta(hours=1), 1.0)
    assert not full.truncated and full.heatmap.samples == 1500


async def test_heatmap_store_streams_newest_first_in_chunks() -> None:
    store = MemoryStore()
    await seed(store, [row(i, 0, i) for i in range(12)])
    chunks = [
        chunk
        async for chunk in store.position_chunks(
            ACCOUNT, MOWER, T0, T0 + seconds(100), limit=10, chunk_size=4
        )
    ]
    assert [len(c) for c in chunks] == [4, 4, 2]
    times = [r.observed_at for c in chunks for r in c]
    assert times == sorted(times, reverse=True) and times[0] == T0 + seconds(11)


def test_heatmap_cell_cap_is_twenty_thousand() -> None:
    assert MAX_CELLS == 20_000


# --- route -------------------------------------------------------------------------------------


async def publish_device(h: Harness, mower_id: UUID, account_id: UUID) -> None:
    mower_map = a_map()
    await h.bus.publish(
        device_subject(mower_id, "map"),
        MowerMapSnapshot(mower_id=mower_id, account_id=account_id, observed_at=T0, map=mower_map),
    )
    await h.bus.publish(
        device_subject(mower_id, "capabilities"),
        MowerCapabilitiesSnapshot(
            mower_id=mower_id,
            account_id=account_id,
            observed_at=T0,
            capabilities=MowerCapabilities(cutting_width_mm=400),
        ),
    )


async def test_heatmap_route_serves_layers_from_published_states() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        url = f"{API}/mowers/{mower_id}/heatmap"
        params = {
            "layer": "coverage",
            "from": T0.isoformat(),
            "to": (T0 + timedelta(hours=1)).isoformat(),
        }
        no_map = await h.client.get(url, headers=owner.headers, params=params)
        await publish_device(h, mower_id, owner.account_id)
        for i in range(51):
            lat, lon = to_wgs84(REF, i * 0.2, 5.0)
            await h.publish_state(
                make_state(
                    mower_id,
                    owner.account_id,
                    at=T0 + seconds(i),
                    activity=MowerActivity.MOWING,
                    position=(lat, lon),
                    raw={"wifi_rssi": -50 - i % 3, "mnet_rssi": -90},
                )
            )
        coverage = await h.client.get(url, headers=owner.headers, params=params)
        wifi = await h.client.get(
            url, headers=owner.headers, params={**params, "layer": "wifi", "cell": "2"}
        )
        bad_cell = await h.client.get(url, headers=owner.headers, params={**params, "cell": "3"})
        bad_layer = await h.client.get(url, headers=owner.headers, params={**params, "layer": "x"})
        stranger = await h.owner()
        foreign = await h.client.get(url, headers=stranger.headers, params=params)
    assert no_map.status_code == 404
    assert no_map.json()["type"] == "urn:mowdeck:problem:no-map-yet"
    assert coverage.status_code == 200, coverage.text
    body = coverage.json()
    assert set(body) == {
        "mower_id",
        "layer",
        "cell_m",
        "from",
        "to",
        "reference",
        "cells",
        "legend",
        "gaps",
        "samples",
        "truncated",
    }
    assert body["layer"] == "coverage" and body["cell_m"] == 1.0
    assert body["reference"] == {"origin_lat": 48.1, "origin_lon": 11.5, "rotation_deg": 30.0}
    assert body["legend"] == {"kind": "ratio", "min": 0.0, "max": 1.0}
    assert body["samples"] == 51 and body["truncated"] is False
    assert {c["y"] for c in body["cells"]} == {4, 5}
    assert set(body["cells"][0]) == {"x", "y", "value", "n"}
    assert [round(g["area_m2"]) for g in body["gaps"]] == [48, 48]
    assert wifi.status_code == 200
    wifi_body = wifi.json()
    assert wifi_body["cell_m"] == 2.0 and wifi_body["legend"]["kind"] == "dbm"
    assert wifi_body["legend"]["min"] <= wifi_body["legend"]["max"] <= -50
    assert bad_cell.status_code == 422 and bad_layer.status_code == 422
    assert foreign.status_code == 404
