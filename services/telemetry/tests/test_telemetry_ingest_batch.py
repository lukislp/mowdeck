"""Batched ingestion (ADR 0078): a batch stores exactly what the same states one by one store,
in order, skips only the states that name another account, stores nothing when the database
fails, changes nothing when delivered again; and the bus handler acknowledges per state.

Runs against the memory store and, with ``MOWDECK_TEST_DATABASE_URL``, PostgreSQL."""

import dataclasses
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import MowerActivity, MowerState
from mowdeck_contracts.v1.subjects import telemetry_subject
from mowdeck_service_kit.bus import BusMessage
from telemetry_support import T0, make_settings, make_state, seconds
from test_telemetry_db import needs_db, store_of

from telemetry.adapters.bus.subscriptions import states_handler
from telemetry.adapters.memory import MemoryStore
from telemetry.application.ingest import Ingest, StateOutcome
from telemetry.application.ports import StateWrites, Store, Tx
from telemetry.domain.errors import AccountMismatchError

KINDS = ["memory", pytest.param("postgres", marks=needs_db)]


def _ingest(store: Store) -> Ingest:
    return Ingest(store, make_settings().to_config(), lambda: T0 + timedelta(hours=1))


def _run(mower: UUID, account: UUID, start: datetime) -> list[MowerState]:
    """A docked mower that mows a line, repeats a report (link heartbeat) and docks again."""
    states = [make_state(mower, account, at=start, position=(48.0, 11.0))]
    for n in range(1, 13):
        states.append(
            make_state(
                mower,
                account,
                at=start + seconds(5 * n),
                activity=MowerActivity.MOWING,
                battery=90 - n,
                position=(48.0 + n * 1e-4, 11.0),
                speed_mps=0.4,
            )
        )
    states.append(states[-1].model_copy(update={"heartbeat_at": start + seconds(62)}))
    states.append(
        make_state(
            mower,
            account,
            at=start + seconds(70),
            activity=MowerActivity.CHARGING,
            battery=78,
            position=(48.0, 11.0),
            charging=True,
        )
    )
    return states


def _interleave(*runs: list[MowerState]) -> list[MowerState]:
    out: list[MowerState] = []
    for row in zip(*runs, strict=True):
        out.extend(row)
    return out


async def _snapshot(store: Store, account: UUID, mower: UUID) -> dict[str, Any]:
    """Everything the ingestion wrote for a mower, through the read side of the store."""
    start, end = T0 - timedelta(days=1), T0 + timedelta(days=1)
    latest = await store.latest_of(account, mower)
    health = await store.health_of(account, mower)
    return {
        "latest": dataclasses.asdict(latest) if latest else None,
        "history": await store.history(account, mower, start, end, None, 10_000),
        "positions": await store.positions(account, mower, start, end, 10_000),
        "health": dataclasses.asdict(health) if health else None,
        "daily": await store.daily_of(account, mower, start.date(), end.date()),
    }


@asynccontextmanager
async def _two_stores(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Store, Store]]:
    async with store_of(kind, monkeypatch) as one, store_of(kind, monkeypatch) as other:
        yield one, other


