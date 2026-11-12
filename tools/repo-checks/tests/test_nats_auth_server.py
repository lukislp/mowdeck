# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The generated NATS permissions against a real server (ADR 0074).

Skipped unless ``MOWDECK_TEST_NATS_AUTH_URL`` points at a NATS server with JetStream that runs
``infra/compose/nats/auth.conf`` with the development passwords of ``infra/compose/.env.example``
(CI job ``python``)::

    grep '^NATS_PASSWORD_' infra/compose/.env.example > /tmp/nats.env
    docker run -d --name mowdeck-test-nats-auth -p 4223:4222 --env-file /tmp/nats.env \
        -v "$PWD/infra/compose/nats:/etc/nats/mowdeck:ro" \
        public.ecr.aws/docker/library/nats:2.15.0-alpine -c /etc/nats/mowdeck/auth.conf -js
    MOWDECK_TEST_NATS_AUTH_URL=nats://127.0.0.1:4223 uv run pytest tools/repo-checks

It checks that a service user can do what its service does and nothing else: no anonymous
client, no subject outside the allow list, no other user's inbox, no consumer with a broader
filter, no stream changes except by the ``streams`` user, and stored messages removed only by
the owner of what they are about (ADR 0082): Fleet purges the subjects of a mower, Identity
erases single domain events. Every user whose service verifies tokens creates the deny list's
consumer as its code does, and a refused one fails open. Every service that forgets a deleted
user creates its durable consumer of ``identity.user.deleted`` with its own adapter and receives
what Identity publishes.

