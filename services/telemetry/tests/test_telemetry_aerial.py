"""The aerial photo layer: tile and bounding box geometry, the use case (bounds, zoom range,
cache, rate limit) and the routes (auth, no open proxy, errors)."""

import math
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import (
    DockPose,
    GeoReference,
    MapArea,
    MapPath,
    MowerMap,
    MowerMapSnapshot,
)
from mowdeck_contracts.v1.subjects import device_subject
from mowdeck_control.geo import to_wgs84
from telemetry_support import API, T0, Harness, harness

from telemetry.adapters.aerial import DiskTileCache, HttpTileSource, sniff_media_type
from telemetry.application.aerial import AerialConfig, FetchedTile, MissLimiter
from telemetry.domain.aerial import (
    WORLD_HALF_M,
    Bounds,
    map_bounds,
    mercator_to_wgs84,
    tile_bounds,
    tile_bounds_3857,
    tile_in_range,
    wgs84_to_mercator,
    wms_get_map_url,
    xyz_url,
)
from telemetry.domain.errors import AerialUpstreamError

# Marienplatz, Munich (ETRS89/WGS84): the tile that holds it at zoom 17 by the slippy map
# formulas of the OSM wiki, computed here independently of the code under test.
LAT, LON = 48.137154, 11.576124
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def slippy(lat: float, lon: float, z: int) -> tuple[int, int]:
    n = 2**z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


# --- geometry ---------------------------------------------------------------------------------


def test_tile_bounds_of_the_world_and_its_quadrants() -> None:
    assert tile_bounds_3857(0, 0, 0) == (-WORLD_HALF_M, -WORLD_HALF_M, WORLD_HALF_M, WORLD_HALF_M)
    # z=1: x grows east, y grows south; tile 1/1/0 is the north-eastern quadrant
    assert tile_bounds_3857(1, 1, 0) == (0.0, 0.0, WORLD_HALF_M, WORLD_HALF_M)
    assert tile_bounds_3857(1, 0, 1) == (-WORLD_HALF_M, -WORLD_HALF_M, 0.0, 0.0)


def test_mercator_known_points_match_published_values() -> None:
    # EPSG:3857 of lon 11 / lat 48 (fixture from proj: 1224514.3987, 6106854.8339)
    x, y = wgs84_to_mercator(48.0, 11.0)
    assert x == pytest.approx(1224514.3987, abs=0.01)
    assert y == pytest.approx(6106854.8339, abs=0.01)
    lat, lon = mercator_to_wgs84(x, y)
    assert (lat, lon) == pytest.approx((48.0, 11.0), abs=1e-9)


def test_a_point_lies_in_the_tile_the_slippy_formula_names() -> None:
    for z in (14, 17, 19):
        x, y = slippy(LAT, LON, z)
        box = tile_bounds(z, x, y)
        assert box.south < LAT < box.north
        assert box.west < LON < box.east
        # the neighbours do not contain it
        assert not (tile_bounds(z, x + 1, y).west < LON < tile_bounds(z, x + 1, y).east)


def test_wms_request_uses_the_exact_mercator_box_of_the_tile() -> None:
    x, y = slippy(LAT, LON, 17)
    url = wms_get_map_url(
        "https://geoservices.bayern.de/od/wms/dop/v1/dop40",
        layer="by_dop40c",
        image_format="image/jpeg",
        version="1.3.0",
        z=17,
        x=x,
        y=y,
        size=256,
    )
    parts = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert parts.netloc == "geoservices.bayern.de" and parts.path == "/od/wms/dop/v1/dop40"
    assert q["CRS"] == "EPSG:3857" and q["LAYERS"] == "by_dop40c" and q["VERSION"] == "1.3.0"
    assert q["REQUEST"] == "GetMap" and q["WIDTH"] == q["HEIGHT"] == "256"
    min_x, min_y, max_x, max_y = (float(v) for v in q["BBOX"].split(","))
    assert (min_x, min_y, max_x, max_y) == pytest.approx(tile_bounds_3857(17, x, y), abs=1e-3)
    # a square box of one tile: 2 * 20037508.34 / 2**17 metres
    assert max_x - min_x == pytest.approx(2 * WORLD_HALF_M / 2**17, abs=1e-3)
    assert max_y - min_y == pytest.approx(max_x - min_x, abs=1e-3)
    legacy = wms_get_map_url(
        "https://w.example/wms?map=a",
        layer="l",
        image_format="image/png",
        version="1.1.1",
        z=0,
        x=0,
        y=0,
        size=512,
    )
    assert "SRS=EPSG:3857" in legacy and "CRS=" not in legacy and "?map=a&SERVICE=WMS" in legacy


