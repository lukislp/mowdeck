"""Applies the rules to incoming states and connection events, and runs the offline timer.

The watch of a mower is read ``FOR UPDATE`` and written back in one transaction, so two
replicas handling messages of the same mower never lose an update. The derived events are fanned
out after the commit: a crash in between loses that one notification (at most once), while a
redelivered message never notifies twice (the watch already moved on, and inbox items are unique
per ``dedupe_key``).

Commands only update the watch of a mower that already has one (``last_command_at``,
``last_drive_at``); a mower without a watch has not reported a state yet, and its first state
notifies nothing anyway. Manual drive frames arrive several times per second, so a replica
writes at most one command time per mower every few seconds (the rules work with minutes).
Map snapshots become the mower's geofence; snapshots older than the stored one are ignored.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from mowdeck_contracts.v1 import (
    BladeHoursThreshold,
    CommandEnvelope,
    MowerMapSnapshot,
    MowerRegistration,
    MowerState,
    VisionProposalCreated,
    VisionProposalUpdated,
    drive_session_of,
)
from mowdeck_service_kit import get_logger

from notifications.application.context import NotificationsConfig
from notifications.application.fanout import Fanout
from notifications.application.ports import Clock, Store
from notifications.domain.errors import AccountMismatchError
from notifications.domain.geofence import Fence, MowerFence
from notifications.domain.models import DirectoryEntry, MaintenanceWindow, NotificationEvent
from notifications.domain.rules import (
    check_offline,
    observe_blade_hours,
    observe_command,
    observe_connection,
    observe_state,
    release_held_offline,
    vision_no_photo_event,
    vision_proposal_event,
)

_log = get_logger("notifications.watcher")

WINDOW_CACHE = timedelta(seconds=2)
"""How long a replica reuses the maintenance window it read (states come every 10 s per mower)."""

COMMAND_RESOLUTION = timedelta(seconds=5)
"""A replica skips a command less than this newer than the last one it recorded for the mower
(manual drive frames come several times per second)."""


class Watcher:
    """The rules use case."""

    def __init__(
        self, store: Store, fanout: Fanout, config: NotificationsConfig, clock: Clock
    ) -> None:
        self._store = store
        self._fanout = fanout
        self._config = config
        self._clock = clock
        self._recorded: dict[tuple[UUID, bool], datetime] = {}
        self._window: tuple[datetime, MaintenanceWindow | None] | None = None

    async def _maintenance(self, now: datetime) -> MaintenanceWindow | None:
        """The maintenance window, read at most every :data:`WINDOW_CACHE`."""
        cached = self._window
        if cached is not None and now - cached[0] < WINDOW_CACHE:
            return cached[1]
        window = await self._store.maintenance_window()
        self._window = (now, window)
        return window

    async def _check_account(self, mower_id: UUID, account_id: UUID, what: str) -> None:
        entry = await self._store.directory_entry(mower_id)
        if entry is not None and entry.account_id != account_id:
            msg = f"the {what}'s account differs from the mower directory"
            raise AccountMismatchError(msg)

    async def on_state(self, state: MowerState) -> list[NotificationEvent]:
        """Apply a state; returns the derived events (already fanned out).

        The gateway's offline state after a lost link (``connected = false``, ADR 0040) proves
        no contact and is skipped: ``mower.disconnected`` drives the offline timer.
        """
        if not state.connected:
            return []
        await self._check_account(state.mower_id, state.account_id, "state")
        stored = await self._store.geofence(state.mower_id)
        fence = (
            stored.fence if stored is not None and stored.account_id == state.account_id else None
        )
        now = self._clock()
        window = await self._maintenance(now)
        async with self._store.transaction() as tx:
            watch, events = observe_state(
                await tx.watch(state.mower_id), state, now, self._config.rules, fence, window
            )
            await tx.put_watch(watch)
        await self._fanout.dispatch(events)
        return events

    async def on_connection(
        self, mower_id: UUID, account_id: UUID, *, connected: bool, at: datetime
    ) -> list[NotificationEvent]:
        """Apply ``mower.connected`` or ``mower.disconnected``."""
        entry = await self._store.directory_entry(mower_id)
        if entry is not None and entry.account_id != account_id:
            msg = "the event's account differs from the mower directory"
            raise AccountMismatchError(msg)
        now = self._clock()
        window = await self._maintenance(now)
        async with self._store.transaction() as tx:
            watch, events = observe_connection(
                await tx.watch(mower_id),
                mower_id,
                account_id,
                connected=connected,
                at=at,
                now=now,
                config=self._config.rules,
                window=window,
            )
            await tx.put_watch(watch)
        await self._fanout.dispatch(events)
        return events

    async def on_command(self, envelope: CommandEnvelope) -> bool:
        """Remember that a command was issued for the mower; True when the watch changed."""
        await self._check_account(envelope.mower_id, envelope.account_id, "command")
        drive = (
            envelope.command.kind == "manual_drive"
            or drive_session_of(envelope.correlation_id) is not None
        )
        issued = envelope.issued_at
        key = (envelope.mower_id, drive)
        recorded = self._recorded.get(key)
        if recorded is not None and issued < recorded + COMMAND_RESOLUTION:
            return False
        async with self._store.transaction() as tx:
            watch = await tx.watch(envelope.mower_id)
            if watch is None or watch.account_id != envelope.account_id:
                return False
            updated = observe_command(watch, issued, drive=drive)
            if updated != watch:
                await tx.put_watch(updated)
        self._recorded[key] = max(issued, recorded) if recorded is not None else issued
        return updated != watch

    async def on_map(self, snapshot: MowerMapSnapshot) -> bool:
        """Store the geofence of a map snapshot; True when stored."""
        await self._check_account(snapshot.mower_id, snapshot.account_id, "map")
        fence = Fence.from_map(snapshot.map)
        if fence is None:
            _log.info(
                "map without areas or geo reference, no geofence", mower_id=str(snapshot.mower_id)
            )
            return False
        async with self._store.transaction() as tx:
            return await tx.put_geofence(
                MowerFence(
                    mower_id=snapshot.mower_id,
                    account_id=snapshot.account_id,
                    observed_at=snapshot.observed_at,
                    fence=fence,
                )
            )

    async def on_blade_threshold(self, payload: BladeHoursThreshold) -> list[NotificationEvent]:
        """Telemetry reported a blade hours crossing: remind the owner."""
        await self._check_account(payload.mower_id, payload.account_id, "event")
        now = self._clock()
        async with self._store.transaction() as tx:
            watch = await tx.watch(payload.mower_id)
            updated, events = observe_blade_hours(watch, payload, now, self._config.rules)
            if updated is not None and updated != watch:
                await tx.put_watch(updated)
        await self._fanout.dispatch(events)
        return events

    async def on_vision_update(self, payload: VisionProposalUpdated) -> list[NotificationEvent]:
        """An automatic analysis got no camera photo: tell the users who may drive the mower
        why, so the skip is never silent (other updates notify nobody)."""
        event = vision_no_photo_event(payload)
        if event is None:
            return []
        await self._check_account(payload.mower_id, payload.account_id, "event")
        await self._fanout.dispatch([event])
        return [event]

    async def on_vision_proposal(self, payload: VisionProposalCreated) -> list[NotificationEvent]:
        """AI assist proposed a maneuver: tell the account's users who may drive the mower
        (``can_drive``), linking to the proposal. One notification per proposal and user."""
        await self._check_account(payload.mower_id, payload.account_id, "event")
        minutes = max(1, round((payload.expires_at - payload.created_at).total_seconds() / 60))
        events = [vision_proposal_event(payload, minutes)]
        await self._fanout.dispatch(events)
        return events

    async def sweep_offline(self) -> list[NotificationEvent]:
        """The offline timer: mark mowers without contact offline (one batch)."""
        now = self._clock()
        rules = self._config.rules
        events: list[NotificationEvent] = []
        window = await self._maintenance(now)
        async with self._store.transaction() as tx:
            candidates = await tx.offline_candidates(
                now - timedelta(seconds=rules.offline_after_seconds),
                now - timedelta(seconds=rules.disconnect_grace_seconds),
                self._config.offline_batch_size,
            )
            for watch in candidates:
                updated, derived = check_offline(watch, now, rules, window)
                if updated != watch:
                    await tx.put_watch(updated)
                events.extend(derived)
            for held in await tx.held_offline(self._config.offline_batch_size):
                updated, derived = release_held_offline(held, now, rules, window)
                if updated != held:
                    await tx.put_watch(updated)
                events.extend(derived)
        await self._fanout.dispatch(events)
        return events

    async def mower_registered(self, registration: MowerRegistration) -> None:
        """Remember (or update) the mower's account and name."""
        async with self._store.transaction() as tx:
            await tx.put_directory(self._entry(registration), replace=True)

    async def mower_removed(self, mower_id: UUID) -> None:
        """Forget the mower."""
        async with self._store.transaction() as tx:
            await tx.drop_mower(mower_id)

    async def seed(self, registrations: Sequence[MowerRegistration]) -> int:
        """Add Fleet's list to the directory without overwriting newer event data."""
        async with self._store.transaction() as tx:
            for registration in registrations:
                await tx.put_directory(self._entry(registration), replace=False)
        return len(registrations)

    def _entry(self, registration: MowerRegistration) -> DirectoryEntry:
        return DirectoryEntry(
            mower_id=registration.mower_id,
            account_id=registration.account_id,
            name=registration.name,
            updated_at=self._clock(),
        )
