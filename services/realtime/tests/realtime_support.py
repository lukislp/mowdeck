"""Helpers for the realtime tests (a plain module: the repository has no conftest.py).

``harness()`` builds the real app with the in-memory bus, the in-memory OpenFGA fake (it
evaluates the packaged model) and access tokens minted with ``mowdeck_auth.testing.TestKeys``,
and serves it with uvicorn in-process on a free port (the same server class as production), so
the tests talk real WebSockets with the ``websockets`` client in the test's event loop.
Fleet's and Identity's tuples are written the way those services write them.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import uvicorn
from fastapi import FastAPI
from mowdeck_auth.testing import TestKeys
from mowdeck_authz import RelationshipTuple, TupleWriter, account, mower, principal_subject
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import (
    IDENTITY_GRANTS_CHANGED,
    CommandEnvelope,
    CommandResult,
    CommandStatus,
    ConnectionKind,
    DriveError,
    DriveErrorCode,
    DriveSessionStarted,
    DriveStartReply,
    DriveStartRequest,
    DriveStopReply,
    DriveStopRequest,
    EventEnvelope,
    GrantsChanged,
    MowerActivity,
    MowerCapabilities,
    MowerCapabilitiesSnapshot,
    MowerState,
    Principal,
    PrincipalKind,
    utc_now,
)
from mowdeck_contracts.v1.subjects import (
    FLEET_DRIVE_START,
    FLEET_DRIVE_STOP,
    device_subject,
    telemetry_subject,
)
from mowdeck_service_kit.bus import (
    BusMessage,
    InMemoryBus,
    publish_command_result,
    publish_event,
)
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from realtime.adapters.auth import JwtTokenVerifier
from realtime.adapters.server import RealtimeServer
from realtime.main import build_app
from realtime.settings import Settings

WS_PATH = "/api/v1/realtime/ws"
T0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)


def make_settings(**overrides: Any) -> Settings:
    """Settings for tests (environment independent)."""
    values: dict[str, Any] = {
        "environment": "test",
        "log_format": "console",
        "nats_enabled": False,
        "shutdown_grace_seconds": 2,
        **overrides,
    }
    return Settings.model_validate(values)


def make_state(
    mower_id: UUID,
    account_id: UUID,
    *,
    activity: MowerActivity = MowerActivity.DOCKED,
    battery: int = 90,
    at: datetime = T0,
) -> MowerState:
    """A state of ``mower_id``."""
    return MowerState(
        mower_id=mower_id,
        account_id=account_id,
        observed_at=at,
        activity=activity,
        battery_percent=battery,
        connection=ConnectionKind.SIMULATED,
    )


@dataclass
class Person:
    """A principal with its access token."""

    principal: Principal
    token: str

    @property
    def account_id(self) -> UUID:
        """The account of the token."""
        assert self.principal.account_id is not None
        return self.principal.account_id


class Client:
    """A WebSocket client that keeps every received message."""

    def __init__(self, ws: ClientConnection) -> None:
        self.ws = ws
        self.seen: list[dict[str, Any]] = []
        self.skipped: list[dict[str, Any]] = []
        """Messages ``expect`` read but did not match; a later ``expect`` checks them first."""

    async def send(self, message: dict[str, Any]) -> None:
        """Send one JSON message."""
        await self.ws.send(json.dumps(message))

    async def recv(self, timeout: float = 3.0) -> dict[str, Any]:  # noqa: ASYNC109
        """The next message."""
        raw = await asyncio.wait_for(self.ws.recv(), timeout)
        message: dict[str, Any] = json.loads(raw)
        self.seen.append(message)
        return message

    async def expect(self, kind: str, timeout: float = 3.0, **fields: Any) -> dict[str, Any]:  # noqa: ASYNC109
        """The first message of ``kind`` (and ``fields``); skipped messages stay available.

        Messages on different subjects (for example ``state`` and ``command_result``) can
        arrive in any order, so a message that does not match is kept in ``skipped`` and a
        later ``expect`` finds it there before reading from the socket.
        """

        def matches(message: dict[str, Any]) -> bool:
            return message.get("type") == kind and all(
                message.get(k) == v for k, v in fields.items()
            )

        for index, message in enumerate(self.skipped):
            if matches(message):
                return self.skipped.pop(index)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            left = deadline - loop.time()
            if left <= 0:
                break
            try:
                message = await self.recv(left)
            except TimeoutError:
                break
            if matches(message):
                return message
            self.skipped.append(message)
        seen = [m.get("type") for m in self.seen]
        raise AssertionError(f"no {kind} {fields} within {timeout}s; seen {seen}")

    async def nothing(self, within: float = 0.3) -> None:
        """Assert that no message arrives within ``within`` seconds."""
        try:
            message = await self.recv(within)
        except TimeoutError:
            return
        raise AssertionError(f"unexpected message {message}")

    async def closed_code(self, timeout: float = 3.0) -> int | None:  # noqa: ASYNC109
        """Read until the server closes; the close code."""
        try:
            async with asyncio.timeout(timeout):
                while True:
                    await self.recv(timeout)
        except ConnectionClosed as exc:
            return exc.rcvd.code if exc.rcvd is not None else None
        raise AssertionError("not closed")  # pragma: no cover

    async def auth(self, token: str) -> dict[str, Any]:
        """Send ``auth`` and wait for ``ready``."""
        await self.send({"type": "auth", "token": token})
        return await self.expect("ready")

    async def subscribe(self, *mower_ids: UUID, request_id: str = "s") -> dict[str, Any]:
        """Subscribe and wait for ``subscribed``."""
        await self.send(
            {
                "type": "subscribe",
                "request_id": request_id,
                "mower_ids": [str(m) for m in mower_ids],
            }
        )
        return await self.expect("subscribed", request_id=request_id)


@dataclass
class Harness:
    """The served app and its fakes."""

    app: FastAPI
    url: str
    bus: InMemoryBus
    authz: InMemoryAuthorizer
    keys: TestKeys
    server: uvicorn.Server
    clients: list[Client] = field(default_factory=list)

    @property
    def fleet(self) -> TupleWriter:
        """Writes tuples like Fleet."""
        return TupleWriter(self.authz, owner="fleet")

    @property
    def identity(self) -> TupleWriter:
        """Writes tuples like Identity."""
        return TupleWriter(self.authz, owner="identity")

    async def owner(self) -> Person:
        """A new account and its owner."""
        principal = Principal(subject=uuid4(), account_id=uuid4(), kind=PrincipalKind.USER)
        subject = principal_subject(principal)
        assert principal.account_id is not None
        await self.identity.write(
            [
                RelationshipTuple(subject, "owner", account(principal.account_id)),
                RelationshipTuple(subject, "member", account(principal.account_id)),
            ]
        )
        return Person(principal, self.keys.mint(principal))

    async def viewer(self, of: Person) -> Person:
        """A sub-user of ``of``'s account with ``mower.view`` on the whole account."""
        principal = Principal(subject=uuid4(), account_id=of.account_id, kind=PrincipalKind.SUBUSER)
        subject = principal_subject(principal)
        await self.identity.write(
            [
                RelationshipTuple(subject, "member", account(of.account_id)),
                RelationshipTuple(subject, "grant_mower_view", account(of.account_id)),
            ]
        )
        return Person(principal, self.keys.mint(principal))

    async def revoke_view(self, person: Person, *, announce: bool = True) -> None:
        """Remove the viewer's grant and (optionally) publish ``identity.grants.changed``."""
        subject = principal_subject(person.principal)
        await self.identity.write(
            [], [RelationshipTuple(subject, "grant_mower_view", account(person.account_id))]
        )
        if announce:
            await publish_event(
                self.bus,
                EventEnvelope[GrantsChanged](
                    type=IDENTITY_GRANTS_CHANGED,
                    producer="identity",
                    account_id=person.account_id,
                    payload=GrantsChanged(account_id=person.account_id, subjects=[str(subject)]),
                ),
            )

    async def revoke_drive(self, person: Person) -> None:
        """Remove ``mower.drive`` from a :func:`driver` and publish ``identity.grants.changed``."""
        subject = principal_subject(person.principal)
        await self.identity.write(
            [], [RelationshipTuple(subject, "grant_mower_drive", account(person.account_id))]
        )
        await publish_event(
            self.bus,
            EventEnvelope[GrantsChanged](
                type=IDENTITY_GRANTS_CHANGED,
                producer="identity",
                account_id=person.account_id,
                payload=GrantsChanged(account_id=person.account_id, subjects=[str(subject)]),
            ),
        )

    async def add_mower(self, owner: Person) -> UUID:
        """A mower of the owner's account (Fleet's ``account`` tuple)."""
        mower_id = uuid4()
        await self.fleet.write(
            [RelationshipTuple(account(owner.account_id).as_subject(), "account", mower(mower_id))]
        )
        return mower_id

    async def publish_state(self, state: MowerState) -> None:
        """Publish like the Device-Gateway."""
        await self.bus.publish(telemetry_subject(state.mower_id), state)

    async def publish_result(
        self, mower_id: UUID, status: CommandStatus = CommandStatus.EXECUTED
    ) -> UUID:
        """Publish a command result; returns the command id."""
        command_id = uuid4()
        await publish_command_result(
            self.bus, CommandResult(command_id=command_id, mower_id=mower_id, status=status)
        )
        return command_id

    async def connect(
        self, *, origin: str | None = None, forwarded_for: str | None = None
    ) -> Client:
        """Open a socket (not yet authenticated); ``forwarded_for`` is the client address the
        gateway would report (``X-Forwarded-For``)."""
        headers = {"Origin": origin} if origin else {}
        if forwarded_for:
            headers["X-Forwarded-For"] = forwarded_for
        ws = await connect(self.url, additional_headers=headers or None, open_timeout=3)
        client = Client(ws)
        self.clients.append(client)
        return client

    async def connect_as(self, person: Person) -> Client:
        """Open a socket and authenticate."""
        client = await self.connect()
        await client.auth(person.token)
        return client


