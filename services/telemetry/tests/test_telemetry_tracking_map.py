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

    async def get(self, key: str) -> FetchedTile | None:
        return self.items.get(key)

    async def put(self, key: str, tile: FetchedTile, lifetime: timedelta) -> None:
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
        {
            "id": "map",
            "attribution": "© Example",
            "min_zoom": 8,
            "max_zoom": 19,
            "covers_last": True,
        },
        {
            "id": "aerial",
            "attribution": "Photo credit",
            "min_zoom": 14,
            "max_zoom": 19,
            "covers_last": True,
        },
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
        {
            "id": "map",
            "attribution": "© Example",
            "min_zoom": 8,
            "max_zoom": 19,
            "covers_last": True,
        }
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


# --- upstream etiquette (OpenStreetMap tile usage policy) -----------------------------------


async def test_upstream_requests_identify_mowdeck_and_send_no_referer() -> None:
    import httpx

    from telemetry.adapters.aerial import HttpTileSource, user_agent

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=JPEG, headers={"Cache-Control": "max-age=86400"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = HttpTileSource(client=client, version="1.2.3")
    tile = await source.fetch("https://tile.openstreetmap.org/15/1/2.png")
    await source.close()
    assert seen[0].headers["user-agent"] == "MowDeck/1.2.3 (+https://github.com/lukislp/mowdeck)"
    assert "referer" not in seen[0].headers
    assert tile.lifetime == timedelta(days=1)
    assert user_agent(None) == "MowDeck/dev (+https://github.com/lukislp/mowdeck)"


def test_cache_lifetime_follows_cache_control_then_expires() -> None:
    from datetime import UTC, datetime

    import httpx

    from telemetry.adapters.aerial import cache_lifetime_of

    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

    def of(**headers: str) -> timedelta | None:
        return cache_lifetime_of(
            httpx.Headers({k.replace("_", "-"): v for k, v in headers.items()}), now
        )

    assert of() is None
    assert of(cache_control="public, max-age=604800") == timedelta(days=7)
    assert of(cache_control='max-age="60", stale-while-revalidate=10') == timedelta(seconds=60)
    assert of(cache_control="no-store") == timedelta(0)
    assert of(cache_control="no-cache, max-age=600") == timedelta(0)
    assert of(expires="Wed, 28 Oct 2026 14:00:00 GMT") == timedelta(hours=2)
    assert of(expires="Tue, 27 Oct 2026 14:00:00 GMT") == timedelta(0)
    assert of(expires="0") == timedelta(0)
    # max-age wins over Expires
    assert of(cache_control="max-age=5", expires="Wed, 28 Oct 2026 14:00:00 GMT") == timedelta(
        seconds=5
    )


async def test_tiles_are_kept_as_long_as_the_source_allows_and_never_prefetched() -> None:
    import asyncio

    from telemetry.application.aerial import cache_lifetime, fetch_cached

    class Source:
        def __init__(self, lifetime: timedelta | None) -> None:
            self.urls: list[str] = []
            self.lifetime = lifetime

        async def fetch(self, url: str) -> FetchedTile:
            self.urls.append(url)
            return FetchedTile(JPEG, "image/jpeg", self.lifetime)

    class Recording(MemoryCache):
        def __init__(self) -> None:
            super().__init__()
            self.lifetimes: list[timedelta] = []

        async def put(self, key: str, tile: FetchedTile, lifetime: timedelta) -> None:
            self.lifetimes.append(lifetime)
            await super().put(key, tile, lifetime)

    gate = asyncio.Semaphore(1)
    # no header: the configured TTL, but never below 7 days
    assert cache_lifetime(FetchedTile(JPEG, "image/jpeg"), timedelta(days=1)) == timedelta(days=7)
    assert cache_lifetime(FetchedTile(JPEG, "image/jpeg"), timedelta(days=30)) == timedelta(days=30)
    for lifetime, kept in ((timedelta(hours=3), timedelta(hours=3)), (None, timedelta(days=30))):
        source, cache = Source(lifetime), Recording()
        for _ in range(3):
            await fetch_cached(
                "https://t/1",
                source,
                cache,
                timedelta(days=30),
                before_fetch=lambda: None,
                gate=gate,
            )
        assert source.urls == ["https://t/1"]  # fetched once, then from the cache
        assert cache.lifetimes == [kept]
    # no-store: served, not kept
    source, cache = Source(timedelta(0)), Recording()
    await fetch_cached(
        "https://t/2", source, cache, timedelta(days=30), before_fetch=lambda: None, gate=gate
    )
    assert cache.items == {}


async def test_layer_outside_its_coverage_is_flagged_and_refused() -> None:
    bavaria: dict[str, Any] = {"telemetry_aerial_coverage": "47.27,8.97,50.57,13.84"}
    elsewhere: dict[str, Any] = {"telemetry_aerial_coverage": "52.0,13.0,53.0,14.0"}
    for coverage, covered in ((bavaria, True), (elsewhere, False)):
        source = FakeSource()
        async with harness(
            tracking_gateway=Gateway(),
            aerial_source=source,
            aerial_cache=MemoryCache(),
            **STREET,
            **AERIAL,
            **coverage,
        ) as h:
            owner, mower_id, token = await tracked(h)
            status = await h.client.get(f"{API}/mowers/{mower_id}/tracking", headers=owner.headers)
            view = await h.client.post(f"{API}/public/tracking/view", json={"token": token})
            x, y = slippy(LAT, LON, 16)
            aerial = await h.client.get(
                f"{API}/mowers/{mower_id}/tracking/map/aerial/16/{x}/{y}", headers=owner.headers
            )
        flags = {layer["id"]: layer["covers_last"] for layer in status.json()["map_layers"]}
        assert flags == {"map": True, "aerial": covered}
        assert [layer["covers_last"] for layer in view.json()["map_layers"]] == [True, covered]
        assert aerial.status_code == (200 if covered else 404)


def test_coverage_setting_is_validated() -> None:
    import pytest
    from pydantic import ValidationError
    from telemetry_support import make_settings

    assert make_settings(telemetry_aerial_coverage="").telemetry_aerial_coverage == ""
    for bad in ("1,2,3", "50,8,47,13", "a,b,c,d", "47,8,95,13"):
        with pytest.raises(ValidationError):
            make_settings(telemetry_aerial_coverage=bad)
