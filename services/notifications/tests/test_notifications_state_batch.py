# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""States in batches (ADR 0078): the rules see each state of a batch exactly as they would one
at a time, a state naming another account is skipped alone, and the bus handler settles each
message; the parsed geofence is reused while the stored one is unchanged."""

from datetime import datetime
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import MowerActivity, MowerErrorCategory, MowerState
from mowdeck_contracts.v1.subjects import telemetry_subject
from mowdeck_service_kit.bus import BusMessage
from notifications_support import harness, make_state, seconds
from test_notifications_alarm_flow import snapshot

from notifications.adapters.bus.subscriptions import states_handler
from notifications.domain.errors import AccountMismatchError


def _timeline(mower: UUID, account: UUID, at: datetime) -> list[MowerState]:
    """Docked, mowing, battery low, lifted, docked again."""
    mowing = MowerActivity.MOWING
    return [
        make_state(mower, account, at=at),
        make_state(mower, account, at=at + seconds(10), activity=mowing, battery=60),
        make_state(mower, account, at=at + seconds(20), activity=mowing, battery=12),
        make_state(
            mower,
            account,
            at=at + seconds(30),
            activity=MowerActivity.ERROR,
            battery=11,
            error_code="lifted",
            error_category=MowerErrorCategory.LIFT_TILT,
        ),
        make_state(mower, account, at=at + seconds(40), battery=11),
    ]


async def test_notifications_batch_applies_the_rules_like_one_at_a_time() -> None:
    async with harness() as h:
        owner = await h.owner()
        one = await h.add_mower(owner, name="One by one")
        batched = await h.add_mower(owner, name="Batched")
        await h.subscribe_all(owner)
        watcher = h.services.watcher
        single_events = []
        for state in _timeline(one, owner.account_id, h.clock.now):
            single_events.extend(await watcher.on_state(state))
        results, batch_events = await watcher._apply_states(
            _timeline(batched, owner.account_id, h.clock.now)
        )
        assert results == [None] * 5
        assert [e.kind for e in batch_events] == [e.kind for e in single_events]
        assert batch_events  # the timeline notifies (battery low, lifted)
        watches = h.store.data.watches
        assert watches[batched].to_json() == watches[one].to_json()


async def test_notifications_batch_skips_only_the_state_of_another_account() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower = await h.add_mower(owner)
        intruder = uuid4()
        states = [
            make_state(mower, intruder, at=h.clock.now),
            make_state(mower, owner.account_id, at=h.clock.now + seconds(1)),
            make_state(mower, owner.account_id, at=h.clock.now + seconds(2), connected=False),
        ]
        results = await h.services.watcher.on_states(states)
        assert isinstance(results[0], AccountMismatchError)
        assert results[1:] == [None, None]
        watch = h.store.data.watches[mower]
        assert watch.account_id == owner.account_id
        assert watch.last_observed_at == states[1].observed_at  # the offline state proves nothing


def _message(state: MowerState) -> BusMessage:
    return BusMessage(
        subject=telemetry_subject(state.mower_id), data=state.model_dump_json().encode()
    )


async def test_notifications_states_handler_settles_each_message() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower = await h.add_mower(owner)
        handler = states_handler(h.services.watcher)
        good = make_state(mower, owner.account_id, at=h.clock.now)
        outcomes = await handler(
            [
                BusMessage(subject=telemetry_subject(mower), data=b"[]"),
                _message(make_state(mower, uuid4(), at=h.clock.now)),
                _message(good),
            ]
        )
        assert outcomes == ["term", "term", "ack"]
        assert mower in h.store.data.watches


async def test_notifications_geofence_is_parsed_once_per_version() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower = await h.add_mower(owner)
        watcher = h.services.watcher
        first = snapshot(mower, owner.account_id)
        assert await watcher.on_map(first)
        await watcher.on_state(make_state(mower, owner.account_id, at=h.clock.now))
        assert watcher._fences[mower][0] == first.observed_at
        cached = watcher._fences[mower][1]
        await watcher.on_state(make_state(mower, owner.account_id, at=h.clock.now + seconds(1)))
        assert watcher._fences[mower][1] is cached  # not parsed again
        newer = snapshot(mower, owner.account_id, seconds_after=90)
        assert await watcher.on_map(newer)
        await watcher.on_state(make_state(mower, owner.account_id, at=h.clock.now + seconds(2)))
        assert watcher._fences[mower][0] == newer.observed_at
