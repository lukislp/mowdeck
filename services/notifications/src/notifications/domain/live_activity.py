"""Live Activities and widget reloads (ADR 0059): the domain values and the content rules.

A *job* is on while the mower mows, is paused or drives back to the dock. The iOS app shows it as
a Live Activity (Lock Screen and Dynamic Island). The server starts the activity by push (the
device's push-to-start token), updates it (the activity's own update token) and ends it. The
JSON of ``content-state`` and ``attributes`` must match the Swift types
``MowingActivityAttributes`` and ``MowingActivityAttributes.ContentState`` of the app
(``apps/mobile/ios/MowDeckWidgets``): names are camelCase, times are whole epoch seconds (the
default ``Date`` coding of ActivityKit is not relied on).
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import MowerActivity, MowerState

ACTIVE_ACTIVITIES = frozenset({MowerActivity.MOWING, MowerActivity.PAUSED, MowerActivity.RETURNING})
"""The mower is in a job."""

ATTRIBUTES_TYPE = "MowingActivityAttributes"
"""The Swift type ActivityKit decodes a push-to-start payload into."""

UPDATE_MIN_INTERVAL_SECONDS = 15.0
"""Updates of one activity at most this often unless the state itself changed."""

RELOAD_MIN_INTERVAL_SECONDS = 120.0
"""Widget reload pushes per mower and device at most this often."""

STALE_AFTER_SECONDS = 15 * 60
"""``stale-date`` of an update: ActivityKit greys the activity when nothing newer arrives."""

DISMISS_AFTER_SECONDS = {"finished": 5 * 60, "error": 30 * 60}
"""How long the ended activity stays on the Lock Screen."""


class ActivityRun(StrEnum):
    """State of a stored activity row."""

    STARTING = "starting"
    """A push-to-start was sent; the app has not reported the activity yet."""
    RUNNING = "running"
    """The app reported the activity and its update token."""
    DISMISSED = "dismissed"
    """The user ended it on the device; the server does not restart it in the same job."""


@dataclass(frozen=True, slots=True)
class ActivityDevice:
    """One app installation of a user: where to push-to-start and whether widgets exist."""

    user_id: UUID
    account_id: UUID
    installation_id: UUID
    push_to_start_token: str | None
    widgets_enabled: bool
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class LiveActivity:
    """A Live Activity of one user's installation for one mower."""

    user_id: UUID
    account_id: UUID
    installation_id: UUID
    mower_id: UUID
    state: ActivityRun
    started_at: datetime
    activity_id: str | None = None
    update_token: str | None = None
    last_sent_at: datetime | None = None
    last_hash: str | None = None


def is_job(state: MowerState) -> bool:
    """True while a job runs on a connected mower."""
    return state.connected and state.activity in ACTIVE_ACTIVITIES


def content_state(
    state: MowerState, *, area_name: str | None, now: datetime, final: str | None = None
) -> dict[str, Any]:
    """The ``ContentState`` JSON. ``final`` (``finished`` or ``error``) marks the last update."""
    job = state.job
    remaining = job.remaining_s if job is not None else None
    data: dict[str, Any] = {
        "state": final or state.activity.value,
        "batteryPercent": state.battery_percent,
        "progressPercent": job.progress_percent if job is not None else None,
        "areaName": area_name,
        "remainingSeconds": remaining,
        "endsAt": int(now.timestamp()) + remaining if remaining is not None else None,
        "updatedAt": int(now.timestamp()),
    }
    return {k: v for k, v in data.items() if v is not None}


def content_hash(content: dict[str, Any]) -> str:
    """A hash of what the user sees: everything but the clock-derived fields."""
    stable = {k: v for k, v in content.items() if k not in {"updatedAt", "endsAt"}}
    # Remaining time is shown as a countdown on the device, so only coarse changes matter.
    if "remainingSeconds" in stable:
        stable["remainingSeconds"] = stable["remainingSeconds"] // 300
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:16]


def attributes(mower_id: UUID, mower_name: str, now: datetime) -> dict[str, Any]:
    """The static ``MowingActivityAttributes`` JSON."""
    return {"mowerId": str(mower_id), "mowerName": mower_name, "startedAt": int(now.timestamp())}


def final_kind(state: MowerState) -> str:
    """``error`` for a stopped mower, else ``finished``."""
    return "error" if state.activity is MowerActivity.ERROR else "finished"
