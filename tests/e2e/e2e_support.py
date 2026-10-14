"""Helpers for the Phase 1 acceptance test (it runs against the Compose stack through Traefik).

Configuration from the environment:

| Variable | Default | Meaning |
| --- | --- | --- |
| ``MOWDECK_E2E_BASE_URL`` | ``http://localhost`` | Traefik's entry point |
| ``MOWDECK_E2E_WS_URL`` | base URL with ``ws://`` + ``/api/v1/realtime/ws`` | Realtime socket |
| ``MOWDECK_E2E_HTTP_TIMEOUT`` | ``10`` | seconds per HTTP request and socket handshake |
| ``MOWDECK_E2E_READY_TIMEOUT`` | ``30`` | new mower until Telemetry reports ``docked`` |
| ``MOWDECK_E2E_LIVE_TIMEOUT`` | ``30`` | command result and state on the socket |
| ``MOWDECK_E2E_DOCK_TIMEOUT`` | ``60`` | ``returning`` or ``docked`` after ``return_to_dock`` |
| ``MOWDECK_E2E_IDLE_SECONDS`` | ``90`` | idle time of the socket through Traefik |
"""

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

IDENTITY = "/api/v1/identity"
FLEET = "/api/v1/fleet"
TELEMETRY = "/api/v1/telemetry"

Message = dict[str, Any]


@dataclass(frozen=True)
class E2EConfig:
    """Where the stack is and how long to wait."""

    base_url: str
    ws_url: str
    http_timeout: float
    ready_timeout: float
    live_timeout: float
    dock_timeout: float
    idle_seconds: float
    run_id: str

    @classmethod
    def from_env(cls) -> "E2EConfig":
        """Read the ``MOWDECK_E2E_*`` variables."""
        env = os.environ
        base = env.get("MOWDECK_E2E_BASE_URL", "http://localhost").rstrip("/")
        ws_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return cls(
            base_url=base,
            ws_url=env.get("MOWDECK_E2E_WS_URL", f"{ws_base}/api/v1/realtime/ws"),
            http_timeout=float(env.get("MOWDECK_E2E_HTTP_TIMEOUT", "10")),
            ready_timeout=float(env.get("MOWDECK_E2E_READY_TIMEOUT", "30")),
            live_timeout=float(env.get("MOWDECK_E2E_LIVE_TIMEOUT", "30")),
            dock_timeout=float(env.get("MOWDECK_E2E_DOCK_TIMEOUT", "60")),
            idle_seconds=float(env.get("MOWDECK_E2E_IDLE_SECONDS", "90")),
            run_id=uuid4().hex[:8],
        )


@dataclass
class Actor:
    """A user of the stack with its current access token."""

    username: str
    password: str
    token: str = ""
    account_id: str = ""
    user_id: str = ""

    @property
    def headers(self) -> dict[str, str]:
        """``Authorization: Bearer <token>``."""
        return {"Authorization": f"Bearer {self.token}"}


def describe(response: httpx.Response) -> str:
    """Method, path, status and body of a response, for assertion messages."""
    return (
        f"{response.request.method} {response.request.url.path} -> "
        f"{response.status_code}: {response.text[:500]}"
    )


def new_actor(name: str, cfg: E2EConfig) -> Actor:
    """Unique username per run (``lukas-<run>``) and a random password."""
    return Actor(username=f"{name}-{cfg.run_id}", password=f"e2e-pw-{uuid4().hex}")


async def login(http: httpx.AsyncClient, actor: Actor) -> Actor:
    """Log in (``POST /identity/session``) and keep the access token."""
    response = await http.post(
        f"{IDENTITY}/session", json={"username": actor.username, "password": actor.password}
    )
    assert response.status_code == 200, describe(response)
    body = response.json()
    actor.token = body["access_token"]
    actor.account_id = actor.account_id or str(body["account_id"])
    return actor


async def register_owner(http: httpx.AsyncClient, cfg: E2EConfig, name: str) -> Actor:
    """Register an account with its owner (open registration) and log in."""
    actor = new_actor(name, cfg)
    response = await http.post(
        f"{IDENTITY}/register",
        json={
            "username": actor.username,
            "password": actor.password,
            "account_name": f"{name} household {cfg.run_id}",
        },
    )
    assert response.status_code == 201, describe(response)
    body = response.json()
    actor.account_id, actor.user_id = str(body["account_id"]), str(body["user_id"])
    return await login(http, actor)


