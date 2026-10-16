"""Phase 1 gate (docs/phases.md): run with ``pnpm e2e:stack acceptance`` (throwaway stack).

The criterion, step by step (usernames are unique per run):

1. ``lukas-<run>`` registers and logs in.
2. He creates the sub-user ``anna-<run>`` with the seeded Operator role on his account; she logs in.
3. He registers a simulated mower through Fleet.
4. Telemetry reports it ``docked`` (the Device-Gateway connected it).
5. Anna opens the Realtime socket, authenticates and subscribes: the mower is allowed.
6. Anna starts mowing area ``front`` through Fleet (202): the socket shows the
   ``command_result`` ``executed`` and a ``state`` ``mowing``; Fleet's record is ``executed``.
7. Anna sends ``return_to_dock`` (202): the socket shows ``returning`` or ``docked``.
8. ``bob-<run>`` (another account) gets 404 on the mower, on a command to it and on its
   telemetry, and his subscription reports it as denied. ``vera-<run>`` (Viewer in Lukas'
   account) gets 403 on ``start_mow`` and 202 on ``emergency_stop``.
9. Anna's socket stays open for 90 s without traffic through Traefik and still receives a
   state afterwards (marker ``slow``).
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from e2e_support import (
    FLEET,
    IDENTITY,
    TELEMETRY,
    Actor,
    E2EConfig,
    LiveSocket,
    connect_socket,
    create_subuser,
    describe,
    is_type,
    isolated_stack_problem,
    login,
    open_socket,
    register_owner,
    wait_for,
)

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio(loop_scope="module")]

UNAUTHORIZED = 4401


@pytest.fixture(scope="module")
def cfg() -> E2EConfig:
    return E2EConfig.from_env()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def http(cfg: E2EConfig) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=cfg.base_url, timeout=cfg.http_timeout) as client:
        problem = await isolated_stack_problem(client)
        if problem:
            pytest.exit(problem, returncode=2)
        try:
            response = await client.get(f"{IDENTITY}/.well-known/jwks.json")
        except httpx.HTTPError as exc:
            pytest.fail(f"stack not reachable at {cfg.base_url}: {exc!r} (docker compose up?)")
        assert response.status_code == 200, f"stack not ready: {describe(response)}"
        yield client


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def lukas(http: httpx.AsyncClient, cfg: E2EConfig) -> Actor:
    return await register_owner(http, cfg, "lukas")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def anna(http: httpx.AsyncClient, cfg: E2EConfig, lukas: Actor) -> Actor:
    return await create_subuser(http, cfg, lukas, "anna", "operator")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def vera(http: httpx.AsyncClient, cfg: E2EConfig, lukas: Actor) -> Actor:
    return await create_subuser(http, cfg, lukas, "vera", "viewer")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def bob(http: httpx.AsyncClient, cfg: E2EConfig) -> Actor:
    return await register_owner(http, cfg, "bob")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def mower(http: httpx.AsyncClient, cfg: E2EConfig, lukas: Actor) -> AsyncIterator[str]:
    response = await http.post(
        f"{FLEET}/mowers",
        headers=lukas.headers,
        json={
            "name": f"e2e mower {cfg.run_id}",
            "model": "simulator",
            "connection_kind": "simulated",
            "connection_options": {"time_scale": 30},
        },
    )
    assert response.status_code == 201, describe(response)
    mower_id: str = response.json()["id"]

    async def docked() -> tuple[dict[str, Any] | None, Any]:
        state = await http.get(f"{TELEMETRY}/mowers/{mower_id}/state", headers=lukas.headers)
        if state.status_code == 200 and state.json()["state"]["activity"] == "docked":
            return state.json(), state.json()
        return None, describe(state)

    await wait_for(docked, cfg.ready_timeout, f"telemetry state 'docked' of mower {mower_id}")
    yield mower_id
    await http.delete(f"{FLEET}/mowers/{mower_id}", headers=lukas.headers)  # best effort


async def send_command(
    http: httpx.AsyncClient, actor: Actor, mower_id: str, command: dict[str, Any]
) -> httpx.Response:
    return await http.post(
        f"{FLEET}/mowers/{mower_id}/commands", headers=actor.headers, json={"command": command}
    )


async def test_phase1_operator_starts_and_docks_with_live_status(
    http: httpx.AsyncClient, cfg: E2EConfig, anna: Actor, mower: str
) -> None:
    live = await open_socket(cfg, anna)
    try:
        await live.send({"type": "subscribe", "request_id": "anna", "mower_ids": [mower]})
        subscribed = await live.expect(
            is_type("subscribed", request_id="anna"), cfg.http_timeout, "subscribed"
        )
        assert subscribed["mower_ids"] == [mower], subscribed
        assert subscribed["denied"] == [], subscribed

        response = await send_command(
            http, anna, mower, {"kind": "start_mow", "area_ids": ["front"]}
        )
        assert response.status_code == 202, describe(response)
        start_id = response.json()["command_id"]
        await live.expect(
            is_type("command_result", result__command_id=start_id, result__status="executed"),
            cfg.live_timeout,
            f"command_result executed for start_mow {start_id}",
        )
        await live.expect(
            is_type("state", mower_id=mower, state__activity="mowing"),
            cfg.live_timeout,
            "state mowing",
        )

        # Fleet updates its record from the same result the socket just delivered, a few
        # milliseconds later, so poll instead of asserting once.
        async def start_executed() -> tuple[str | None, Any]:
            record = await http.get(f"{FLEET}/commands/{start_id}", headers=anna.headers)
            status = record.json().get("status") if record.status_code == 200 else None
            return (status if status == "executed" else None), describe(record)

        await wait_for(start_executed, cfg.live_timeout, f"start_mow {start_id} executed")

        response = await send_command(http, anna, mower, {"kind": "return_to_dock"})
        assert response.status_code == 202, describe(response)
        dock_id = response.json()["command_id"]
        since_dock = live.mark()

        def returning_or_docked(message: dict[str, Any]) -> bool:
            return (
                message.get("type") == "state"
                and message.get("mower_id") == mower
                and message["state"]["activity"] in ("returning", "docked")
            )

        await live.expect(
            returning_or_docked,
            cfg.dock_timeout,
            f"state returning or docked after {dock_id}",
            after=since_dock,
        )
    finally:
        await live.close()


async def test_phase1_foreign_owner_is_rejected_everywhere(
    http: httpx.AsyncClient, cfg: E2EConfig, bob: Actor, mower: str
) -> None:
    checks = [
        ("GET", f"{FLEET}/mowers/{mower}", None),
        (
            "POST",
            f"{FLEET}/mowers/{mower}/commands",
            {"command": {"kind": "start_mow", "area_ids": ["front"]}},
        ),
        ("GET", f"{TELEMETRY}/mowers/{mower}/state", None),
    ]
    for method, path, body in checks:
        response = await http.request(method, path, headers=bob.headers, json=body)
        assert response.status_code == 404, describe(response)
    listing = await http.get(f"{FLEET}/mowers", headers=bob.headers)
    assert listing.status_code == 200, describe(listing)
    assert mower not in {m["id"] for m in listing.json()["items"]}, describe(listing)

    live = await open_socket(cfg, bob)
    try:
        await live.send({"type": "subscribe", "request_id": "bob", "mower_ids": [mower]})
        subscribed = await live.expect(
            is_type("subscribed", request_id="bob"), cfg.http_timeout, "subscribed"
        )
        assert subscribed["mower_ids"] == [], subscribed
        assert subscribed["denied"] == [mower], subscribed
        # Heartbeats arrive every 10 s; none may reach bob.
        await live.silent_for(12)
    finally:
        await live.close()


async def test_phase1_requests_without_token_are_rejected(
    http: httpx.AsyncClient, cfg: E2EConfig, mower: str
) -> None:
    assert (await http.get(f"{FLEET}/mowers/{mower}")).status_code == 401
    assert (await http.get(f"{TELEMETRY}/mowers/{mower}/state")).status_code == 401
    unauthenticated = await connect_socket(cfg)
    await unauthenticated.send({"type": "auth", "token": "not-a-token"})
    assert await unauthenticated.closed_code(cfg.http_timeout) == UNAUTHORIZED
    silent = await connect_socket(cfg)  # no auth message at all: closed after 5 s
    assert await silent.closed_code(cfg.http_timeout) == UNAUTHORIZED


@pytest.mark.slow
async def test_phase1_idle_socket_survives_traefik(
    http: httpx.AsyncClient, cfg: E2EConfig, anna: Actor, mower: str
) -> None:
    await login(http, anna)  # a fresh token: the socket must outlive the idle time
    live: LiveSocket = await open_socket(cfg, anna, keepalive=False)
    try:
        await live.silent_for(cfg.idle_seconds)
        await live.send({"type": "subscribe", "request_id": "idle", "mower_ids": [mower]})
        await live.expect(is_type("subscribed", request_id="idle"), cfg.http_timeout, "subscribed")
        state = await live.expect(
            is_type("state", mower_id=mower), cfg.live_timeout, "state after the idle time"
        )
        assert state["state"]["mower_id"] == mower
    finally:
        await live.close()


async def test_phase1_viewer_cannot_start_but_can_emergency_stop(  # last: it stops the mower
    http: httpx.AsyncClient, cfg: E2EConfig, vera: Actor, mower: str
) -> None:
    response = await send_command(http, vera, mower, {"kind": "start_mow", "area_ids": ["front"]})
    assert response.status_code == 403, describe(response)
    response = await send_command(http, vera, mower, {"kind": "emergency_stop"})
    assert response.status_code == 202, describe(response)
    stop_id = response.json()["command_id"]

    async def executed() -> tuple[str | None, Any]:
        record = await http.get(f"{FLEET}/commands/{stop_id}", headers=vera.headers)
        status = record.json().get("status") if record.status_code == 200 else None
        return (status if status == "executed" else None), describe(record)

    await wait_for(executed, cfg.live_timeout, f"emergency stop {stop_id} executed")
