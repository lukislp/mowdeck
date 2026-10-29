"""The shared account session: start, websocket events and the rate-limit-friendly refresh."""

import asyncio
from collections.abc import Mapping

import pytest
from aioautomower.model import MowerAttributes, MowerStates
from husqvarna_support import APP_KEY, HUGO, SECRET, ManualClock, ScriptedTransport
from mowdeck_control import CloudLoginRefused, CloudUnavailable
from mowdeck_control_husqvarna.account import (
    DEFAULT_REFRESH_S,
    KEEPALIVE_S,
    MIN_REFRESH_S,
    HusqvarnaAccount,
    open_account,
)
from mowdeck_control_husqvarna.fake_cloud import (
    FAKE_APP_KEY,
    FAKE_APP_SECRET,
    FAKE_MOWER_ONLINE,
    FakeHusqvarnaCloud,
)


def _gets(cloud: FakeHusqvarnaCloud) -> int:
    return cloud.requests.count(("get", "mowers"))


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_start_reads_the_mowers_once_and_skips_the_invalid_id() -> None:
    transport = ScriptedTransport()
    clock = ManualClock()
    account = HusqvarnaAccount(transport, APP_KEY, SECRET, clock=clock, sleep=clock.sleep)
    await open_account(account)
    try:
        assert sorted(account.mowers) == sorted(
            [HUGO, "8f2a6c3e-1d4b-4e5f-a6b7-c8d9e0f1a2b3", "2b7d9e1f-3c5a-4d6e-8f90-a1b2c3d4e5f6"]
        )
        assert [(m, p) for m, p, _, _ in transport.calls] == [("get", "mowers")]
        assert transport.tokens_issued == 1
        assert not account.websocket_open  # the scripted transport refuses websockets
        assert account.heartbeat_at is not None
    finally:
        await account.stop()
    assert transport.closed and not account.alive


async def test_refused_credentials_stop_the_half_started_account() -> None:
    transport = ScriptedTransport(token_status=400)
    with pytest.raises(CloudLoginRefused):
        await open_account(HusqvarnaAccount(transport, APP_KEY, SECRET))
    assert transport.closed


async def test_an_application_without_the_automower_api_is_refused() -> None:
    transport = ScriptedTransport(status={("get", "mowers"): 403})
    with pytest.raises(CloudLoginRefused, match="Automower Connect API"):
        await open_account(HusqvarnaAccount(transport, APP_KEY, SECRET))


async def test_a_failing_cloud_is_unavailable() -> None:
    transport = ScriptedTransport(status={("get", "mowers"): 502})
    with pytest.raises(CloudUnavailable):
        await open_account(HusqvarnaAccount(transport, APP_KEY, SECRET))


async def test_the_refresh_never_polls_faster_than_aioautomower() -> None:
    account = HusqvarnaAccount(ScriptedTransport(), APP_KEY, SECRET, refresh_s=10)
    assert account.refresh_s == MIN_REFRESH_S == 300
    assert HusqvarnaAccount(ScriptedTransport(), APP_KEY, SECRET).refresh_s == DEFAULT_REFRESH_S


async def test_websocket_events_reach_the_listeners_without_rest() -> None:
    cloud = FakeHusqvarnaCloud()
    account = await cloud.opener()(FAKE_APP_KEY, FAKE_APP_SECRET)
    seen: list[Mapping[str, MowerAttributes]] = []
    remove = account.listen(seen.append)
    try:
        await _settle()
        assert account.websocket_open
        before = account.changed_at(FAKE_MOWER_ONLINE)
        cloud.update(FAKE_APP_KEY, FAKE_MOWER_ONLINE, state="PAUSED")
        for _ in range(50):
            if seen and seen[-1][FAKE_MOWER_ONLINE].mower.state is MowerStates.PAUSED:
                break
            await asyncio.sleep(0.01)
        assert seen[-1][FAKE_MOWER_ONLINE].mower.state is MowerStates.PAUSED
        changed = account.changed_at(FAKE_MOWER_ONLINE)
        assert before is not None and changed is not None and changed >= before
        assert _gets(cloud) == 1  # the event came over the websocket
    finally:
        remove()
        await account.stop()


async def test_maintenance_keeps_alive_and_refreshes_only_when_due() -> None:
    cloud = FakeHusqvarnaCloud()
    clock = ManualClock()
    account = await cloud.opener(clock=clock, sleep=clock.sleep)(FAKE_APP_KEY, FAKE_APP_SECRET)
    try:
        await _settle()
        assert account.websocket_open
        socket = cloud.application(FAKE_APP_KEY).sockets[0]
        ticks = int(DEFAULT_REFRESH_S // KEEPALIVE_S)
        for _ in range(ticks - 1):
            clock.now += KEEPALIVE_S
            await account.maintain_once()
        assert _gets(cloud) == 1
        assert socket.sent == [""] * (ticks - 1)  # keepalive frames, no REST
        clock.now += KEEPALIVE_S
        await account.maintain_once()
        assert _gets(cloud) == 2
        assert account.refreshes == 2
    finally:
        await account.stop()


async def test_a_refused_websocket_falls_back_to_rest_and_is_retried_with_backoff() -> None:
    cloud = FakeHusqvarnaCloud()
    cloud.refuse_websocket = True
    clock = ManualClock()
    account = await cloud.opener(clock=clock, sleep=clock.sleep)(FAKE_APP_KEY, FAKE_APP_SECRET)
    try:
        assert not account.websocket_open
        assert account.mowers  # REST answered
        clock.now += KEEPALIVE_S
        await account.maintain_once()  # retry 1 fails, back-off doubles
        assert not account.websocket_open
        cloud.refuse_websocket = False
        clock.now += KEEPALIVE_S
        await account.maintain_once()  # still waiting for the doubled back-off
        assert not account.websocket_open
        clock.now += KEEPALIVE_S
        await account.maintain_once()
        await _settle()
        assert account.websocket_open
        # the "ready" after a reopen asks for one refresh (events may have been missed)
        gets = _gets(cloud)
        clock.now += KEEPALIVE_S
        await account.maintain_once()
        assert _gets(cloud) == gets + 1
        clock.now += KEEPALIVE_S
        await account.maintain_once()
        assert _gets(cloud) == gets + 1
    finally:
        await account.stop()


async def test_a_failed_refresh_keeps_the_last_data() -> None:
    transport = ScriptedTransport()
    clock = ManualClock()
    account = HusqvarnaAccount(transport, APP_KEY, SECRET, clock=clock, sleep=clock.sleep)
    await open_account(account)
    try:
        transport.status[("get", "mowers")] = 503
        clock.now += DEFAULT_REFRESH_S
        await account.maintain_once()
        assert HUGO in account.mowers
    finally:
        await account.stop()


async def test_a_stopped_account_sends_nothing() -> None:
    account = await FakeHusqvarnaCloud().opener()(FAKE_APP_KEY, FAKE_APP_SECRET)
    await account.stop()
    await account.stop()  # idempotent
    await account.maintain_once()
    with pytest.raises(CloudUnavailable):
        from mowdeck_control_husqvarna.account import ActionKind, MowerAction

        await account.act(FAKE_MOWER_ONLINE, MowerAction(ActionKind.PAUSE))