async def create_subuser(
    http: httpx.AsyncClient, cfg: E2EConfig, owner: Actor, name: str, role_key: str
) -> Actor:
    """A sub-user of the owner's account with a seeded role at account level, logged in."""
    roles_response = await http.get(
        f"{IDENTITY}/accounts/{owner.account_id}/roles", headers=owner.headers
    )
    assert roles_response.status_code == 200, describe(roles_response)
    roles = roles_response.json()
    role_id = next((r["id"] for r in roles if r["key"] == role_key), None)
    assert role_id is not None, f"no role {role_key!r} in {[r['key'] for r in roles]}"
    actor = new_actor(name, cfg)
    response = await http.post(
        f"{IDENTITY}/accounts/{owner.account_id}/users",
        headers=owner.headers,
        json={
            "username": actor.username,
            "password": actor.password,
            "display_name": name.capitalize(),
            "must_change_password": False,
            "assignments": [
                {"role_id": role_id, "level": {"type": "account", "id": owner.account_id}}
            ],
        },
    )
    assert response.status_code == 201, describe(response)
    actor.user_id, actor.account_id = str(response.json()["id"]), owner.account_id
    return await login(http, actor)


async def wait_for[T](
    probe: Callable[[], Awaitable[tuple[T | None, Any]]],
    timeout: float,  # noqa: ASYNC109 - the deadline is the point of this helper
    what: str,
    interval: float = 0.5,
) -> T:
    """Poll ``probe`` until it returns a value; it also returns what it observed last.

    On timeout the assertion names the expectation and the last observation.
    """
    deadline = time.monotonic() + timeout
    last: Any = None
    while True:
        value, last = await probe()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}; last: {last}")
        await asyncio.sleep(interval)


@dataclass
class LiveSocket:
    """A Realtime socket that keeps every message, so expectations can look back."""

    ws: ClientConnection
    seen: list[Message] = field(default_factory=list)
    consumed: int = 0

    async def send(self, message: Message) -> None:
        """Send one JSON message."""
        await self.ws.send(json.dumps(message))

    def _summary(self) -> str:
        tail = self.seen[-5:]
        return f"{len(self.seen)} messages, types {[m.get('type') for m in self.seen]}, last {tail}"

    def mark(self) -> int:
        """A position in ``seen``; ``expect(..., after=mark)`` ignores older messages."""
        return len(self.seen)

    async def expect(
        self,
        predicate: Callable[[Message], bool],
        timeout: float,  # noqa: ASYNC109 - the deadline is the point of this helper
        what: str,
        *,
        after: int = 0,
    ) -> Message:
        """The first message (already received or arriving within ``timeout``) that matches."""
        for earlier in self.seen[after:]:
            if predicate(earlier):
                return earlier
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=left)
            except TimeoutError:
                break
            except ConnectionClosed as exc:
                raise AssertionError(
                    f"socket closed ({exc.rcvd}) while waiting for {what}; {self._summary()}"
                ) from exc
            message: Message = json.loads(raw)
            self.seen.append(message)
            if predicate(message):
                return message
        raise AssertionError(f"no {what} within {timeout}s; {self._summary()}")

    async def silent_for(self, seconds: float) -> None:
        """Assert that nothing arrives for ``seconds`` and the socket stays open."""
        try:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=seconds)
        except TimeoutError:
            return
        except ConnectionClosed as exc:
            raise AssertionError(f"socket closed while idle: {exc.rcvd}") from exc
        raise AssertionError(f"unexpected message while idle: {raw!r}")

    async def closed_code(self, timeout: float) -> int | None:  # noqa: ASYNC109
        """Read until the server closes the socket; its close code."""
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=left)
            except TimeoutError:
                break
            except ConnectionClosed as exc:
                return exc.rcvd.code if exc.rcvd is not None else None
            self.seen.append(json.loads(raw))
        raise AssertionError(f"socket not closed within {timeout}s; {self._summary()}")

    async def close(self) -> None:
        """Close the socket."""
        await self.ws.close()


async def connect_socket(cfg: E2EConfig, *, keepalive: bool = True) -> LiveSocket:
    """Connect without authenticating.

    ``keepalive=False`` turns off the client's own protocol pings, so only the server's pings
    keep an idle connection alive.
    """
    ws = await connect(
        cfg.ws_url,
        open_timeout=cfg.http_timeout,
        ping_interval=20 if keepalive else None,
    )
    return LiveSocket(ws)


async def open_socket(cfg: E2EConfig, actor: Actor, *, keepalive: bool = True) -> LiveSocket:
    """Connect, send ``auth`` with the actor's token and wait for ``ready``."""
    live = await connect_socket(cfg, keepalive=keepalive)
    await live.send({"type": "auth", "token": actor.token})
    ready = await live.expect(is_type("ready"), cfg.http_timeout, "ready")
    assert ready["principal"]["account_id"] == actor.account_id, ready
    return live


def is_type(kind: str, **fields: Any) -> Callable[[Message], bool]:
    """Match ``type`` and dotted fields written with ``__`` (``result__status="executed"``)."""

    def match(message: Message) -> bool:
        return message.get("type") == kind and all(
            _get(message, key) == value for key, value in fields.items()
        )

    return match


def _get(message: Message, dotted: str) -> Any:
    value: Any = message
    for part in dotted.split("__"):
        value = value.get(part) if isinstance(value, dict) else None
    return value
