"""The iOS widget endpoints (ADR 0059): compact state and the map picture."""

import struct
import zlib
from datetime import UTC, datetime
from uuid import UUID

from mowdeck_contracts.v1 import (
    DockPose,
    GeoReference,
    JobProgress,
    MapArea,
    MapZone,
    MowerActivity,
    MowerMap,
    MowerMapSnapshot,
    MowPath,
    MowPathProgress,
)
from mowdeck_contracts.v1.subjects import device_subject
from telemetry_support import API, T0, Harness, harness, make_state
from test_telemetry_path import publish_path

from telemetry.application import mapimage
from telemetry.application.widget import MAX_RENDERS


def a_map() -> MowerMap:
    return MowerMap(
        reference=GeoReference(origin_lat=48.1, origin_lon=11.5),
        areas=[
            MapArea(id="front", name="Großer Garten", boundary=[(0, 0), (30, 0), (30, 20), (0, 20)])
        ],
        no_go_zones=[MapZone(id="pond", boundary=[(10, 5), (14, 5), (14, 9), (10, 9)])],
        dock=DockPose(x=1.0, y=1.0),
        revision="map-1",
    )


async def publish_map(h: Harness, mower_id: UUID, account_id: UUID) -> None:
    await h.bus.publish(
        device_subject(mower_id, "map"),
        MowerMapSnapshot(mower_id=mower_id, account_id=account_id, observed_at=T0, map=a_map()),
    )


def pixels(png: bytes) -> list[list[tuple[int, int, int]]]:
    """Decode the PNGs this service writes (8 bit RGB, filter 0)."""
    width, height = struct.unpack(">II", png[16:24])
    pos, data = 8, b""
    while pos < len(png):
        length = struct.unpack(">I", png[pos : pos + 4])[0]
        if png[pos + 4 : pos + 8] == b"IDAT":
            data += png[pos + 8 : pos + 8 + length]
        pos += 12 + length
    raw = zlib.decompress(data)
    stride = 1 + 3 * width
    rows = []
    for y in range(height):
        assert raw[y * stride] == 0
        row = raw[y * stride + 1 : (y + 1) * stride]
        rows.append([(row[3 * x], row[3 * x + 1], row[3 * x + 2]) for x in range(width)])
    return rows


async def test_widget_state_is_compact_and_names_the_area() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        url = f"{API}/mowers/{mower_id}/widget"
        before = await h.client.get(url, headers=owner.headers)
        await publish_map(h, mower_id, owner.account_id)
        await h.publish_state(
            make_state(
                mower_id,
                owner.account_id,
                at=datetime.now(UTC),
                activity=MowerActivity.MOWING,
                battery=64,
                job=JobProgress(progress_percent=42, area_id="front", remaining_s=1800),
            )
        )
        mowing = await h.client.get(url, headers=owner.headers)
        old_id = await h.add_mower(owner)
        await h.publish_state(
            make_state(old_id, owner.account_id, at=T0, activity=MowerActivity.DOCKED)
        )
        stale = await h.client.get(f"{API}/mowers/{old_id}/widget", headers=owner.headers)
        foreign = await h.client.get(url, headers=(await h.owner()).headers)
        anonymous = await h.client.get(url)
    assert before.status_code == 404
    assert before.json()["type"] == "urn:mowdeck:problem:no-telemetry-yet"
    body = mowing.json()
    assert mowing.headers["cache-control"] == "private, max-age=15"
    assert body["activity"] == "mowing" and body["battery_percent"] == 64
    assert body["job"]["progress_percent"] == 42
    assert body["job"]["area_name"] == "Großer Garten"
    assert body["job"]["remaining_s"] == 1800
    assert body["stale"] is False
    assert stale.json()["activity"] == "offline" and stale.json()["stale"] is True
    assert foreign.status_code == 404
    assert anonymous.status_code == 401


async def test_widget_state_reports_the_error_while_in_error() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.publish_state(
            make_state(
                mower_id,
                owner.account_id,
                at=datetime.now(UTC),
                activity=MowerActivity.ERROR,
                error_code="1001",
                error_message="Lifted",
            )
        )
        body = (await h.client.get(f"{API}/mowers/{mower_id}/widget", headers=owner.headers)).json()
    assert body["activity"] == "error"
    assert (body["error_code"], body["error_message"]) == ("1001", "Lifted")
    assert body["job"] is None


