"""The WebSocket protocol end to end: uvicorn in-process, real sockets, in-memory bus and OpenFGA.

Authentication (timeout, bad token, service principal, expiry, refresh), subscriptions with
allowed and denied ids, delivery of states and command results, isolation between accounts,
re-checks (``identity.grants.changed`` and the periodic timer), limits and shutdown.
"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import CommandStatus, MowerActivity, Principal, PrincipalKind
from mowdeck_contracts.v1.realtime import RealtimeCloseCode
from realtime_support import T0, harness, make_state, short_token
from structlog.testing import capture_logs


async def test_realtime_auth_timeout_closes_4401() -> None:
    async with harness(realtime_auth_timeout_seconds=0.3) as h:
        client = await h.connect()
        assert await client.closed_code() == RealtimeCloseCode.UNAUTHORIZED


async def test_realtime_bad_token_closes_4401_after_an_error() -> None:
    async with harness() as h:
        client = await h.connect()
        await client.send({"type": "auth", "token": "not-a-jwt"})
        error = await client.expect("error")
        assert error["code"] == "unauthorized"
        assert await client.closed_code() == RealtimeCloseCode.UNAUTHORIZED


async def test_realtime_first_message_must_be_auth() -> None:
    async with harness() as h:
        client = await h.connect()
        await client.send({"type": "ping"})
        assert await client.closed_code() == RealtimeCloseCode.UNAUTHORIZED
        garbage = await h.connect()
        await garbage.send({"type": "nonsense"})
        assert await garbage.closed_code() == RealtimeCloseCode.UNAUTHORIZED


async def test_realtime_service_principal_is_refused() -> None:
    async with harness() as h:
        service = Principal(subject=uuid4(), kind=PrincipalKind.SERVICE)
        client = await h.connect()
        await client.send({"type": "auth", "token": h.keys.mint(service)})
        assert await client.closed_code() == RealtimeCloseCode.UNAUTHORIZED


async def test_realtime_ready_summarises_the_principal() -> None:
    async with harness() as h:
        owner = await h.owner()
        client = await h.connect()
        ready = await client.auth(owner.token)
        assert ready["principal"]["subject"] == str(owner.principal.subject)
        assert ready["principal"]["account_id"] == str(owner.account_id)
        assert ready["principal"]["kind"] == "user"
        assert ready["token_expires_at"] > ready["server_time"]
        assert h.app.state.hub.connections_of(user=owner.principal.subject) == 1


async def test_realtime_subscribe_reports_allowed_and_denied_ids() -> None:
    async with harness() as h:
        owner = await h.owner()
        stranger = await h.owner()
        own = await h.add_mower(owner)
        foreign = await h.add_mower(stranger)
        unknown = uuid4()
        client = await h.connect_as(owner)
        subscribed = await client.subscribe(own, foreign, unknown, own)
        assert subscribed["mower_ids"] == [str(own)]
        assert subscribed["denied"] == [str(foreign), str(unknown)]
        # A state of the foreign mower is never delivered, the own one is.
        await h.publish_state(make_state(foreign, stranger.account_id))
        await h.publish_state(make_state(own, owner.account_id, activity=MowerActivity.MOWING))
        state = await client.expect("state")
        assert state["mower_id"] == str(own)
        assert state["state"]["activity"] == "mowing"
        await client.nothing()


async def test_realtime_state_and_command_result_are_delivered() -> None:
    async with harness() as h:
        owner = await h.owner()
        viewer = await h.viewer(owner)
        mower_id = await h.add_mower(owner)
        first = await h.connect_as(owner)
        second = await h.connect_as(viewer)
        await first.subscribe(mower_id)
        await second.subscribe(mower_id)
        command_id = await h.publish_result(mower_id, CommandStatus.EXECUTED)
        await h.publish_state(make_state(mower_id, owner.account_id))
        for client in (first, second):
            result = await client.expect("command_result")
            assert result["mower_id"] == str(mower_id)
            assert result["result"]["command_id"] == str(command_id)
            assert result["result"]["status"] == "executed"
            assert (await client.expect("state"))["state"]["activity"] == "docked"


async def test_realtime_link_fields_pass_through_unchanged() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        client = await h.connect_as(owner)
        await client.subscribe(mower_id)
        state = make_state(mower_id, owner.account_id, at=T0)
        beat = state.model_copy(update={"heartbeat_at": T0 + timedelta(minutes=5)})
        await h.publish_state(beat)
        live = (await client.expect("state"))["state"]
        await h.publish_state(beat.model_copy(update={"connected": False}))
        lost = (await client.expect("state"))["state"]
    assert live["observed_at"].startswith("2026-10-09T12:00:00")  # the values' time stays
    assert live["heartbeat_at"].startswith("2026-10-09T12:05:00")
    assert live["connected"] is True
    assert lost["connected"] is False and lost["heartbeat_at"] == live["heartbeat_at"]


async def test_realtime_state_of_another_account_is_not_forwarded() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        client = await h.connect_as(owner)
        await client.subscribe(mower_id)
        await h.publish_state(make_state(mower_id, uuid4()))  # a wrong account in the payload
        await client.nothing()


async def test_realtime_new_subscription_gets_the_newest_known_state() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.publish_state(make_state(mower_id, owner.account_id, battery=55))
        client = await h.connect_as(owner)
        await client.subscribe(mower_id)
        state = await client.expect("state")
        assert state["state"]["battery_percent"] == 55
        # Nobody else gets the cached state of that mower.
        stranger = await h.owner()
        other = await h.connect_as(stranger)
        assert (await other.subscribe(mower_id))["denied"] == [str(mower_id)]
        await other.nothing()


async def test_realtime_unsubscribe_stops_updates() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        client = await h.connect_as(owner)
        await client.subscribe(mower_id)
        await client.send({"type": "unsubscribe", "request_id": "u", "mower_ids": [str(mower_id)]})
        answer = await client.expect("unsubscribed", request_id="u")
        assert answer["mower_ids"] == [str(mower_id)]
        assert h.app.state.hub.subscribers(mower_id) == 0
        await h.publish_state(make_state(mower_id, owner.account_id))
        await h.publish_result(mower_id)
        await client.nothing()


async def test_realtime_ping_and_invalid_messages() -> None:
    async with harness() as h:
        owner = await h.owner()
        client = await h.connect_as(owner)
        await client.send({"type": "ping", "request_id": "p1"})
        assert (await client.expect("pong"))["request_id"] == "p1"
        await client.ws.send("{not json")
        assert (await client.expect("error"))["code"] == "invalid_message"
        await client.send({"type": "subscribe", "mower_ids": ["nope"]})
        assert (await client.expect("error"))["code"] == "invalid_message"
        await client.send({"type": "ping"})
        await client.expect("pong")  # the socket is still open


async def test_realtime_binary_and_oversized_frames_close_the_socket() -> None:
    async with harness(realtime_max_message_bytes=1024) as h:
        owner = await h.owner()
        binary = await h.connect_as(owner)
        await binary.ws.send(b"\x00\x01")
        assert await binary.closed_code() == RealtimeCloseCode.PROTOCOL_ERROR
        big = await h.connect_as(owner)
        await big.ws.send("x" * 4096)
        assert await big.closed_code() == RealtimeCloseCode.MESSAGE_TOO_BIG


async def test_realtime_grants_changed_revokes_the_subscription() -> None:
    async with harness() as h:
        owner = await h.owner()
        viewer = await h.viewer(owner)
        mower_id = await h.add_mower(owner)
        owner_client = await h.connect_as(owner)
        viewer_client = await h.connect_as(viewer)
        await owner_client.subscribe(mower_id)
        assert (await viewer_client.subscribe(mower_id))["mower_ids"] == [str(mower_id)]
        await h.revoke_view(viewer)
        revoked = await viewer_client.expect("revoked")
        assert revoked["mower_ids"] == [str(mower_id)]
        assert revoked["reason"] == "access_revoked"
        await h.publish_state(make_state(mower_id, owner.account_id))
        assert (await owner_client.expect("state"))["mower_id"] == str(mower_id)
        await viewer_client.nothing()
        await owner_client.nothing()  # the owner was not affected


async def test_realtime_periodic_recheck_revokes_without_an_event() -> None:
    async with harness(realtime_recheck_seconds=0.2) as h:
        owner = await h.owner()
        viewer = await h.viewer(owner)
        mower_id = await h.add_mower(owner)
        client = await h.connect_as(viewer)
        await client.subscribe(mower_id)
        await h.revoke_view(viewer, announce=False)
        revoked = await client.expect("revoked", timeout=3)
        assert revoked["mower_ids"] == [str(mower_id)]


async def test_realtime_token_expiry_closes_4401() -> None:
    async with harness() as h:
        owner = await h.owner()
        client = await h.connect()
        await client.auth(short_token(h.keys, owner.principal, 2))
        assert await client.closed_code(timeout=5) == RealtimeCloseCode.UNAUTHORIZED
        assert any(m.get("message") == "token expired" for m in client.seen)


async def test_realtime_reauth_extends_the_socket_and_rechecks() -> None:
    async with harness() as h:
        owner = await h.owner()
        viewer = await h.viewer(owner)
        mower_id = await h.add_mower(owner)
        client = await h.connect()
        await client.auth(short_token(h.keys, viewer.principal, 2))
        await client.subscribe(mower_id)
        await h.revoke_view(viewer, announce=False)
        ready = await client.auth(viewer.token)  # a fresh token: ready again, then a re-check
        assert ready["principal"]["subject"] == str(viewer.principal.subject)
        assert (await client.expect("revoked"))["mower_ids"] == [str(mower_id)]
        await asyncio.sleep(2.5)  # past the first token's expiry
        await client.send({"type": "ping", "request_id": "alive"})
        assert (await client.expect("pong"))["request_id"] == "alive"


async def test_realtime_reauth_with_another_principal_closes_4401() -> None:
    async with harness() as h:
        owner = await h.owner()
        other = await h.owner()
        client = await h.connect_as(owner)
        await client.send({"type": "auth", "token": other.token})
        assert await client.closed_code() == RealtimeCloseCode.UNAUTHORIZED


async def test_realtime_connection_limits_close_4429() -> None:
    async with harness(
        realtime_max_connections_per_user=2, realtime_max_connections_per_account=3
    ) as h:
        owner = await h.owner()
        viewer = await h.viewer(owner)
        await h.connect_as(owner)
        await h.connect_as(owner)
        third = await h.connect()
        await third.send({"type": "auth", "token": owner.token})
        assert (await third.expect("error"))["code"] == "limit_exceeded"
        assert await third.closed_code() == RealtimeCloseCode.LIMIT_EXCEEDED
        await h.connect_as(viewer)  # third of the account
        fourth = await h.connect()
        await fourth.send({"type": "auth", "token": viewer.token})
        assert await fourth.closed_code() == RealtimeCloseCode.LIMIT_EXCEEDED
        assert h.app.state.hub.connections_of(account=owner.account_id) == 3


async def test_realtime_demo_counts_per_login_session_and_address() -> None:
    """The public demo (ADR 0068): every visitor is the same user, so the user limit counts per
    login session; the per-address limit keeps one visitor from taking every socket."""
    async with harness(
        demo_mode=True,
        realtime_max_connections_per_user=2,
        realtime_max_connections_per_address=3,
        realtime_max_connections_per_account=10,
    ) as h:
        owner = await h.owner()

        def visitor() -> str:
            principal = owner.principal.model_copy(update={"session_id": uuid4()})
            return h.keys.mint(principal)

        first, second = visitor(), visitor()
        await (await h.connect()).auth(first)
        await (await h.connect()).auth(first)
        third = await h.connect()
        await third.send({"type": "auth", "token": first})
        error = await third.expect("error")
        assert error["code"] == "limit_exceeded"
        assert "login session" in error["message"]
        assert await third.closed_code() == RealtimeCloseCode.LIMIT_EXCEEDED
        # Another visitor (the same user, another login session) still gets a socket ...
        await (await h.connect()).auth(second)
        # ... until the address has its three.
        fourth = await h.connect()
        await fourth.send({"type": "auth", "token": visitor()})
        error = await fourth.expect("error")
        assert "per address" in error["message"]
        assert await fourth.closed_code() == RealtimeCloseCode.LIMIT_EXCEEDED
        hub = h.app.state.hub
        assert hub.connections_of(user=owner.principal.subject) == 3
        assert hub.connections_of(address="127.0.0.1") == 3


async def test_realtime_address_limit_counts_ipv6_per_64() -> None:
    """Like every other limit, the per-address cap counts an IPv6 client by its /64: one
    connection rotating through its prefix gets no fresh slots, another /64 does."""
    async with harness(
        demo_mode=True,
        realtime_max_connections_per_user=10,
        realtime_max_connections_per_address=2,
        realtime_max_connections_per_account=10,
    ) as h:
        owner = await h.owner()

        def visitor() -> str:
            principal = owner.principal.model_copy(update={"session_id": uuid4()})
            return h.keys.mint(principal)

        await (await h.connect(forwarded_for="2001:db8:1:2::a")).auth(visitor())
        await (await h.connect(forwarded_for="2001:db8:1:2:ffff::b")).auth(visitor())
        same_64 = await h.connect(forwarded_for="2001:db8:1:2:1234::c")
        await same_64.send({"type": "auth", "token": visitor()})
        error = await same_64.expect("error")
        assert "per address" in error["message"]
        assert await same_64.closed_code() == RealtimeCloseCode.LIMIT_EXCEEDED
        await (await h.connect(forwarded_for="2001:db8:1:3::a")).auth(visitor())
        hub = h.app.state.hub
        assert hub.connections_of(address="2001:db8:1:2::99") == 2
        assert hub.connections_of(address="2001:db8:1:3::a") == 1


async def test_realtime_closed_sockets_free_their_slots() -> None:
    async with harness(
        realtime_max_connections_per_user=1, realtime_max_connections_per_address=1
    ) as h:
        owner = await h.owner()
        client = await h.connect_as(owner)
        await client.ws.close()
        hub = h.app.state.hub
        for _ in range(50):
            if hub.connections_of(address="127.0.0.1") == 0:
                break
            await asyncio.sleep(0.02)
        assert hub.connections_of(address="127.0.0.1") == 0
        assert hub.connections_of(user=owner.principal.subject) == 0
        await h.connect_as(owner)


async def test_realtime_subscription_limit_closes_4429() -> None:
    async with harness(realtime_max_subscriptions=2) as h:
        owner = await h.owner()
        mowers = [await h.add_mower(owner) for _ in range(3)]
        client = await h.connect_as(owner)
        await client.subscribe(mowers[0], mowers[1])
        await client.send({"type": "subscribe", "mower_ids": [str(mowers[2])]})
        assert (await client.expect("error"))["code"] == "limit_exceeded"
        assert await client.closed_code() == RealtimeCloseCode.LIMIT_EXCEEDED


async def test_realtime_queue_overflow_closes_4430() -> None:
    async with harness(realtime_queue_size=3) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        client = await h.connect_as(owner)
        await client.subscribe(mower_id)
        # The in-memory bus delivers inline: the writer gets no turn between these publishes.
        for _ in range(10):
            await h.publish_result(mower_id)
        assert await client.closed_code() == RealtimeCloseCode.SLOW_CONSUMER


async def test_realtime_origin_check() -> None:
    async with harness(realtime_allowed_origins="https://app.example") as h:
        owner = await h.owner()
        allowed = await h.connect(origin="https://app.example")
        await allowed.auth(owner.token)
        try:
            await h.connect(origin="https://evil.example")
        except Exception as exc:  # the handshake is refused (HTTP 403)
            assert "403" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("a foreign origin was accepted")


async def test_realtime_refused_origin_is_logged() -> None:
    """A refused socket names its origin at info level (a phone on a LAN address was refused
    without a trace)."""
    async with harness(realtime_allowed_origins="http://localhost") as h:
        with capture_logs() as logs, pytest.raises(Exception, match="403"):
            await h.connect(origin="http://192.168.1.20")
        refused = [e for e in logs if e["event"] == "websocket refused: origin not allowed"]
        assert len(refused) == 1, logs
        assert refused[0]["log_level"] == "info"
        assert refused[0]["origin"] == "http://192.168.1.20"
        assert refused[0]["allowed"] == ["http://localhost"]
        assert "REALTIME_ALLOWED_ORIGINS" in refused[0]["hint"]
        with capture_logs() as quiet:
            await h.connect(origin="http://localhost")
        assert not [e for e in quiet if e["event"].startswith("websocket refused")]


async def test_realtime_shutdown_closes_with_1001() -> None:
    async with harness() as h:
        owner = await h.owner()
        client = await h.connect_as(owner)
        h.server.should_exit = True
        assert await client.closed_code(timeout=5) == RealtimeCloseCode.GOING_AWAY
        assert h.app.state.draining is True
