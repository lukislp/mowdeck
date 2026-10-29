"""Live Activities for mowing jobs and reload pushes for the iOS widgets (ADR 0059).

The iOS app registers per installation: its ActivityKit push-to-start token and whether it has
widgets. While a mower is in a job the service

* **starts** the Live Activity on every registered installation of a user who may view the mower
  (push-to-start, so a schedule or Home Assistant starting a job reaches the lock screen without
  the app running),
* **updates** the activities the app reported (their update tokens), at most every
  :data:`~notifications.domain.live_activity.UPDATE_MIN_INTERVAL_SECONDS` and only when what the
  user sees changed, and
* **ends** them when the job is over (finished, docked, error, offline); an error carries its
  title in plain words (``errorTitle``, the vendor's error lexicon in each user's language,
  ADR 0067).

Independently, a change of the mower's activity (mowing, docked, error, ...) sends one silent
background push per widget installation (at most every
:data:`~notifications.domain.live_activity.RELOAD_MIN_INTERVAL_SECONDS` and mower) so the widgets
reload their timelines; iOS decides whether to wake the app, so widgets also refresh on their
own budget.

Every user is checked with ``can_view`` on the mower before anything is sent. Failures are
logged and never stop the state handling. State kept in memory only throttles (a restart at
worst sends one extra update); the rows in the store are what makes an activity endable.
"""

import re
from dataclasses import replace
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from mowdeck_contracts.v1 import MowerState
from mowdeck_service_kit import get_logger

from notifications.application.ports import (
    Channels,
    Clock,
    ErrorExplainer,
    Store,
    ViewChecker,
)
from notifications.domain.errors import InvalidInputError, NotFoundError
from notifications.domain.live_activity import (
    ATTRIBUTES_TYPE,
    DISMISS_AFTER_SECONDS,
    RELOAD_MIN_INTERVAL_SECONDS,
    STALE_AFTER_SECONDS,
    UPDATE_MIN_INTERVAL_SECONDS,
    ActivityDevice,
    ActivityRun,
    LiveActivity,
    attributes,
    content_hash,
    content_state,
    error_title,
    final_kind,
    is_job,
)
from notifications.domain.models import DeliveryOutcome, DeliveryResult

_log = get_logger("notifications.live_activity")

_TOKEN = re.compile(r"^[0-9a-fA-F]{64,200}$")
_ACTIVITY_ID = re.compile(r"^[A-Za-z0-9-]{1,100}$")


class ActivityPusher(Protocol):
    """What the APNs channel offers for Live Activities and background pushes."""

    async def push(
        self,
        token: str,
        payload: dict[str, Any],
        *,
        push_type: str,
        priority: int,
        expiration: int = 0,
    ) -> DeliveryResult:
        """Post one push."""
        ...


