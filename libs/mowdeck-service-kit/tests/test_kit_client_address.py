"""The client address behind the gateway: one rule for every service (client_address)."""

from typing import Any

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from mowdeck_service_kit import ServiceSettings
from mowdeck_service_kit.client_address import client_address, forwarded_client
from mowdeck_service_kit.testing import make_test_settings

CADDY = "10.250.80.2"
NGINX = "192.0.2.10"


@pytest.mark.parametrize(
    ("header", "depth", "expected"),
    [
        # No proxy in front: Traefik dropped the client's header and appended its peer.
        ("203.0.113.1", 0, "203.0.113.1"),
        # Caddy replaces the header, Traefik appends Caddy.
        (f"203.0.113.1, {CADDY}", 1, "203.0.113.1"),
        # nginx appends to what the client sent, Traefik appends nginx: the forged entries on
        # the left change nothing.
        (f"198.51.100.7, 203.0.113.1, {NGINX}", 1, "203.0.113.1"),
        (f"1.1.1.1,2.2.2.2 , 203.0.113.1,{NGINX}", 1, "203.0.113.1"),
        # A request that bypassed the proxy has fewer entries: the one Traefik wrote.
        ("203.0.113.9", 1, "203.0.113.9"),
        ("203.0.113.9", 3, "203.0.113.9"),
        # Depth 0 behind a proxy names the proxy (the gateway's peer), never a client value.
        (f"198.51.100.7, {NGINX}", 0, NGINX),
        # Unset keeps the earlier rule: the leftmost entry.
        (f"203.0.113.1, {CADDY}", None, "203.0.113.1"),
        (f"198.51.100.7, 203.0.113.1, {NGINX}", None, "198.51.100.7"),
        # Empty entries are skipped.
        (f" , 203.0.113.1, {CADDY}, ", 1, "203.0.113.1"),
    ],
)
def test_forwarded_client_counts_from_the_right(
    header: str, depth: int | None, expected: str
) -> None:
    assert forwarded_client(header, "172.18.0.5", proxy_depth=depth) == expected


@pytest.mark.parametrize("header", [None, "", " , "])
def test_without_a_header_the_peer_counts(header: str | None) -> None:
    assert forwarded_client(header, "172.18.0.5", proxy_depth=1) == "172.18.0.5"
    assert forwarded_client(header, None, proxy_depth=None) is None


def test_entries_are_cut_to_64_characters() -> None:
    assert forwarded_client("a" * 200, None, proxy_depth=0) == "a" * 64
    assert forwarded_client(None, "b" * 200, proxy_depth=0) == "b" * 64


def _app(**settings: Any) -> FastAPI:
    app = FastAPI()
    app.state.settings = make_test_settings(**settings)

    @app.get("/ip")
    async def ip(request: Request, trusted: bool = True) -> dict[str, str | None]:
        return {"ip": client_address(request, trust_forwarded_for=trusted)}

    return app


async def _ip(app: FastAPI, headers: list[tuple[str, str]], **params: Any) -> str | None:
    transport = ASGITransport(app=app, client=("172.18.0.5", 50000))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ip", headers=headers, params=params)
    value: str | None = response.json()["ip"]
    return value


async def test_client_address_uses_the_settings_depth() -> None:
    app = _app(client_ip_depth=1)
    chain = [("X-Forwarded-For", f"198.51.100.7, 203.0.113.1, {NGINX}")]
    assert await _ip(app, chain) == "203.0.113.1"
    # Several header lines form one list.
    lines = [("X-Forwarded-For", "198.51.100.7"), ("X-Forwarded-For", f"203.0.113.1, {NGINX}")]
    assert await _ip(app, lines) == "203.0.113.1"


async def test_client_address_without_trust_is_the_peer() -> None:
    app = _app(client_ip_depth=1)
    chain = [("X-Forwarded-For", f"203.0.113.1, {CADDY}")]
    assert await _ip(app, chain, trusted="false") == "172.18.0.5"


async def test_client_address_without_depth_keeps_the_leftmost_entry() -> None:
    app = _app()
    chain = [("X-Forwarded-For", f"198.51.100.7, {NGINX}")]
    assert await _ip(app, chain) == "198.51.100.7"


@pytest.mark.parametrize(("raw", "expected"), [("", None), (" ", None), ("0", 0), ("2", 2)])
def test_depth_setting_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int | None
) -> None:
    monkeypatch.setenv("MOWDECK_CLIENT_IP_DEPTH", raw)
    assert ServiceSettings(service_name="test-service").client_ip_depth == expected


def test_depth_setting_rejects_negative_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOWDECK_CLIENT_IP_DEPTH", "-1")
    with pytest.raises(ValueError, match="client_ip_depth"):
        ServiceSettings(service_name="test-service")
