"""Demo sandboxes (ADR 0076): presence announcements and the isolation of two visitors.

A demo Realtime announces on ``presence.accounts`` when the first socket of an account opens,
when the last one closes, and as a keep-alive; outside demo mode it announces nothing. Two
visitors are two accounts: neither can subscribe to, or receive anything of, the other's
mowers over the socket.
"""

import asyncio
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import ACCOUNT_PRESENCE, AccountPresence
from realtime_support import harness, make_state

from realtime.application.hub import Hub


def _announcements(bus: Any) -> list[AccountPresence]:
    return [m.model(AccountPresence) for m in bus.messages(ACCOUNT_PRESENCE)]


def _counts(bus: Any, account_id: UUID) -> list[int]:
    return [
        entry.watchers
        for presence in _announcements(bus)
        if not presence.full
        for entry in presence.accounts
        if entry.account_id == account_id
    ]


async def _until(predicate: Any, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def test_the_first_and_the_last_socket_of_an_account_are_announced() -> None:
    async with harness(demo_mode=True, background_tasks=False) as h:
        anna = await h.owner()
        first = await h.connect_as(anna)
        second = await h.connect_as(anna)
        await _until(lambda: _counts(h.bus, anna.account_id) == [1])
        await second.ws.close()
        await first.nothing(0.2)
        assert _counts(h.bus, anna.account_id) == [1]  # still one socket open
        await first.ws.close()
        await _until(lambda: _counts(h.bus, anna.account_id) == [1, 0])
        hub: Hub = h.app.state.hub
        assert all(p.instance == hub.instance for p in _announcements(h.bus))


async def test_the_keep_alive_lists_every_watched_account() -> None:
    async with harness(demo_mode=True, realtime_presence_seconds=0.1) as h:
        anna, ben = await h.owner(), await h.owner()
        await h.connect_as(anna)
        await h.connect_as(ben)

        def keep_alive() -> AccountPresence | None:
            full = [p for p in _announcements(h.bus) if p.full and len(p.accounts) == 2]
            return full[-1] if full else None

        await _until(lambda: keep_alive() is not None)
        presence = keep_alive()
        assert presence is not None
        assert {(e.account_id, e.watchers) for e in presence.accounts} == {
            (anna.account_id, 1),
            (ben.account_id, 1),
        }
        assert presence.interval_seconds == 0.1


async def test_outside_demo_mode_nothing_is_announced() -> None:
    async with harness(realtime_presence_seconds=0.1) as h:
        anna = await h.owner()
        client = await h.connect_as(anna)
        await client.nothing(0.3)
        await client.ws.close()
        assert _announcements(h.bus) == []


async def test_two_sandbox_visitors_never_see_each_other_over_the_socket() -> None:
    async with harness(demo_mode=True, background_tasks=False) as h:
        anna, ben = await h.owner(), await h.owner()
        annas_mower, bens_mower = await h.add_mower(anna), await h.add_mower(ben)
        annas = await h.connect_as(anna)
        bens = await h.connect_as(ben)
        # Ben may not subscribe to Anna's mower, and gets nothing of it.
        assert (await bens.subscribe(annas_mower))["denied"] == [str(annas_mower)]
        assert (await annas.subscribe(annas_mower))["denied"] == []
        assert (await bens.subscribe(bens_mower))["denied"] == []
        await h.publish_state(make_state(annas_mower, anna.account_id, battery=42))
        state = await annas.expect("state")
        assert state["mower_id"] == str(annas_mower)
        await bens.nothing(0.3)
        # Anna cannot drive or command Ben's mower over the socket either.
        await annas.send({"type": "subscribe", "mower_ids": [str(bens_mower)], "request_id": "x"})
        answer = await annas.expect("subscribed", request_id="x")
        assert answer["denied"] == [str(bens_mower)]
        await h.publish_state(make_state(bens_mower, ben.account_id, battery=7))
        assert (await bens.expect("state"))["mower_id"] == str(bens_mower)
        await annas.nothing(0.3)
