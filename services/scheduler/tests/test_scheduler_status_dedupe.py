# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The mower status write rule (docs/ops/load-testing.md, "Ingestion cost"): an activity change
is written at once, an unchanged activity at most every STATUS_REFRESH or after a move of
STATUS_MOVE_M, never an older or repeated report."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import ConnectionKind, GeoPosition, MowerActivity, MowerState

from scheduler.adapters.memory import MemoryStore
from scheduler.application.ingest import STATUS_MOVE_M, STATUS_REFRESH, Ingest, status_due
from scheduler.domain.models import MowerStatus

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
MOWER, ACCOUNT = uuid4(), uuid4()
# About 1.1 m per 1e-5 degree of latitude.
METRES_PER_DEGREE = 111_195.0


def status(
    seconds: float = 0,
    activity: str = "mowing",
    lat: float | None = 48.0,
    account: UUID = ACCOUNT,
) -> MowerStatus:
    return MowerStatus(
        mower_id=MOWER,
        account_id=account,
        activity=activity,
        observed_at=T0 + timedelta(seconds=seconds),
        latitude=lat,
        longitude=11.0 if lat is not None else None,
    )


def test_scheduler_status_due() -> None:
    written = status()
    assert status_due(None, status())
    assert not status_due(written, status(5))  # same activity, same place, fresh
    assert status_due(written, status(5, activity="returning"))  # activity: at once
    assert status_due(written, status(STATUS_REFRESH.total_seconds()))  # refresh
    near = 48.0 + (STATUS_MOVE_M / 2) / METRES_PER_DEGREE
    far = 48.0 + (STATUS_MOVE_M * 1.5) / METRES_PER_DEGREE
    assert not status_due(written, status(5, lat=near))
    assert status_due(written, status(5, lat=far))
    assert status_due(written, status(5, lat=None))  # the position came or went
    assert status_due(status(lat=None), status(5))
    assert not status_due(written, status(-1, activity="docked"))  # older: never
    assert not status_due(written, status(0, activity="docked"))  # the same report again
    assert status_due(written, status(5, account=uuid4()))  # another account: write


def _state(seconds: float, activity: MowerActivity, lat: float = 48.0) -> MowerState:
    return MowerState(
        mower_id=MOWER,
        account_id=ACCOUNT,
        observed_at=T0 + timedelta(seconds=seconds),
        activity=activity,
        battery_percent=80,
        position=GeoPosition(lat=lat, lon=11.0),
        connection=ConnectionKind.SIMULATED,
    )


async def test_scheduler_ingest_writes_changes_and_refreshes() -> None:
    store = MemoryStore()
    ingest = Ingest(store)
    mowing, docked = MowerActivity.MOWING, MowerActivity.RETURNING
    assert await ingest.on_state(_state(0, mowing))
    assert not await ingest.on_state(_state(1, mowing))  # skipped: nothing relevant changed
    stored = store.data.status[MOWER]
    assert stored.observed_at == T0
    assert await ingest.on_state(_state(2, docked))  # the activity is written at once
    assert store.data.status[MOWER].activity == "returning"
    refresh = STATUS_REFRESH.total_seconds()
    assert not await ingest.on_state(_state(2 + refresh - 1, docked))
    assert await ingest.on_state(_state(2 + refresh, docked))
    assert store.data.status[MOWER].observed_at == T0 + timedelta(seconds=2 + refresh)
