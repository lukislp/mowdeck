# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""A restarted Vision does not get an erased account's map back from the streams (ADR 0082).

Vision keeps the newest state, map and route of every mower in memory and fills that view on
start from the last-value streams (``last_per_subject``). The account erasure empties the view
of the replica that answers; without the purge of the streams (Fleet, the owner of the mower
subjects) the next start would read the erased account's last map and position again, for up to
30 days. The test restarts the consumer against the same streams.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import (
    ConnectionKind,
    GeoPosition,
    GeoReference,
    MowerActivity,
    MowerMap,
    MowerMapSnapshot,
    MowerState,
)
from mowdeck_contracts.v1.subjects import device_subject, device_wildcard, telemetry_subject
from mowdeck_service_kit.bus import InMemoryBus

from vision.adapters.bus.subscriptions import subscribe_all
from vision.adapters.bus.view import BusMowerView
from vision.adapters.memory import MemoryStore

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


async def running_mower(bus: InMemoryBus, account_id: UUID) -> UUID:
    """What the Device-Gateway leaves in the streams of one mower."""
    mower_id = uuid4()
    reference = GeoReference(origin_lat=52.52, origin_lon=13.4)
    await bus.publish(
        device_subject(mower_id, "map"),
        MowerMapSnapshot(
            mower_id=mower_id,
            account_id=account_id,
            observed_at=NOW,
            map=MowerMap(reference=reference),
        ),
    )
    await bus.publish(
        telemetry_subject(mower_id),
        MowerState(
            mower_id=mower_id,
            account_id=account_id,
            observed_at=NOW,
            activity=MowerActivity.MOWING,
            battery_percent=80,
            connection=ConnectionKind.SIMULATED,
            position=GeoPosition(lat=52.52, lon=13.4),
        ),
    )
    return mower_id


async def started(bus: InMemoryBus) -> BusMowerView:
    """A Vision process that starts now: an empty view, filled by its subscriptions."""
    view = BusMowerView()
    await subscribe_all(bus, view, MemoryStore())
    return view


async def test_a_restart_replays_what_the_streams_still_hold() -> None:
    """The gap: emptying the view is not enough while the streams keep the messages."""
    bus = InMemoryBus()
    account_id = uuid4()
    mower_id = await running_mower(bus, account_id)
    first = await started(bus)
    first.forget_account(account_id)  # what the erasure does in the replica that answers
    assert first.mower_map(mower_id) is None

    restarted = await started(bus)

    assert restarted.mower_map(mower_id) is not None
    assert restarted.state(mower_id) is not None


async def test_after_the_stream_purge_a_restart_loads_nothing_of_the_erased_account() -> None:
    bus = InMemoryBus()
    erased, other = uuid4(), uuid4()
    gone, stays = await running_mower(bus, erased), await running_mower(bus, other)
    first = await started(bus)
    assert first.mower_map(gone) is not None and first.state(gone) is not None

    # The account erasure: Fleet purges the subjects of the account's mowers, Vision empties
    # its view.
    await bus.purge_subject(device_wildcard(gone))
    await bus.purge_subject(telemetry_subject(gone))
    first.forget_account(erased)

    restarted = await started(bus)

    assert restarted.mower_map(gone) is None
    assert restarted.state(gone) is None
    assert restarted.capabilities(gone) is None
    assert restarted.recent_track(gone) == []
    assert restarted.mower_map(stays) is not None and restarted.state(stays) is not None