@pytest.mark.parametrize("kind", KINDS)
async def test_telemetry_batch_stores_what_one_by_one_stores(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, a, b = uuid4(), uuid4(), uuid4()
    states = _interleave(_run(a, account, T0), _run(b, account, T0 + seconds(1)))
    async with _two_stores(kind, monkeypatch) as (single, batched):
        one = _ingest(single)
        expected = [await one.record_state(s) for s in states]
        outcomes = []
        for start in range(0, len(states), 7):  # batches that split the runs anywhere
            outcomes.extend(await _ingest(batched).record_states(states[start : start + 7]))
        assert [o is StateOutcome.STORED for o in outcomes] == expected
        for mower in (a, b):
            assert await _snapshot(batched, account, mower) == await _snapshot(
                single, account, mower
            )
        assert len((await _snapshot(batched, account, a))["positions"]) == 14


@pytest.mark.parametrize("kind", KINDS)
async def test_telemetry_batch_keeps_the_order_and_ignores_older_states(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, mower = uuid4(), uuid4()
    newer = make_state(mower, account, at=T0 + seconds(10), battery=50)
    older = make_state(mower, account, at=T0, battery=80)
    async with store_of(kind, monkeypatch) as store:
        outcomes = await _ingest(store).record_states([newer, older])
        assert outcomes == [StateOutcome.STORED, StateOutcome.OLDER]
        latest = await store.latest_of(account, mower)
        assert latest is not None
        assert latest.battery_percent == 50


@pytest.mark.parametrize("kind", KINDS)
async def test_telemetry_batch_skips_only_the_state_of_another_account(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, intruder, mower, other_mower = uuid4(), uuid4(), uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        await _ingest(store).record_state(make_state(mower, account, at=T0))
        batch = [
            make_state(mower, intruder, at=T0 + seconds(5), battery=10),
            make_state(mower, account, at=T0 + seconds(6), battery=70),
            make_state(other_mower, account, at=T0 + seconds(6)),
        ]
        outcomes = await _ingest(store).record_states(batch)
        assert isinstance(outcomes[0], AccountMismatchError)
        assert outcomes[1:] == [StateOutcome.STORED, StateOutcome.STORED]
        latest = await store.latest_of(account, mower)
        assert latest is not None
        assert latest.battery_percent == 70
        assert await store.latest_of(intruder, mower) is None
        with pytest.raises(AccountMismatchError):
            await _ingest(store).record_state(make_state(mower, intruder, at=T0 + seconds(9)))


@pytest.mark.parametrize("kind", KINDS)
async def test_telemetry_batch_delivered_again_changes_nothing(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At least once delivery: the same batch again (a redelivery after a lost
    acknowledgement) neither adds rows nor counts the health twice."""
    account, mower = uuid4(), uuid4()
    states = _run(mower, account, T0)
    async with store_of(kind, monkeypatch) as store:
        await _ingest(store).record_states(states)
        before = await _snapshot(store, account, mower)
        outcomes = await _ingest(store).record_states(states)
        assert StateOutcome.STORED in outcomes  # the equal last state replaces the latest row
        assert await _snapshot(store, account, mower) == before


class _FailingStore(MemoryStore):
    """A memory store whose batch write fails once (a database that went away)."""

    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Tx]:
        async with super().transaction() as tx:
            if self.fail:
                original = tx.write_states

                async def broken(writes: StateWrites) -> None:
                    await original(writes)
                    raise ConnectionError("the database went away")

                tx.write_states = broken  # type: ignore[method-assign]
            yield tx


async def test_telemetry_batch_failure_stores_nothing_and_the_retry_everything() -> None:
    account, mower = uuid4(), uuid4()
    states = _run(mower, account, T0)
    store = _FailingStore()
    ingest = _ingest(store)
    with pytest.raises(ConnectionError):
        await ingest.record_states(states)
    assert await store.latest_of(account, mower) is None  # rolled back as a whole
    store.fail = False
    await ingest.record_states(states)
    reference = MemoryStore()
    await _ingest(reference).record_states(states)
    assert await _snapshot(store, account, mower) == await _snapshot(reference, account, mower)


def _message(state: MowerState, subject: str | None = None) -> BusMessage:
    return BusMessage(
        subject=subject or telemetry_subject(state.mower_id),
        data=state.model_dump_json().encode(),
    )


async def test_telemetry_states_handler_settles_each_message() -> None:
    account, intruder, mower = uuid4(), uuid4(), uuid4()
    store = MemoryStore()
    ingest = _ingest(store)
    await ingest.record_state(make_state(mower, account, at=T0))
    handler = states_handler(ingest)
    good = make_state(mower, account, at=T0 + seconds(5))
    messages = [
        BusMessage(subject=telemetry_subject(mower), data=b"{not json"),
        _message(make_state(mower, intruder, at=T0 + seconds(4))),
        _message(good, subject=telemetry_subject(uuid4())),  # subject names another mower
        _message(good),
        _message(make_state(mower, account, at=T0 + seconds(1))),  # older: acknowledged
    ]
    assert await handler(messages) == ["term", "term", "term", "ack", "ack"]
    latest = await store.latest_of(account, mower)
    assert latest is not None
    assert latest.observed_at == good.observed_at


async def test_telemetry_states_handler_raises_on_a_database_failure() -> None:
    """The kit naks the whole batch: every state comes again."""
    account, mower = uuid4(), uuid4()
    handler = states_handler(_ingest(_FailingStore()))
    with pytest.raises(ConnectionError):
        await handler([_message(make_state(mower, account, at=T0))])