async def test_widget_map_is_a_bounded_cached_png_with_the_mower_on_it() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        url = f"{API}/mowers/{mower_id}/widget-map.png"
        no_map = await h.client.get(url, headers=owner.headers)
        await publish_map(h, mower_id, owner.account_id)
        await h.publish_state(
            make_state(
                mower_id,
                owner.account_id,
                at=datetime.now(UTC),
                activity=MowerActivity.MOWING,
                position=(48.1 + 10 / 111_320, 11.5 + 15 / (111_320 * 0.667)),
            )
        )
        await publish_path(
            h,
            mower_id,
            owner.account_id,
            MowPath(
                path_hash="1",
                polylines=[[(2.0, 2.0), (28.0, 2.0)], [(28.0, 4.0), (2.0, 4.0)]],
                kinds=["mow", "mow"],
                points_total=4,
                progress=MowPathProgress(index=2),
                planned_at=T0,
            ),
        )
        first = await h.client.get(f"{url}?width=320&height=192", headers=owner.headers)
        etag = first.headers["etag"]
        again = await h.client.get(
            f"{url}?width=320&height=192", headers={**owner.headers, "If-None-Match": etag}
        )
        dark = await h.client.get(f"{url}?width=320&height=192&theme=dark", headers=owner.headers)
        huge = await h.client.get(f"{url}?width=5000", headers=owner.headers)
        bad_theme = await h.client.get(f"{url}?theme=sepia", headers=owner.headers)
        foreign = await h.client.get(url, headers=(await h.owner()).headers)
        anonymous = await h.client.get(url)
    assert no_map.status_code == 404
    assert no_map.json()["type"] == "urn:mowdeck:problem:no-map-yet"
    assert first.status_code == 200 and first.headers["content-type"] == "image/png"
    assert first.headers["cache-control"] == "private, max-age=30"
    assert mapimage.decode_png_size(first.content) == (320, 192)
    assert again.status_code == 304 and again.content == b""
    assert dark.headers["etag"] != etag
    assert pixels(dark.content)[0][0] == mapimage.PALETTES["dark"].background
    flat = [p for row in pixels(first.content) for p in row]
    assert mapimage.PALETTES["light"].background in flat
    assert any(abs(p[0] - 46) < 14 and abs(p[1] - 125) < 14 for p in flat)  # mowed path
    assert any(p[2] > 230 and p[0] < 40 for p in flat)  # the mower marker
    assert huge.status_code == 422 and bad_theme.status_code == 422
    assert foreign.status_code == 404 and anonymous.status_code == 401


async def test_widget_map_without_a_path_works_and_answers_503_when_all_slots_are_busy() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await publish_map(h, mower_id, owner.account_id)
        url = f"{API}/mowers/{mower_id}/widget-map.png"
        ok = await h.client.get(url, headers=owner.headers)
        widget = h.app.state.telemetry.widget
        for _ in range(MAX_RENDERS):
            await widget._slots.acquire()
        busy = await h.client.get(f"{url}?width=330", headers=owner.headers)
        cached = await h.client.get(url, headers=owner.headers)  # served from the cache
    assert ok.status_code == 200
    assert busy.status_code == 503
    assert cached.status_code == 200


def test_the_renderer_clamps_sizes_and_draws_huge_scenes_within_bounds() -> None:
    w, h = mapimage.clamp_size(5000, 5000)
    assert w * h <= mapimage.MAX_PIXELS and w <= mapimage.MAX_WIDTH and h <= mapimage.MAX_HEIGHT
    assert mapimage.clamp_size(1, 1) == (mapimage.MIN_WIDTH, mapimage.MIN_HEIGHT)
    line = [(float(i % 400), float(i // 400)) for i in range(60_000)]  # beyond MAX_POINTS
    scene = mapimage.MapScene(
        areas=[[(0, 0), (400, 0), (400, 150), (0, 150)]], mowed=[line], mower=(5.0, 5.0)
    )
    png = mapimage.render(scene, 320, 192, "light")
    assert mapimage.decode_png_size(png) == (320, 192)
    empty = mapimage.render(mapimage.MapScene(), 160, 96, "dark")
    assert {p for row in pixels(empty) for p in row} == {mapimage.PALETTES["dark"].background}
