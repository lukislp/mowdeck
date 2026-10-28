"""The base map under a theft tracking track (ADR 0058): street map and aerial photo tiles,
served only around the tracked positions, for the owner and for a share link."""

import math
from datetime import timedelta
from typing import Any
from uuid import UUID

from telemetry_support import API, T0, Harness, Person, harness, make_state, seconds

from telemetry.application.aerial import FetchedTile
from telemetry.domain.aerial import points_bounds, tile_bounds

JPEG = b"\xff\xd8\xff\xe0tile"
STREET: dict[str, Any] = {
    "telemetry_basemap_url": "https://tiles.example/{z}/{y}/{x}.png",
    "telemetry_basemap_attribution": "© Example",
}
AERIAL: dict[str, Any] = {
    "telemetry_aerial_wms_url": "https://wms.example/dop",
    "telemetry_aerial_wms_layer": "dop",
    "telemetry_aerial_attribution": "Photo credit",
}
LAT, LON = 48.0, 11.0


class FakeSource:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def fetch(self, url: str) -> FetchedTile:
        self.urls.append(url)
        return FetchedTile(JPEG, "image/jpeg")


class MemoryCache:
    def __init__(self) -> None:
        self.items: dict[str, FetchedTile] = {}

    async def get(self, key: str, max_age: timedelta) -> FetchedTile | None:
        return self.items.get(key)

    async def put(self, key: str, tile: FetchedTile) -> None:
        self.items[key] = tile


class Gateway:
    async def lease(self, request: Any) -> None:
        return None

    async def photo(self, request: Any) -> None:
        return None


def slippy(lat: float, lon: float, z: int) -> tuple[int, int]:
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(rad)) / math.pi) / 2.0 * n)
    return x, y


async def tracked(h: Harness) -> tuple[Person, UUID, str]:
    """A mower with a session that has moved ~1 km north, and a share token."""
    owner = await h.owner()
    mower_id = await h.add_mower(owner)
    await h.publish_state(make_state(mower_id, owner.account_id, at=T0, position=(LAT, LON)))
    base = f"{API}/mowers/{mower_id}/tracking"
    assert (await h.client.post(base, headers=owner.headers)).status_code == 201
    await h.publish_state(
        make_state(mower_id, owner.account_id, at=T0 + seconds(30), position=(LAT + 0.01, LON))
    )
    token = (await h.client.post(f"{base}/links", headers=owner.headers, json={})).json()["token"]
    return owner, mower_id, token


def test_points_bounds_add_the_margin_and_need_points() -> None:
    assert points_bounds([], 100) is None
    box = points_bounds([(48.0, 11.0), (48.01, 11.02)], 1000)
    assert box is not None
    assert box.south < 48.0 - 0.008 and box.north > 48.01 + 0.008
    assert box.west < 11.0 - 0.012 and box.east > 11.02 + 0.012
    # the box at the date line is clamped to the world
    edge = points_bounds([(89.99, 179.99)], 5000)
    assert edge is not None and edge.north == 90.0 and edge.east == 180.0


async def test_no_base_map_without_a_source() -> None:
    async with harness(tracking_gateway=Gateway()) as h:
        owner, mower_id, token = await tracked(h)
        status = await h.client.get(f"{API}/mowers/{mower_id}/tracking", headers=owner.headers)
        x, y = slippy(LAT, LON, 15)
        tile = await h.client.get(
            f"{API}/mowers/{mower_id}/tracking/map/map/15/{x}/{y}", headers=owner.headers
        )
        public = await h.client.post(
            f"{API}/public/tracking/tile",
            json={"token": token, "layer": "map", "z": 15, "x": x, "y": y},
        )
    assert status.json()["map_layers"] == []
    assert tile.status_code == 404
    assert tile.json()["type"] == "urn:mowdeck:problem:tile-outside-map"
    assert public.status_code == 404