def test_xyz_template_is_filled() -> None:
    assert xyz_url("https://t.example/{z}/{x}/{y}.jpg", 17, 3, 4) == "https://t.example/17/3/4.jpg"


def test_tile_range() -> None:
    assert tile_in_range(0, 0, 0) and tile_in_range(17, 131071, 0)
    assert not tile_in_range(17, 131072, 0) and not tile_in_range(1, 0, 2)
    assert not tile_in_range(25, 0, 0) and not tile_in_range(-1, 0, 0)


def a_map(rotation: float = 0.0) -> MowerMap:
    return MowerMap(
        reference=GeoReference(origin_lat=LAT, origin_lon=LON, rotation_deg=rotation),
        areas=[MapArea(id="a", boundary=[(0, 0), (30, 0), (30, 20), (0, 20)])],
        paths=[MapPath(id="p", points=[(0, 0), (-10, -5)])],
        dock=DockPose(x=-12, y=-6),
        revision="r1",
    )


def test_map_bounds_enclose_every_point_plus_the_margin_and_follow_the_rotation() -> None:
    plain = map_bounds(a_map(), 50.0)
    # 30 m east + 12 m west + 2 x 50 m: about 142 m wide, 20 + 6 + 100 = 126 m high
    metres_per_deg_lat = math.pi * 6_378_137.0 / 180
    metres_per_deg_lon = metres_per_deg_lat * math.cos(math.radians(LAT))
    assert (plain.north - plain.south) * metres_per_deg_lat == pytest.approx(126, abs=0.5)
    assert (plain.east - plain.west) * metres_per_deg_lon == pytest.approx(142, abs=0.5)
    assert plain.south < LAT < plain.north and plain.west < LON < plain.east
    rotated = map_bounds(a_map(90.0), 0.0)
    # rotated by 90 degrees counter-clockwise: local +x (30 m) points north
    assert (rotated.north - LAT) * metres_per_deg_lat == pytest.approx(30, abs=0.1)
    for x, y in [(0, 0), (30, 20), (-12, -6)]:
        lat, lon = to_wgs84(a_map(90.0).reference, x, y)
        assert rotated.south <= lat <= rotated.north and rotated.west <= lon <= rotated.east


def test_local_metres_land_on_the_right_mercator_pixels() -> None:
    """Alignment: two local points 100 m east of each other are 100 / cos(lat) Mercator metres
    apart (the scale of Web Mercator), which is how Leaflet places the same points."""
    ref = GeoReference(origin_lat=LAT, origin_lon=LON, rotation_deg=0)
    x0, y0 = wgs84_to_mercator(*to_wgs84(ref, 0, 0))
    x1, y1 = wgs84_to_mercator(*to_wgs84(ref, 100, 0))
    assert (x1 - x0) == pytest.approx(100 / math.cos(math.radians(LAT)), rel=2e-3)
    assert y1 == pytest.approx(y0, abs=1e-6)
    # and at 0.2985 m / pixel (zoom 19 at the equator) the tile holds the point at the pixel
    # the Mercator coordinates give
    z = 19
    tx, ty = slippy(*to_wgs84(ref, 0, 0), z)
    min_x, min_y, max_x, max_y = tile_bounds_3857(z, tx, ty)
    assert min_x <= x0 < max_x and min_y <= y0 < max_y


def test_bounds_intersection_ignores_touching_edges() -> None:
    a = Bounds(0, 0, 1, 1)
    assert a.intersects(Bounds(0.5, 0.5, 2, 2))
    assert not a.intersects(Bounds(1, 0, 2, 1))
    assert not a.intersects(Bounds(2, 2, 3, 3))


# --- cache, source, limiter -------------------------------------------------------------------


async def test_disk_cache_hits_expires_and_trims(tmp_path: Path) -> None:
    cache = DiskTileCache(tmp_path / "c", max_bytes=1000)
    key = "ab" + "0" * 62
    assert await cache.get(key, timedelta(days=1)) is None
    await cache.put(key, FetchedTile(JPEG, "image/jpeg"))
    hit = await cache.get(key, timedelta(days=1))
    assert hit is not None and hit.body == JPEG and hit.media_type == "image/jpeg"
    assert await cache.get(key, timedelta(seconds=0)) is None  # older than the TTL
    old = time.time() - 3600
    for i in range(30):
        k = f"{i:02x}" + "1" * 62
        await cache.put(k, FetchedTile(PNG, "image/png"))
        (tmp_path / "c" / k[:2] / k).touch()
        import os

        os.utime(tmp_path / "c" / k[:2] / k, (old + i, old + i))
    cache.trim()
    assert cache.size_bytes() <= 1000
    # the oldest tiles went first
    assert await cache.get("00" + "1" * 62, timedelta(days=1)) is None
    assert await cache.get("1d" + "1" * 62, timedelta(days=1)) is not None