@asynccontextmanager
async def serve(app: FastAPI, settings: Settings) -> AsyncIterator[tuple[uvicorn.Server, str]]:
    """Serve ``app`` with :class:`RealtimeServer` on a free local port."""
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        lifespan="on",
        log_config=None,
        ws_ping_interval=settings.realtime_ping_seconds or None,
        ws_ping_timeout=settings.realtime_ping_seconds or None,
        ws_max_size=settings.realtime_max_message_bytes,
        timeout_graceful_shutdown=2,
    )
    server = RealtimeServer(config)
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield server, f"ws://127.0.0.1:{port}{WS_PATH}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)


@asynccontextmanager
async def harness(
    *,
    bus: Any = None,
    authz: InMemoryAuthorizer | None = None,
    background_tasks: bool = True,
    entitlements: Any = None,
    **overrides: Any,
) -> AsyncIterator[Harness]:
    """The app with fakes, served in-process."""
    settings = make_settings(**overrides)
    fake = authz or InMemoryAuthorizer()
    active_bus = bus or InMemoryBus()
    keys = TestKeys()
    app = build_app(
        settings,
        bus=active_bus,
        authorizer=fake,
        verifier=JwtTokenVerifier(keys.verifier()),
        background_tasks=background_tasks,
        entitlements=entitlements,
    )
    async with serve(app, settings) as (server, url):
        h = Harness(app, url, active_bus, fake, keys, server)
        try:
            yield h
        finally:
            for client in h.clients:
                await client.ws.close()


