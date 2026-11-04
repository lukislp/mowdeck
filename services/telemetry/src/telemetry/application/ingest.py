# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Ingestion: states, command results and the mower directory.

The health history (blade and mowing hours, distance, battery cycles, charge sessions, errors;
see :mod:`telemetry.domain.health`) advances in the same transaction as the latest row. The
blade hours threshold event is published after the commit; a failed publish is logged.

States arrive in batches (:meth:`Ingest.record_states`, ADR 0078): one transaction per batch,
the rows of its mowers read once, the write rules applied state by state in order, one
statement per table. A batch of one is exactly the former per-message transaction.

Manual drive frames (ten per second) produce command results whose correlation id names a drive
session; their ``accepted`` results are not stored, so they do not flood the timeline.

Tenant attribution: a state carries its ``account_id`` (set by the Device-Gateway from the Fleet
registration). A command result carries none, so the account comes from the mower directory,
which Fleet's events and the ``fleet.mowers.list`` seed fill. Nothing here reads an account from
a request.
"""

import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import date
from enum import Enum
from typing import Any, Protocol
from uuid import UUID

from mowdeck_contracts.v1 import (
    BladeHoursThreshold,
    CommandResult,
    CommandStatus,
    MowerCapabilitiesSnapshot,
    MowerMapSnapshot,
    MowerPathSnapshot,
    MowerRegistration,
    MowerState,
    drive_session_of,
)
from mowdeck_service_kit import get_logger

from telemetry.application.context import TelemetryConfig
from telemetry.application.ports import Clock, HealthEvents, IngestRows, StateWrites, Store, Tx
from telemetry.domain.errors import AccountMismatchError, UnknownMowerError
from telemetry.domain.health import (
    DailyRow,
    HealthLimits,
    HealthRow,
    advance,
    apply_delta,
    blade_counter_seconds,
    threshold_seconds,
)
from telemetry.domain.models import CommandEvent, DirectoryEntry, LatestRow, SnapshotRow
from telemetry.domain.rules import (
    decide,
    history_row,
    position_fingerprint,
    position_row,
    state_fingerprint,
)

_log = get_logger("telemetry.ingest")


class StateOutcome(Enum):
    """What :meth:`Ingest.record_states` did with a state (besides an account mismatch)."""

    STORED = "stored"
    OLDER = "older"
    """Older than the stored latest state: nothing written."""


class _Batch:
    """The rows one batch of states works on: read once, updated in memory state by state."""

    def __init__(self, rows: IngestRows) -> None:
        self.rows = rows
        self.latest: dict[UUID, LatestRow] = dict(rows.latest)
        self.health: dict[UUID, HealthRow] = dict(rows.health)
        self.daily: dict[tuple[UUID, date], DailyRow | None] = {}
        self.writes = StateWrites.empty()


class PositionObserver(Protocol):
    """Follows the stored states after their commit (the virtual zones, ADR 0070)."""

    async def observe_many(self, states: Sequence[MowerState]) -> None:
        """Look at the stored states of one batch, in order (failures are its own to log)."""
        ...


class Ingest:
    """Writes what arrives on the bus."""

    def __init__(
        self,
        store: Store,
        config: TelemetryConfig,
        clock: Clock,
        events: HealthEvents | None = None,
        observer: PositionObserver | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._clock = clock
        self._events = events
        self._observer = observer
        self._limits = health_limits(config)

    async def record_state(self, state: MowerState) -> bool:
        """Store a state per the write rules; False when it was older than the stored one.

        Raises :class:`AccountMismatchError` when the state names another account than the one the
        directory or the stored rows assign to the mower.
        """
        outcome = (await self.record_states([state]))[0]
        if isinstance(outcome, AccountMismatchError):
            raise outcome
        return outcome is StateOutcome.STORED

    async def record_states(
        self, states: Sequence[MowerState]
    ) -> list[StateOutcome | AccountMismatchError]:
        """Store a batch of states in one transaction, each exactly as :meth:`record_state`
        would one after the other, in the given order; one outcome per state.

        The batch reads the directory, the latest rows and the health totals of its mowers once
        (locked, in mower id order), applies the write rules state by state in memory and then
        writes everything with one statement per table: the history and position rows of every
        state, the last latest row, health totals and daily totals per mower. A state naming
        another account than the directory or the stored rows is left out (its outcome is the
        :class:`AccountMismatchError`), the others are stored. A database failure raises and
        stores nothing, so the whole batch is delivered again; a redelivered state changes
        nothing (rule 4 of :mod:`telemetry.domain.rules`, :func:`~telemetry.domain.health.advance`).
        Blade threshold events are published and the observer runs after the commit, in order.
        """
        if not states:
            return []
        outcomes: list[StateOutcome | AccountMismatchError] = []
        stored: list[MowerState] = []
        crossed: list[BladeHoursThreshold] = []
        async with self._store.transaction() as tx:
            batch = _Batch(await tx.ingest_rows({s.mower_id for s in states}))
            for state in states:
                outcome = await self._apply(tx, batch, state, crossed)
                outcomes.append(outcome)
                if outcome is StateOutcome.STORED:
                    stored.append(state)
            if stored:
                await tx.write_states(batch.writes)
        for event in crossed:
            await self._publish_threshold(event)
        if stored:
            await self._observe(stored)
        return outcomes

    async def _apply(
        self, tx: Tx, batch: "_Batch", state: MowerState, crossed: list[BladeHoursThreshold]
    ) -> "StateOutcome | AccountMismatchError":
        """The write rules for one state against the batch's rows (see :meth:`record_states`)."""
        mower = state.mower_id
        owner = batch.rows.owners.get(mower)
        if owner is not None and owner != state.account_id:
            return AccountMismatchError("state names another account than the mower directory")
        current = batch.latest.get(mower)
        if current is not None and current.account_id != state.account_id:
            return AccountMismatchError("state names another account than the stored state")
        decision = decide(state, current, heartbeat_seconds=self._config.heartbeat_history_seconds)
        if not decision.latest:
            return StateOutcome.OLDER
        health = batch.health.get(mower)
        if health is not None and health.account_id != state.account_id:
            return AccountMismatchError("state names another account than the stored health totals")
        writes = batch.writes
        history_at = current.history_written_at if current else None
        history_print = current.history_fingerprint if current else None
        position_print = current.position_fingerprint if current else None
        if decision.history:
            writes.history.append(history_row(state))
            history_at, history_print = state.observed_at, state_fingerprint(state)
        if decision.position:
            row = position_row(state)
            if row is not None:
                writes.positions.append(row)
                position_print = position_fingerprint(state)
        latest = LatestRow(
            mower_id=mower,
            account_id=state.account_id,
            observed_at=state.observed_at,
            received_at=self._clock(),
            activity=state.activity.value,
            battery_percent=state.battery_percent,
            state=state.model_dump(mode="json"),
            history_written_at=history_at,
            history_fingerprint=history_print,
            position_fingerprint=position_print,
        )
        batch.latest[mower] = writes.latest[mower] = latest
        event = await self._advance_health(tx, batch, state, health)
        if event is not None:
            crossed.append(event)
        return StateOutcome.STORED

    async def _observe(self, states: list[MowerState]) -> None:
        """Hand the stored states to the observer; its failure never fails the ingestion."""
        if self._observer is None:
            return
        try:
            await self._observer.observe_many(states)
        except Exception:
            _log.exception("evaluating the zones failed", states=len(states))

    async def _advance_health(
        self, tx: Tx, batch: "_Batch", state: MowerState, current: HealthRow | None
    ) -> BladeHoursThreshold | None:
        """Add the state to the health totals; the threshold event when it was crossed."""
        step = advance(current, state, self._limits, self._clock())
        if step is None:  # redelivery or out of order: counted already
            return None
        mower = state.mower_id
        batch.health[mower] = batch.writes.health[mower] = step.row
        for delta in step.deltas:
            key = (mower, delta.day)
            if key in batch.daily:
                day = batch.daily[key]
            else:
                day = await tx.daily(mower, delta.day)
            updated = apply_delta(day, delta, mower, state.account_id)
            batch.daily[key] = batch.writes.daily[key] = updated
        if not step.threshold_crossed:
            return None
        return BladeHoursThreshold(
            mower_id=mower,
            account_id=state.account_id,
            blade_hours_since_change=blade_counter_seconds(step.row) / 3600,
            threshold_hours=threshold_seconds(step.row, self._limits) / 3600,
            blade_changed_at=step.row.blade_changed_at,
            crossed_at=state.observed_at,
        )

    async def _publish_threshold(self, payload: BladeHoursThreshold) -> None:
        if self._events is None:
            return
        try:
            await self._events.blade_threshold(payload)
        except Exception:
            _log.exception(
                "publishing the blade hours threshold failed", mower_id=str(payload.mower_id)
            )

    async def record_result(self, result: CommandResult) -> None:
        """Append a command result to the timeline of its mower's account.

        ``accepted`` results of manual drive frames are skipped (ten per second would flood the
        timeline); their rejections, failures and expiries are stored. Raises
        :class:`UnknownMowerError` when the directory does not know the mower.
        """
        if (
            result.status == CommandStatus.ACCEPTED
            and drive_session_of(result.correlation_id) is not None
        ):
            return
        async with self._store.transaction() as tx:
            account_id = await tx.directory_account(result.mower_id)
            if account_id is None:
                msg = "command result for a mower that is not in the directory"
                raise UnknownMowerError(msg)
            await tx.add_command_event(
                CommandEvent(
                    account_id=account_id,
                    mower_id=result.mower_id,
                    command_id=result.command_id,
                    status=result.status.value,
                    reason=result.reason,
                    at=result.at,
                )
            )

    async def record_snapshot(
        self, snapshot: MowerMapSnapshot | MowerCapabilitiesSnapshot | MowerPathSnapshot
    ) -> bool:
        """Store a map, capability or mow path snapshot (a cleared path as an empty body).

        Raises :class:`AccountMismatchError` when the directory assigns the mower to another
        account than the snapshot names.
        """
        if isinstance(snapshot, MowerMapSnapshot):
            kind, body = "map", snapshot.map.model_dump(mode="json")
            # The mower's own revision covers only its zones; the dock, the geo reference and
            # names arrive later without changing it, so the ETag also carries a content hash
            # (otherwise clients revalidating with If-None-Match would keep the old map).
            content = _content_hash(body)
            revision = (
                f"{snapshot.map.revision}.{content[:12]}" if snapshot.map.revision else content
            )
        elif isinstance(snapshot, MowerPathSnapshot):
            kind = "path"
            body = snapshot.path.model_dump(mode="json") if snapshot.path is not None else {}
            revision = _content_hash(body)
        else:
            kind, body = "capabilities", snapshot.capabilities.model_dump(mode="json")
            revision = _content_hash(body)
        async with self._store.transaction() as tx:
            owner = await tx.directory_account(snapshot.mower_id)
            if owner is not None and owner != snapshot.account_id:
                msg = "snapshot names another account than the mower directory"
                raise AccountMismatchError(msg)
            await tx.put_snapshot(
                SnapshotRow(
                    mower_id=snapshot.mower_id,
                    kind=kind,
                    account_id=snapshot.account_id,
                    observed_at=snapshot.observed_at,
                    received_at=self._clock(),
                    revision=revision,
                    body=body,
                )
            )
        return True

    # --- directory ----------------------------------------------------------------------------

    async def mower_registered(self, registration: MowerRegistration) -> None:
        """Remember (or update) which account owns a mower (events win over the seed)."""
        async with self._store.transaction() as tx:
            await tx.put_directory(self._entry(registration), replace=True)

    def _entry(self, registration: MowerRegistration) -> DirectoryEntry:
        device_name = registration.connection_options.get("device_name")
        return DirectoryEntry(
            registration.mower_id,
            registration.account_id,
            self._clock(),
            name=registration.name,
            serial=device_name if isinstance(device_name, str) and device_name else None,
        )

    async def mower_removed(self, mower_id: UUID) -> None:
        """Forget a mower and delete everything stored about it."""
        async with self._store.transaction() as tx:
            await tx.drop_mower(mower_id)

    async def seed(self, registrations: Iterable[MowerRegistration]) -> int:
        """Add mowers Fleet listed; entries that events already created are kept."""
        count = 0
        async with self._store.transaction() as tx:
            for registration in registrations:
                await tx.put_directory(self._entry(registration), replace=False)
                count += 1
        return count


def health_limits(config: TelemetryConfig) -> HealthLimits:
    """The accounting parameters of the health history."""
    return HealthLimits(
        max_gap=config.health_max_gap,
        max_step_m=config.health_max_step_m,
        blade_threshold_seconds=config.blade_hours_threshold * 3600,
    )


def _content_hash(body: dict[str, Any]) -> str:
    """A short hash of a JSON body (the ETag of snapshots without a revision)."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