async def test_cache_that_cannot_be_written_just_misses(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    cache = DiskTileCache(blocker / "sub", max_bytes=1000)
    await cache.put("ab" + "0" * 62, FetchedTile(JPEG, "image/jpeg"))
    assert await cache.get("ab" + "0" * 62, timedelta(days=1)) is None


def test_sniffing_accepts_only_jpeg_and_png() -> None:
    assert sniff_media_type(JPEG) == "image/jpeg" and sniff_media_type(PNG) == "image/png"
    assert sniff_media_type(b"<?xml version='1.0'?><ServiceExceptionReport/>") is None


async def test_http_source_rejects_errors_non_images_and_unreachable_hosts() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        match request.url.path:
            case "/ok":
                return httpx.Response(200, content=JPEG)
            case "/xml":
                return httpx.Response(200, content=b"<ServiceExceptionReport/>")
            case "/big":
                return httpx.Response(200, content=JPEG + b"\x00" * 2_100_000)
            case "/redirect":
                return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
            case "/boom":
                raise httpx.ConnectError("down")
        return httpx.Response(500)

    source = HttpTileSource(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert (await source.fetch("https://s.example/ok")).media_type == "image/jpeg"
    for path in ("xml", "big", "redirect", "boom", "other"):
        with pytest.raises(AerialUpstreamError):
            await source.fetch(f"https://s.example/{path}")
    await source.close()


def test_miss_limiter_is_per_account_and_a_sliding_window() -> None:
    now = [0.0]
    limiter = MissLimiter(2, lambda: now[0])
    a, b = uuid4(), uuid4()
    assert limiter.take(a) is None and limiter.take(a) is None
    wait = limiter.take(a)
    assert wait is not None and 1 <= wait <= 61
    assert limiter.take(b) is None
    now[0] = 61.0
    assert limiter.take(a) is None


# --- routes -----------------------------------------------------------------------------------


class FakeSource:
    def __init__(self, body: bytes = JPEG) -> None:
        self.urls: list[str] = []
        self.body = body

    async def fetch(self, url: str) -> FetchedTile:
        self.urls.append(url)
        return FetchedTile(self.body, "image/png" if self.body == PNG else "image/jpeg")


class MemoryCache:
    def __init__(self) -> None:
        self.items: dict[str, FetchedTile] = {}

    async def get(self, key: str, max_age: timedelta) -> FetchedTile | None:
        return self.items.get(key)

    async def put(self, key: str, tile: FetchedTile) -> None:
        self.items[key] = tile


ATTRIBUTION = "Bayerische Vermessungsverwaltung \u2013 www.geodaten.bayern.de"
WMS: dict[str, Any] = {
    "telemetry_aerial_wms_url": "https://geoservices.bayern.de/od/wms/dop/v1/dop40",
    "telemetry_aerial_wms_layer": "by_dop40c",
    "telemetry_aerial_attribution": ATTRIBUTION,
}


async def publish_map(h: Harness, mower_id: UUID, account_id: UUID) -> None:
    await h.bus.publish(
        device_subject(mower_id, "map"),
        MowerMapSnapshot(mower_id=mower_id, account_id=account_id, observed_at=T0, map=a_map()),
    )


async def test_aerial_is_off_by_default_and_says_so() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        before_map = await h.client.get(f"{API}/mowers/{mower_id}/aerial", headers=owner.headers)
        await publish_map(h, mower_id, owner.account_id)
        info = await h.client.get(f"{API}/mowers/{mower_id}/aerial", headers=owner.headers)
        tile = await h.client.get(f"{API}/mowers/{mower_id}/aerial/17/1/1", headers=owner.headers)
    assert before_map.status_code == 404
    assert info.status_code == 200
    assert info.json() == {
        "mower_id": str(mower_id),
        "enabled": False,
        "tile_url": None,
        "attribution": None,
        "min_zoom": None,
        "max_zoom": None,
        "bounds": None,
    }
    assert tile.status_code == 404
    assert tile.json()["type"] == "urn:mowdeck:problem:aerial-disabled"


async def test_aerial_serves_tiles_around_the_map_from_the_source_once() -> None:
    source, cache = FakeSource(), MemoryCache()
    async with harness(aerial_source=source, aerial_cache=cache, **WMS) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await publish_map(h, mower_id, owner.account_id)
        info = await h.client.get(f"{API}/mowers/{mower_id}/aerial", headers=owner.headers)
        x, y = slippy(LAT, LON, 18)
        url = f"{API}/mowers/{mower_id}/aerial/18/{x}/{y}"
        first = await h.client.get(url, headers=owner.headers)
        second = await h.client.get(url, headers=owner.headers)
        anonymous = await h.client.get(url)
        stranger = await h.owner()
        foreign = await h.client.get(url, headers=stranger.headers)
    body = info.json()
    assert body["enabled"] is True
    assert body["tile_url"] == f"/api/v1/telemetry/mowers/{mower_id}/aerial/{{z}}/{{x}}/{{y}}"
    assert body["attribution"] == ATTRIBUTION
    assert (body["min_zoom"], body["max_zoom"]) == (14, 19)
    b = body["bounds"]
    assert b["south"] < LAT < b["north"] and b["west"] < LON < b["east"]
    assert first.status_code == 200 and first.content == JPEG
    assert first.headers["content-type"] == "image/jpeg"
    assert first.headers["cache-control"] == "private, max-age=86400"
    assert second.content == JPEG
    assert len(source.urls) == 1  # the second answer came from the cache
    assert "by_dop40c" in source.urls[0] and "EPSG%3A3857" in source.urls[0].replace(":", "%3A")
    assert anonymous.status_code == 401
    assert foreign.status_code == 404


async def test_aerial_is_not_an_open_proxy() -> None:
    source = FakeSource()
    async with harness(aerial_source=source, aerial_cache=MemoryCache(), **WMS) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await publish_map(h, mower_id, owner.account_id)
        base = f"{API}/mowers/{mower_id}/aerial"
        x, y = slippy(LAT, LON, 18)
        far = await h.client.get(f"{base}/18/{x + 40}/{y}", headers=owner.headers)
        too_low = await h.client.get(f"{base}/10/{x >> 8}/{y >> 8}", headers=owner.headers)
        too_high = await h.client.get(f"{base}/20/{x * 2}/{y * 2}", headers=owner.headers)
        bad_index = await h.client.get(f"{base}/18/999999999/0", headers=owner.headers)
        negative = await h.client.get(f"{base}/18/-1/0", headers=owner.headers)
        near_edge = await h.client.get(f"{base}/18/{x + 1}/{y}", headers=owner.headers)
    for response in (far, too_low, too_high, bad_index):
        assert response.status_code == 404, response.text
        assert response.json()["type"] == "urn:mowdeck:problem:tile-outside-map"
    assert negative.status_code == 422
    assert near_edge.status_code in (200, 404)
    assert len(source.urls) <= 1  # nothing outside the map ever reached the source


async def test_aerial_limits_new_tiles_per_account_and_reports_a_failing_source() -> None:
    source = FakeSource()
    async with harness(
        aerial_source=source,
        aerial_cache=MemoryCache(),
        telemetry_aerial_misses_per_minute=2,
        **WMS,
    ) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await publish_map(h, mower_id, owner.account_id)
        x, y = slippy(LAT, LON, 19)
        results = [
            await h.client.get(
                f"{API}/mowers/{mower_id}/aerial/19/{x + dx}/{y}", headers=owner.headers
            )
            for dx in (0, 0, -1, 1)
        ]
    assert [r.status_code for r in results] == [200, 200, 200, 429]
    assert int(results[3].headers["retry-after"]) >= 1
    assert results[3].json()["type"] == "urn:mowdeck:problem:rate-limited"

    class Broken:
        async def fetch(self, url: str) -> FetchedTile:
            raise AerialUpstreamError("the aerial photo source is unreachable")

    async with harness(aerial_source=Broken(), **WMS) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await publish_map(h, mower_id, owner.account_id)
        x, y = slippy(LAT, LON, 18)
        failed = await h.client.get(
            f"{API}/mowers/{mower_id}/aerial/18/{x}/{y}", headers=owner.headers
        )
    assert failed.status_code == 502
    assert failed.json()["type"] == "urn:mowdeck:problem:aerial-source-failed"


def test_settings_treat_empty_urls_as_unset_and_reject_plain_http() -> None:
    from pydantic import ValidationError
    from telemetry_support import make_settings

    config = make_settings(telemetry_aerial_wms_url="", telemetry_aerial_xyz_url="")
    assert not config.to_aerial_config().enabled
    assert make_settings(**WMS).to_aerial_config().enabled
    with pytest.raises(ValidationError):
        make_settings(telemetry_aerial_wms_url="http://insecure.example/wms")
    with pytest.raises(ValidationError):
        make_settings(telemetry_aerial_xyz_url="https://t.example/tiles.png")
    xyz = make_settings(telemetry_aerial_xyz_url="https://t.example/{z}/{x}/{y}.jpg")
    assert xyz.to_aerial_config() == AerialConfig(
        xyz_url="https://t.example/{z}/{x}/{y}.jpg",
        wms_layer="",
        cache_ttl=timedelta(days=30),
    )