def short_token(keys: TestKeys, principal: Principal, seconds: float) -> str:
    """A token that expires in about ``seconds`` (``exp`` has whole seconds)."""
    return keys.mint(principal, ttl=timedelta(seconds=seconds))


class FakeFleet:
    """Answers ``fleet.drive.start``/``stop`` on the bus like Fleet (records the requests)."""

    def __init__(self) -> None:
        self.starts: list[DriveStartRequest] = []
        self.stops: list[DriveStopRequest] = []
        self.error: DriveErrorCode | None = None
        """Answer every start with this error instead of a session."""

    async def attach(self, bus: InMemoryBus) -> None:
        """Subscribe the responders."""

        async def start(message: BusMessage) -> None:
            request = message.model(DriveStartRequest)
            self.starts.append(request)
            if self.error is not None:
                reply = DriveStartReply(error=DriveError(code=self.error, detail="refused"))
            else:
                now = utc_now()
                reply = DriveStartReply(
                    started=DriveSessionStarted(
                        session_id=uuid4(),
                        mower_id=request.mower_id,
                        account_id=request.account_id,
                        started_at=now,
                        expires_at=now + timedelta(seconds=request.max_seconds),
                    )
                )
            await message.respond(reply)

        async def stop(message: BusMessage) -> None:
            self.stops.append(message.model(DriveStopRequest))
            await message.respond(DriveStopReply(found=True))

        await bus.subscribe(FLEET_DRIVE_START, start, queue="fleet")
        await bus.subscribe(FLEET_DRIVE_STOP, stop, queue="fleet")


async def publish_capabilities(
    bus: InMemoryBus,
    mower_id: UUID,
    account_id: UUID,
    *,
    manual_drive: bool = True,
    manual_drive_blades: bool = True,
) -> None:
    """Publish a capability snapshot like the Device-Gateway."""
    await bus.publish(
        device_subject(mower_id, "capabilities"),
        MowerCapabilitiesSnapshot(
            mower_id=mower_id,
            account_id=account_id,
            observed_at=utc_now(),
            capabilities=MowerCapabilities(
                manual_drive=manual_drive, manual_drive_blades=manual_drive_blades
            ),
        ),
    )


def frames_sent(bus: InMemoryBus, mower_id: UUID) -> list[CommandEnvelope]:
    """Every command envelope published for ``mower_id``."""
    return [m.model(CommandEnvelope) for m in bus.messages(f"command.mower.{mower_id}")]


async def driver(h: Harness, of: Person) -> Person:
    """A sub-user of ``of``'s account with ``mower.view`` and ``mower.drive`` on the account."""
    principal = Principal(subject=uuid4(), account_id=of.account_id, kind=PrincipalKind.SUBUSER)
    subject = principal_subject(principal)
    await h.identity.write(
        [
            RelationshipTuple(subject, "member", account(of.account_id)),
            RelationshipTuple(subject, "grant_mower_view", account(of.account_id)),
            RelationshipTuple(subject, "grant_mower_drive", account(of.account_id)),
        ]
    )
    return Person(principal, h.keys.mint(principal))