class LiveActivities:
    """Registration and the push logic."""

    def __init__(
        self,
        store: Store,
        channels: Channels,
        views: ViewChecker,
        clock: Clock,
        errors: ErrorExplainer | None = None,
    ) -> None:
        self._store = store
        self._channels = channels
        self._views = views
        self._clock = clock
        self._errors = errors
        self._last: dict[UUID, str] = {}
        self._active: set[UUID] = set()
        self._checked: set[UUID] = set()
        self._reloaded: dict[tuple[UUID, UUID], datetime] = {}
        self._areas: dict[UUID, dict[str, str]] = {}

    # --- registration (the routes) -------------------------------------------------------

    async def register_device(
        self,
        user_id: UUID,
        account_id: UUID,
        installation_id: UUID,
        *,
        push_to_start_token: str | None,
        widgets_enabled: bool,
    ) -> ActivityDevice:
        """Store (or replace) what the app installation offers."""
        token = push_to_start_token.strip().lower() if push_to_start_token else None
        if token is not None and not _TOKEN.match(token):
            msg = "push_to_start_token: 64 to 200 hexadecimal characters"
            raise InvalidInputError(msg)
        device = ActivityDevice(
            user_id, account_id, installation_id, token, widgets_enabled, self._clock()
        )
        await self._store.put_activity_device(device)
        return device

    async def unregister_device(self, user_id: UUID, installation_id: UUID) -> None:
        """Forget an installation (logout); 404 when it was not registered."""
        if not await self._store.delete_activity_device(user_id, installation_id):
            msg = "installation not found"
            raise NotFoundError(msg)

    async def report_activity(
        self,
        user_id: UUID,
        account_id: UUID,
        installation_id: UUID,
        mower_id: UUID,
        *,
        activity_id: str,
        push_token: str,
    ) -> LiveActivity:
        """The app started (or restarted) an activity for the mower and reports its update
        token. The user must be able to view the mower."""
        token = push_token.strip().lower()
        if not _TOKEN.match(token) or not _ACTIVITY_ID.match(activity_id):
            msg = "push_token or activity_id is malformed"
            raise InvalidInputError(msg)
        if not (await self._views.can_view([user_id], mower_id))[0]:
            msg = "mower not found"
            raise NotFoundError(msg)
        now = self._clock()
        current = await self._store.live_activity(user_id, installation_id, mower_id)
        activity = LiveActivity(
            user_id,
            account_id,
            installation_id,
            mower_id,
            ActivityRun.RUNNING,
            current.started_at if current is not None else now,
            activity_id,
            token,
            current.last_sent_at if current is not None else None,
            current.last_hash if current is not None else None,
        )
        await self._store.put_live_activity(activity)
        return activity

    async def activity_ended(self, user_id: UUID, installation_id: UUID, mower_id: UUID) -> None:
        """The user ended the activity on the device: it is not restarted during this job."""
        current = await self._store.live_activity(user_id, installation_id, mower_id)
        if current is None:
            return
        await self._store.put_live_activity(
            replace(current, state=ActivityRun.DISMISSED, update_token=None)
        )

    def remember_areas(self, mower_id: UUID, names: dict[str, str]) -> None:
        """Area names of the mower's map, for the activity's subtitle."""
        self._areas[mower_id] = names

    # --- state handling (the watcher) ----------------------------------------------------

    def _pusher(self) -> ActivityPusher | None:
        channel = self._channels.get("apns")
        return channel if channel is not None and hasattr(channel, "push") else None  # type: ignore[return-value]

    async def on_state(self, state: MowerState) -> None:
        """React to a state; never raises."""
        try:
            await self._on_state(state)
        except Exception as exc:
            _log.warning(
                "live activity handling failed", mower_id=str(state.mower_id), error=repr(exc)
            )

    async def _on_state(self, state: MowerState) -> None:
        pusher = self._pusher()
        if pusher is None:
            return
        now = self._clock()
        mower_id = state.mower_id
        previous = self._last.get(mower_id)
        self._last[mower_id] = state.activity.value
        changed = previous is not None and previous != state.activity.value
        if changed:
            await self._reload_widgets(pusher, state, now)
        if not is_job(state):
            if mower_id in self._active or mower_id not in self._checked:
                self._checked.add(mower_id)
                self._active.discard(mower_id)
                await self._end(pusher, state, now)
            return
        self._active.add(mower_id)
        self._checked.add(mower_id)
        await self._drive(pusher, state, now, changed=changed)

    async def _recipients(self, state: MowerState) -> list[ActivityDevice]:
        devices = await self._store.activity_devices(state.account_id)
        if not devices:
            return []
        users = sorted({d.user_id for d in devices}, key=str)
        allowed = await self._views.can_view(users, state.mower_id)
        ok = {u for u, a in zip(users, allowed, strict=True) if a}
        return [d for d in devices if d.user_id in ok]

    def _area_name(self, state: MowerState) -> str | None:
        job = state.job
        if job is None or job.area_id is None:
            return None
        return self._areas.get(state.mower_id, {}).get(job.area_id)

    async def _drive(
        self, pusher: ActivityPusher, state: MowerState, now: datetime, *, changed: bool
    ) -> None:
        devices = await self._recipients(state)
        if not devices:
            return
        entry = await self._store.directory_entry(state.mower_id)
        name = entry.name if entry is not None else ""
        content = content_state(state, area_name=self._area_name(state), now=now)
        digest = content_hash(content)
        stamp = int(now.timestamp())
        for device in devices:
            row = await self._store.live_activity(
                device.user_id, device.installation_id, state.mower_id
            )
            if row is None:
                if device.push_to_start_token is None:
                    continue
                payload = {
                    "aps": {
                        "timestamp": stamp,
                        "event": "start",
                        "content-state": content,
                        "attributes-type": ATTRIBUTES_TYPE,
                        "attributes": attributes(state.mower_id, name, now),
                    }
                }
                result = await pusher.push(
                    device.push_to_start_token, payload, push_type="liveactivity", priority=10
                )
                if result.outcome is DeliveryOutcome.GONE:
                    await self._store.put_activity_device(
                        replace(device, push_to_start_token=None, updated_at=now)
                    )
                elif result.outcome is DeliveryOutcome.SENT:
                    await self._store.put_live_activity(
                        LiveActivity(
                            device.user_id,
                            device.account_id,
                            device.installation_id,
                            state.mower_id,
                            ActivityRun.STARTING,
                            now,
                            last_sent_at=now,
                            last_hash=digest,
                        )
                    )
                continue
            if row.state is not ActivityRun.RUNNING or row.update_token is None:
                continue
            if row.last_hash == digest:
                continue
            due = row.last_sent_at is None or (
                (now - row.last_sent_at).total_seconds() >= UPDATE_MIN_INTERVAL_SECONDS
            )
            if not due and not changed:
                continue
            payload = {
                "aps": {
                    "timestamp": stamp,
                    "event": "update",
                    "content-state": content,
                    "stale-date": stamp + STALE_AFTER_SECONDS,
                }
            }
            result = await pusher.push(
                row.update_token,
                payload,
                push_type="liveactivity",
                priority=10 if changed else 5,
                expiration=stamp + 3600,
            )
            if result.outcome is DeliveryOutcome.GONE:
                await self._store.delete_live_activity(
                    row.user_id, row.installation_id, row.mower_id
                )
            elif result.outcome is DeliveryOutcome.SENT:
                await self._store.put_live_activity(
                    replace(row, last_sent_at=now, last_hash=digest)
                )

    async def _end(self, pusher: ActivityPusher, state: MowerState, now: datetime) -> None:
        rows = await self._store.live_activities(state.mower_id)
        if not rows:
            return
        kind = final_kind(state)
        content = content_state(state, area_name=self._area_name(state), now=now, final=kind)
        stamp = int(now.timestamp())
        titles: dict[str, str | None] = {}
        for row in rows:
            if row.state is not ActivityRun.DISMISSED and row.update_token is not None:
                final = dict(content)
                if kind == "error":
                    language = await self._language(row.user_id, row.account_id)
                    if language not in titles:
                        titles[language] = await self._error_title(state, language)
                    if titles[language] is not None:
                        final["errorTitle"] = titles[language]
                payload = {
                    "aps": {
                        "timestamp": stamp,
                        "event": "end",
                        "content-state": final,
                        "dismissal-date": stamp + DISMISS_AFTER_SECONDS[kind],
                    }
                }
                await pusher.push(
                    row.update_token,
                    payload,
                    push_type="liveactivity",
                    priority=10,
                    expiration=stamp + 3600,
                )
            await self._store.delete_live_activity(row.user_id, row.installation_id, row.mower_id)

    async def _language(self, user_id: UUID, account_id: UUID) -> str:
        preferences = await self._store.preferences(user_id, account_id)
        return preferences.language if preferences is not None else "en"

    async def _error_title(self, state: MowerState, language: str) -> str | None:
        """The error in plain words in ``language``; None without a code or an explainer."""
        if self._errors is None or not state.error_code:
            return None
        explanation = await self._errors.explain(
            state.error_code,
            state.connection.value,
            language,
            state.error_category.value if state.error_category is not None else None,
            state.cloud_provider,
        )
        return error_title(explanation)

    async def _reload_widgets(
        self, pusher: ActivityPusher, state: MowerState, now: datetime
    ) -> None:
        devices = [d for d in await self._recipients(state) if d.widgets_enabled]
        if not devices:
            return
        payload = {
            "aps": {"content-available": 1},
            "kind": "widget-reload",
            "mower_id": str(state.mower_id),
        }
        for device in devices:
            key = (state.mower_id, device.installation_id)
            last = self._reloaded.get(key)
            if last is not None and (now - last).total_seconds() < RELOAD_MIN_INTERVAL_SECONDS:
                continue
            self._reloaded[key] = now
            for target in await self._store.apns_devices(device.user_id, device.account_id):
                if target.installation_id == device.installation_id:
                    await pusher.push(target.token, payload, push_type="background", priority=5)
