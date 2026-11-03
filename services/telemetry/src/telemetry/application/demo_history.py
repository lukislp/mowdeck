"""Past mowing days for demo mowers (ADR 0068), simulated once a day per garden (ADR 0079).

The demo seed job and every demo sandbox pool fill (ADR 0076) ask for a week of history for
three simulated mowers. Simulating a week and running thousands of states through the
ingestion costs seconds of CPU per sandbox, and every sandbox of a day gets the same days: the
jobs start at the same hour each past day, in the same gardens, and nothing of them depends on
the mower but its ids. So the history of one garden is made once per UTC day, for a template
mower of a template account nobody can sign in to, through the normal ingestion; every mower
that asks for that garden on that day gets a copy of the template's rows with its own ids
(:meth:`~telemetry.application.ports.Tx.copy_ingested`, one ``INSERT ... SELECT`` per table).

The template rows are ordinary rows of an account that has no users, no mowers in Fleet and no
directory entry: no query of a visitor reaches them, the retention and the thinning treat them
like any other mower's. When a day's template is made, the templates of the days before are
dropped. Which templates are complete is remembered in memory only: after a restart a
template is made again (its leftovers are dropped first), so an interrupted one is never
copied. The blade hours threshold event of the template's history is not repeated per copy.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from mowdeck_contracts.v1 import MowerState
from mowdeck_control.history import simulated_history

from telemetry.application.ingest import Ingest, StateOutcome
from telemetry.application.ports import Clock, Store
from telemetry.domain.errors import AccountMismatchError

TEMPLATE_ACCOUNT = uuid5(NAMESPACE_URL, "urn:mowdeck:telemetry:demo-history-template")
"""The account of the template mowers (no user, no Fleet account, never signed in to)."""

HISTORY_BATCH = 200
"""States of a simulated history stored per transaction (ADR 0078)."""

DROPPED_DAYS = 7
"""When a day's template is made, those of this many days before are dropped."""

Simulate = Callable[..., Awaitable[list[MowerState]]]
""":func:`mowdeck_control.history.simulated_history` or a stand-in (tests count the calls)."""


@dataclass(frozen=True, slots=True)
class HistoryMower:
    """One mower that should get past mowing days."""

    mower_id: UUID
    account_id: UUID
    options: dict[str, Any]


@dataclass(frozen=True, slots=True)
class HistoryResult:
    """States offered to the ingestion and stored (a copy counts its template's states)."""

    states: int
    stored: int


def template_id(options: dict[str, Any], days: int, day: date) -> UUID:
    """The template mower of one garden configuration, number of days and UTC day."""
    key = json.dumps(options, sort_keys=True, separators=(",", ":"), default=str)
    return uuid5(TEMPLATE_ACCOUNT, f"{key}|{days}|{day.isoformat()}")


class DemoHistory:
    """Simulated history for demo mowers, from a daily template per garden."""

    def __init__(
        self, store: Store, ingest: Ingest, clock: Clock, simulate: Simulate = simulated_history
    ) -> None:
        self._store = store
        self._ingest = ingest
        self._clock = clock
        self._simulate = simulate
        self._ready: dict[UUID, HistoryResult] = {}
        """Templates made by this process (complete), with their counts."""
        self._locks: dict[UUID, asyncio.Lock] = {}

    async def fill(self, mowers: Sequence[HistoryMower], days: int) -> HistoryResult:
        """``days`` past mowing days for every mower, oldest first, ending now.

        Raises :class:`AccountMismatchError` when a state names another account than the
        store assigns to the mower (a copy to a mower of another account is refused the same
        way: the target must be new or the account's own)."""
        now = self._clock()
        states = stored = 0
        for mower in mowers:
            template = await self._template(mower.options, days, now)
            copied = await self._copy(template, mower, now)
            done = self._ready[template]
            states += done.states
            stored += done.stored if copied else 0
        return HistoryResult(states, stored)

    async def _template(self, options: dict[str, Any], days: int, now: datetime) -> UUID:
        template = template_id(options, days, now.date())
        lock = self._locks.setdefault(template, asyncio.Lock())
        async with lock:
            if template not in self._ready:
                self._ready[template] = await self._make(template, options, days, now)
                await self._drop_older(options, days, now.date())
        return template

    async def _make(
        self, template: UUID, options: dict[str, Any], days: int, now: datetime
    ) -> HistoryResult:
        async with self._store.transaction() as tx:
            await tx.drop_mower(template)  # leftovers of an interrupted attempt
        simulated = await self._simulate(
            mower_id=template, account_id=TEMPLATE_ACCOUNT, options=options, end=now, days=days
        )
        offered = stored = 0
        for start in range(0, len(simulated), HISTORY_BATCH):
            outcomes = await self._ingest.record_states(simulated[start : start + HISTORY_BATCH])
            for outcome in outcomes:
                if isinstance(outcome, AccountMismatchError):  # pragma: no cover - own account
                    raise outcome
                offered += 1
                stored += outcome is StateOutcome.STORED
        return HistoryResult(offered, stored)

    async def _drop_older(self, options: dict[str, Any], days: int, today: date) -> None:
        old = [
            template_id(options, days, today - timedelta(days=n))
            for n in range(1, DROPPED_DAYS + 1)
        ]
        async with self._store.transaction() as tx:
            for template in old:
                await tx.drop_mower(template)
        for template in old:
            self._ready.pop(template, None)
            self._locks.pop(template, None)

    async def _copy(self, template: UUID, mower: HistoryMower, now: datetime) -> bool:
        """Copy the template to the mower; False when the mower has a state already (the
        ingestion refuses states older than the stored one, so it would store nothing)."""
        async with self._store.transaction() as tx:
            rows = await tx.ingest_rows([mower.mower_id])
            owner = rows.owners.get(mower.mower_id)
            current = rows.latest.get(mower.mower_id)
            if (owner is not None and owner != mower.account_id) or (
                current is not None and current.account_id != mower.account_id
            ):
                msg = "the history names another account than the mower directory"
                raise AccountMismatchError(msg)
            if current is not None:
                return False
            await tx.copy_ingested(template, mower.mower_id, mower.account_id, now)
        return True