async def test_owner_gets_street_and_aerial_tiles_around_the_track_only() -> None:
    source, cache = FakeSource(), MemoryCache()
    async with harness(
        tracking_gateway=Gateway(), aerial_source=source, aerial_cache=cache, **STREET, **AERIAL
    ) as h:
        owner, mower_id, _ = await tracked(h)
        status = await h.client.get(f"{API}/mowers/{mower_id}/tracking", headers=owner.headers)
        base = f"{API}/mowers/{mower_id}/tracking/map"
        x, y = slippy(LAT + 0.01, LON, 16)
        street = await h.client.get(f"{base}/map/16/{x}/{y}", headers=owner.headers)
        again = await h.client.get(f"{base}/map/16/{x}/{y}", headers=owner.headers)
        aerial = await h.client.get(f"{base}/aerial/16/{x}/{y}", headers=owner.headers)
        far = await h.client.get(f"{base}/map/16/{x + 200}/{y}", headers=owner.headers)
        too_low = await h.client.get(f"{base}/map/5/{x >> 11}/{y >> 11}", headers=owner.headers)
        aerial_low = await h.client.get(
            f"{base}/aerial/12/{x >> 4}/{y >> 4}", headers=owner.headers
        )
        unknown = await h.client.get(f"{base}/osm/16/{x}/{y}", headers=owner.headers)
        anonymous = await h.client.get(f"{base}/map/16/{x}/{y}")
        stranger = await h.owner()
        foreign = await h.client.get(f"{base}/map/16/{x}/{y}", headers=stranger.headers)
    layers = status.json()["map_layers"]
    assert layers == [
        {"id": "map", "attribution": "© Example", "min_zoom": 8, "max_zoom": 18},
        {"id": "aerial", "attribution": "Photo credit", "min_zoom": 14, "max_zoom": 19},
    ]
    assert street.status_code == 200 and street.content == JPEG
    assert street.headers["content-type"] == "image/jpeg"
    assert again.status_code == 200
    assert aerial.status_code == 200
    assert source.urls[0] == f"https://tiles.example/16/{y}/{x}.png"
    assert source.urls[1].startswith("https://wms.example/dop?") and "LAYERS=dop" in source.urls[1]
    assert len(source.urls) == 2  # the repeated street tile came from the cache
    for refused in (far, too_low, aerial_low):
        assert refused.status_code == 404, refused.text
        assert refused.json()["type"] == "urn:mowdeck:problem:tile-outside-map"
    assert unknown.status_code == 422
    assert anonymous.status_code == 401
    assert foreign.status_code == 404


async def test_share_link_gets_tiles_of_its_track_and_nothing_else() -> None:
    source = FakeSource()
    async with harness(
        tracking_gateway=Gateway(),
        aerial_source=source,
        aerial_cache=MemoryCache(),
        telemetry_tracking_public_failures_per_minute=3,
        **STREET,
    ) as h:
        owner, mower_id, token = await tracked(h)
        view = await h.client.post(f"{API}/public/tracking/view", json={"token": token})
        x, y = slippy(LAT, LON, 15)
        url = f"{API}/public/tracking/tile"
        ok = await h.client.post(
            url, json={"token": token, "layer": "map", "z": 15, "x": x, "y": y}
        )
        far = await h.client.post(
            url, json={"token": token, "layer": "map", "z": 15, "x": x + 100, "y": y}
        )
        aerial = await h.client.post(
            url, json={"token": token, "layer": "aerial", "z": 15, "x": x, "y": y}
        )
        bad = await h.client.post(
            url, json={"token": "A" * 43, "layer": "map", "z": 15, "x": x, "y": y}
        )
        links = await h.client.get(f"{API}/mowers/{mower_id}/tracking/links", headers=owner.headers)
    assert view.json()["map_layers"] == [
        {"id": "map", "attribution": "© Example", "min_zoom": 8, "max_zoom": 18}
    ]
    assert ok.status_code == 200 and ok.content == JPEG
    assert ok.headers["cache-control"] == "no-store"
    assert ok.headers["referrer-policy"] == "no-referrer"
    assert far.status_code == 404
    assert aerial.status_code == 404  # no aerial source configured here
    assert bad.status_code == 404
    assert len(source.urls) == 1
    assert links.json()[0]["uses"] == 1  # tiles do not count as views of the link


async def test_public_tiles_have_their_own_rate_limit() -> None:
    async with harness(
        tracking_gateway=Gateway(),
        aerial_source=FakeSource(),
        aerial_cache=MemoryCache(),
        telemetry_tracking_public_tiles_per_minute=2,
        **STREET,
    ) as h:
        _, _, token = await tracked(h)
        x, y = slippy(LAT, LON, 15)
        body = {"token": token, "layer": "map", "z": 15, "x": x, "y": y}
        codes = [
            (await h.client.post(f"{API}/public/tracking/tile", json=body)).status_code
            for _ in range(3)
        ]
        view = await h.client.post(f"{API}/public/tracking/view", json={"token": token})
    assert codes == [200, 200, 429]
    assert view.status_code == 200  # the page itself keeps working


def test_basemap_settings_need_https_and_all_placeholders() -> None:
    import pytest
    from pydantic import ValidationError
    from telemetry_support import make_settings

    assert make_settings(telemetry_basemap_url="").telemetry_basemap_url is None
    topplus = (
        "https://sgx.geodatenzentrum.de/wmts_topplus_open/tile/1.0.0/web/default/"
        "WEBMERCATOR/{z}/{y}/{x}.png"
    )
    assert make_settings(telemetry_basemap_url=topplus).telemetry_basemap_url == topplus
    with pytest.raises(ValidationError):
        make_settings(telemetry_basemap_url="http://tiles.example/{z}/{x}/{y}.png")
    with pytest.raises(ValidationError):
        make_settings(telemetry_basemap_url="https://tiles.example/{z}/{x}.png")


def test_tile_of_the_track_lies_inside_the_area() -> None:
    box = points_bounds([(LAT, LON)], 2000)
    assert box is not None
    x, y = slippy(LAT, LON, 17)
    assert box.intersects(tile_bounds(17, x, y))
    assert not box.intersects(tile_bounds(17, x + 100, y))