The server must be started for the test run with the file of the checkout (CI starts it in the
job): a server that runs an older file still refuses what the new file allows, which is what
``deploy.sh`` reloads NATS for (the v0.69.4 deploy).
"""

import asyncio
import importlib
import importlib.util
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import nats
import nats.errors
import pytest
from mowdeck_auth import RevocationFeed, RevocationList, subscribe_revocations
from mowdeck_contracts.v1 import (
    IDENTITY_ACCESS_REVOKED,
    IDENTITY_USER_DELETED,
    AccessRevoked,
    AccessScope,
    EventEnvelope,
    Principal,
    PrincipalKind,
    UserChanged,
)
from mowdeck_contracts.v1.subjects import ALL_ACCESS_REVOKED, access_revoked_subject
from mowdeck_service_kit.bus import BusError, BusMessage, JetStreamBus, publish_event
from mowdeck_service_kit.user_erasure import UserErasure
from nats.aio.client import Client

URL = os.environ.get("MOWDECK_TEST_NATS_AUTH_URL")
pytestmark = pytest.mark.skipif(
    not URL, reason="needs MOWDECK_TEST_NATS_AUTH_URL (a NATS server running nats/auth.conf)"
)

REPO = Path(__file__).resolve().parents[3]


def _generator() -> ModuleType:
    """tools/repo-checks/scripts/nats_auth.py, the subject map the server's file comes from."""
    path = REPO / "tools" / "repo-checks" / "scripts" / "nats_auth.py"
    spec = importlib.util.spec_from_file_location("nats_auth_for_the_server_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nats_auth = _generator()


def _password(user: str) -> str:
    variable = "NATS_PASSWORD_" + user.upper().replace("-", "_")
    for line in (REPO / "infra" / "compose" / ".env.example").read_text("utf-8").splitlines():
        if line.startswith(f"{variable}="):
            return line.split("=", 1)[1]
    raise KeyError(variable)


def _bus(user: str) -> JetStreamBus:
    assert URL is not None
    return JetStreamBus(
        URL,
        name=f"test-{user}",
        user=user,
        password=_password(user),
        manage_streams=user == "streams",
        stream_wait_seconds=10,
        publish_timeout_seconds=1,
    )


class UserClient:
    """A raw nats-py client of one user that records the server's permission errors."""

    def __init__(self, user: str) -> None:
        self.user = user
        self.errors: list[str] = []
        self.nc: Client | None = None

    async def __aenter__(self) -> "UserClient":
        async def on_error(exc: Exception) -> None:
            self.errors.append(str(exc).lower())

        assert URL is not None
        self.nc = await nats.connect(
            URL,
            user=self.user,
            password=_password(self.user),
            inbox_prefix=f"_INBOX.{self.user}",
            error_cb=on_error,
            allow_reconnect=False,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        assert self.nc is not None
        await self.nc.close()

    async def violation(self, needle: str) -> bool:
        assert self.nc is not None
        await self.nc.flush()
        for _ in range(50):
            if any("permissions violation" in e and needle.lower() in e for e in self.errors):
                return True
            await asyncio.sleep(0.05)
        return False


@pytest.fixture
async def streams() -> AsyncIterator[None]:
    """The nats-streams job: the only user that may create the streams."""
    bus = _bus("streams")
    await bus.connect()
    await bus.close()
    yield


async def test_a_client_without_credentials_is_refused() -> None:
    assert URL is not None
    with pytest.raises(nats.errors.Error, match="Authorization Violation"):
        await nats.connect(URL, allow_reconnect=False, connect_timeout=2)
    with pytest.raises(nats.errors.Error, match="Authorization Violation"):
        await nats.connect(
            URL,
            user="fleet",
            password="wrong-password-0123456789",  # noqa: S106 - a test value
            allow_reconnect=False,
        )


async def test_a_service_waits_for_the_streams_and_does_its_work(streams: None) -> None:
    domain = uuid4().hex[:8]
    fleet = _bus("fleet")
    gateway = _bus("device-gateway")
    billing = _bus("billing")
    await asyncio.gather(fleet.connect(), gateway.connect(), billing.connect())
    try:
        # Request/reply: the gateway asks Fleet for credentials; Fleet answers into the
        # gateway's inbox (allow_responses), which only the gateway may read.
        async def credentials(message: BusMessage) -> None:
            await message.respond(b"secret-reply")

        served = await fleet.subscribe("fleet.mowers.credentials", credentials, queue="fleet")
        await fleet._client().flush()  # the server knows the subscription before the request
        reply = await gateway.request("fleet.mowers.credentials", b"{}", timeout=2)
        assert reply.data == b"secret-reply"
        await served.unsubscribe()

        # JetStream: Billing consumes identity.account.created and publishes its own events.
        received: list[str] = []

        async def account(message: BusMessage) -> None:
            received.append(message.subject)

        consumer = await billing.subscribe(
            "event.identity.account.created", account, durable=f"test-billing-{domain}"
        )
        identity = _bus("identity")
        await identity.connect()
        try:
            result = await identity.publish("event.identity.account.created", b"{}")
            assert result.stream == "EVENTS"
        finally:
            await identity.close()
        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.05)
        assert received == ["event.identity.account.created"]
        info = await billing._jetstream().consumer_info("EVENTS", f"test-billing-{domain}")
        assert info.config.deliver_subject == f"_INBOX.billing.durable.EVENTS.test-billing-{domain}"
        await consumer.unsubscribe()
        assert (await billing.publish("event.billing.subscription.changed", b"{}")).stream
    finally:
        await asyncio.gather(fleet.close(), gateway.close(), billing.close())


async def test_a_service_cannot_publish_outside_its_allow_list(streams: None) -> None:
    billing = _bus("billing")
    await billing.connect()
    try:
        # A stream subject: JetStream never acknowledges the refused message.
        with pytest.raises(BusError):
            await billing.publish(f"command.mower.{uuid4()}", b"{}")
        # Another service's events.
        with pytest.raises(BusError):
            await billing.publish("event.identity.account.created", b"{}")
    finally:
        await billing.close()
    async with UserClient("vision") as vision:
        assert vision.nc is not None
        await vision.nc.publish("fleet.mowers.credentials", b"{}")
        assert await vision.violation('publish to "fleet.mowers.credentials"')
        await vision.nc.publish("relay.camera.attach", b"{}")
        assert await vision.violation('publish to "relay.camera.attach"')


async def test_a_service_cannot_read_what_is_not_its_own(streams: None) -> None:
    async with UserClient("notifications") as notifications:
        nc = notifications.nc
        assert nc is not None
        # Another user's inbox (the replies with mower credentials go to the gateway's).
        await nc.subscribe("_INBOX.device-gateway.>")
        assert await notifications.violation('subscription to "_inbox.device-gateway.>"')
        # The whole inbox space, and a request subject it does not serve.
        await nc.subscribe("_INBOX.>")
        assert await notifications.violation('subscription to "_inbox.>"')
        await nc.subscribe("fleet.mowers.credentials")
        assert await notifications.violation('subscription to "fleet.mowers.credentials"')
        # A JetStream consumer with a broader filter than allowed (all events), through the
        # subject based API and through the legacy one.
        config = {"name": "evil", "deliver_subject": nc.new_inbox(), "filter_subject": ">"}
        body: dict[str, object] = {"stream_name": "EVENTS", "config": config}
        with pytest.raises(nats.errors.TimeoutError):
            await nc.request(
                "$JS.API.CONSUMER.CREATE.EVENTS.evil.event.>", json.dumps(body).encode(), 1
            )
        assert await notifications.violation("$js.api.consumer.create.events.evil.event.>")
        with pytest.raises(nats.errors.TimeoutError):
            await nc.request("$JS.API.CONSUMER.CREATE.EVENTS", json.dumps(body).encode(), 1)
        # The filter in the request must equal the one in the (allowed) API subject.
        config["filter_subject"] = "event.>"
        answer = await nc.request(
            "$JS.API.CONSUMER.CREATE.EVENTS.evil.event.identity.>", json.dumps(body).encode(), 2
        )
        assert "error" in json.loads(answer.data)


async def test_fleet_purges_the_subjects_of_a_mower_and_nobody_else_can(streams: None) -> None:
    mower_id = uuid4()
    gateway, fleet = _bus("device-gateway"), _bus("fleet")
    await gateway.connect()
    await fleet.connect()
    try:
        await gateway.publish(f"telemetry.mower.{mower_id}", b"{}")
        await gateway.publish(f"device.mower.{mower_id}.map", b"{}")
        await fleet.publish(f"command.mower.{mower_id}", b"{}")
        assert await fleet.count_subject(f"telemetry.mower.{mower_id}") == 1
        assert await fleet.purge_subject(f"telemetry.mower.{mower_id}") == 1
        assert await fleet.purge_subject(f"device.mower.{mower_id}.>") == 1
        assert await fleet.purge_subject(f"command.mower.{mower_id}") == 1
        assert await fleet.purge_subject(f"command.result.mower.{mower_id}") == 0
        assert await fleet.purge_subject(f"camera.mower.{mower_id}.>") == 0
        assert await fleet.count_subject(f"device.mower.{mower_id}.>") == 0
        # Not the domain events, and not message by message.
        with pytest.raises(BusError):
            await fleet.purge_subject("event.mower.removed")
        with pytest.raises(BusError):
            _ = [m async for m in fleet.stored_messages("event.mower.removed")]
        # The publisher of the states may not remove them, nor may a consumer.
        with pytest.raises(BusError):
            await gateway.purge_subject(f"telemetry.mower.{mower_id}")
    finally:
        await gateway.close()
        await fleet.close()
    async with UserClient("vision") as vision:
        assert vision.nc is not None
        with pytest.raises(nats.errors.TimeoutError):
            await vision.nc.request("$JS.API.STREAM.PURGE.DEVICE", b"{}", 1)
        assert await vision.violation("$js.api.stream.purge.device")


async def test_identity_erases_single_events_and_nobody_else_can(streams: None) -> None:
    marker = uuid4().hex
    identity, notifications = _bus("identity"), _bus("notifications")
    await identity.connect()
    await notifications.connect()
    try:
        await notifications.publish("event.notifications.raised", f"raised {marker}".encode())
        await identity.publish("event.identity.user.created", f"created {marker}".encode())
        mine = [m async for m in identity.stored_messages("event.>") if marker.encode() in m.data]
        assert sorted(m.subject for m in mine) == [
            "event.identity.user.created",
            "event.notifications.raised",
        ]
        assert [await identity.erase_message(m) for m in mine] == [True, True]
        assert await identity.erase_message(mine[0]) is False
        left = [m async for m in identity.stored_messages("event.>") if marker.encode() in m.data]
        assert left == []
        # The read can be bounded and resumed (a batched direct get on the stream).
        end = await identity.last_sequence("event.>")
        bounded = [m async for m in identity.stored_messages("event.>", after=end - 1, until=end)]
        assert len(bounded) <= 1 and end >= mine[-1].sequence
        # Identity purges no stream as a whole, and reads no other stream.
        with pytest.raises(BusError):
            await identity.purge_subject("event.>")
        with pytest.raises(BusError):
            _ = [m async for m in identity.stored_messages("telemetry.mower.*")]
        # A publisher of events can neither read the stream by sequence nor delete from it.
        with pytest.raises(BusError):
            _ = [m async for m in notifications.stored_messages("event.>")]
    finally:
        await identity.close()
        await notifications.close()
    async with UserClient("notifications") as client:
        assert client.nc is not None
        with pytest.raises(nats.errors.TimeoutError):
            await client.nc.request("$JS.API.STREAM.MSG.DELETE.EVENTS", b'{"seq": 1}', 1)
        assert await client.violation("$js.api.stream.msg.delete.events")
        with pytest.raises(nats.errors.TimeoutError):
            await client.nc.request("$JS.API.DIRECT.GET.EVENTS", b'{"seq": 1}', 1)
        assert await client.violation("$js.api.direct.get.events")


async def test_every_token_verifier_hears_a_revocation_and_only_identity_sends_one(
    streams: None,
) -> None:
    """The deny list of deleted users and erased accounts (ADR 0082): Identity publishes an
    entry on the subject of its id, a service's verifier reads it live and again after a
    restart, and no other service can put an entry there."""
    now = datetime.now(UTC)
    entry = AccessRevoked(
        scope=AccessScope.SUBJECT,
        id=uuid4(),
        issued_before=now,
        expires_at=now + timedelta(minutes=11),
    )
    envelope = EventEnvelope[AccessRevoked](
        type=IDENTITY_ACCESS_REVOKED, producer="identity", payload=entry
    )
    token = Principal(subject=entry.id, account_id=uuid4(), kind=PrincipalKind.USER)
    identity, planner, restarted = _bus("identity"), _bus("planner"), _bus("planner")
    await identity.connect()
    await planner.connect()
    try:
        live = RevocationList()
        await subscribe_revocations(planner, live)
        await publish_event(identity, envelope, subject=access_revoked_subject(entry.id))
        for _ in range(100):
            if live.refuses(token, now):
                break
            await asyncio.sleep(0.05)
        assert live.refuses(token, now)

        await restarted.connect()  # a replica that starts while the entry is in force
        replayed = RevocationList()
        await subscribe_revocations(restarted, replayed)
        for _ in range(100):
            if replayed.refuses(token, now):
                break
            await asyncio.sleep(0.05)
        assert replayed.refuses(token, now)

        with pytest.raises(BusError):  # only Identity publishes below event.identity
            await publish_event(planner, envelope, subject=access_revoked_subject(uuid4()))
    finally:
        await identity.close()
        await planner.close()
        await restarted.close()


TOKEN_VERIFIERS = sorted(
    user for user, access in nats_auth.USERS.items() if ALL_ACCESS_REVOKED in access.consume
)
"""Every user whose service verifies access tokens, from the map itself (a new one is covered
without a change here)."""


def test_every_service_that_verifies_tokens_is_in_the_map() -> None:
    assert len(TOKEN_VERIFIERS) >= 11
    assert {"identity", "realtime", "fleet", "scheduler", "telemetry"} <= set(TOKEN_VERIFIERS)


@pytest.mark.parametrize("user", TOKEN_VERIFIERS)
async def test_every_token_verifier_creates_the_deny_list_consumer(
    streams: None, user: str
) -> None:
    """The v0.69.4 deploy: the deny list's ephemeral consumer is created through
    ``$JS.API.CONSUMER.CREATE.EVENTS.<name>.event.identity.access.revoked.*`` (a filter ending
    in ``*``, in the API subject). Each user creates it exactly as its service does, on a
    server that runs the generated file, and the server records no permission error."""
    errors: list[str] = []
    bus = _bus(user)
    await bus.connect()
    bus._client()._error_cb = _record(errors)  # the server's -ERR lines, as nats-py reports them
    feed = RevocationFeed(RevocationList(), retry_seconds=60)
    try:
        await feed.start(bus)
        assert feed.subscribed, f"{user}: the deny list's consumer was refused"
        assert await feed.readiness_check().check() is True
        await bus._client().flush()
    finally:
        await feed.stop()
        await bus.close()
    assert not [e for e in errors if "permissions violation" in e], errors


async def test_a_refused_deny_list_consumer_fails_open(streams: None) -> None:
    """The same request from a user the server does not allow it (a NATS that still runs the
    permissions of an older release looks like this): the server refuses it with a permissions
    violation, the feed reports it and does not raise, so the service would start."""
    assert "camera-relay" not in TOKEN_VERIFIERS
    errors: list[str] = []
    bus = _bus("camera-relay")
    await bus.connect()
    bus._client()._error_cb = _record(errors)
    feed = RevocationFeed(RevocationList(), retry_seconds=60)
    try:
        await feed.start(bus)
        assert not feed.subscribed
        assert await feed.readiness_check().check() is False
    finally:
        await feed.stop()
        await bus.close()
    assert any(
        "permissions violation" in e and "event.identity.access.revoked.*" in e for e in errors
    ), errors


FORGETTING = sorted(
    adapter.parents[4].name
    for adapter in (REPO / "services").glob("*/src/*/adapters/bus/user_erasure.py")
)
"""Every service with the user erasure's adapter (ADR 0082, "delete me"), from the repository
(a new one is covered without a change here)."""


def test_every_service_that_forgets_a_user_is_found() -> None:
    assert {"fleet", "notifications", "scheduler", "telemetry", "vision"} <= set(FORGETTING)


@pytest.mark.parametrize("service", FORGETTING)
async def test_every_service_that_forgets_a_user_receives_the_event(
    streams: None, service: str
) -> None:
    """Each service subscribes with its own adapter (``subscribe_user_erasure``: the durable
    ``<service>-users`` on ``event.identity.user.deleted``), on a server that runs the generated
    file, and its use case gets the user of the event Identity publishes."""
    adapter = importlib.import_module(f"{service}.adapters.bus.user_erasure")
    forgotten: list[tuple[object, object]] = []

    async def forget(user_id: object, account_id: object) -> int:
        forgotten.append((user_id, account_id))
        return 0

    async def nothing_left(_: object) -> dict[str, int]:
        return {}

    errors: list[str] = []
    bus, identity = _bus(service), _bus("identity")
    await asyncio.gather(bus.connect(), identity.connect())
    bus._client()._error_cb = _record(errors)
    user_id, account_id = uuid4(), uuid4()
    try:
        subscription = await adapter.subscribe_user_erasure(bus, UserErasure(forget, nothing_left))
        envelope = EventEnvelope[UserChanged](
            type=IDENTITY_USER_DELETED,
            version=2,
            producer="identity",
            account_id=account_id,
            payload=UserChanged(user_id=user_id, account_id=account_id, reason="self"),
        )
        await publish_event(identity, envelope)
        for _ in range(100):
            if (user_id, account_id) in forgotten:
                break
            await asyncio.sleep(0.05)
        info = await bus._jetstream().consumer_info("EVENTS", adapter.USERS_CONSUMER)
        await subscription.unsubscribe()
    finally:
        await asyncio.gather(bus.close(), identity.close())
    assert (user_id, account_id) in forgotten
    assert f"{service}-users" == adapter.USERS_CONSUMER
    assert info.config.filter_subject == "event.identity.user.deleted"
    assert not [e for e in errors if "permissions violation" in e], errors


def _record(errors: list[str]) -> Callable[[Exception], Awaitable[None]]:
    async def on_error(exc: Exception) -> None:
        errors.append(str(exc).lower())

    return on_error


async def test_only_the_streams_job_changes_streams(streams: None) -> None:
    async with UserClient("fleet") as fleet:
        nc = fleet.nc
        assert nc is not None
        for api in ("STREAM.UPDATE.EVENTS", "STREAM.DELETE.EVENTS", "STREAM.PURGE.EVENTS"):
            with pytest.raises(nats.errors.TimeoutError):
                await nc.request(f"$JS.API.{api}", b"{}", 1)
            assert await fleet.violation(f"$js.api.{api.lower()}")
