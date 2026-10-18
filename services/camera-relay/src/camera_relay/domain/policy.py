"""Timings and limits of the relay, and the admission rules of Vision leases.

The defaults are the ones of ADR 0045, taken from ``mowdeck_contracts.v1.relay`` where the
contracts define them. Three values exist only inside the relay and are defined here:
:data:`UNBOUND_LEASE_SECONDS`, :data:`START_TIMEOUT_SECONDS` and the backoff bounds.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from mowdeck_contracts.v1.camera import RELAY_HEARTBEAT_SECONDS
from mowdeck_contracts.v1.relay import (
    RELAY_GRACE_SECONDS,
    RELAY_LEASE_TTL_SECONDS,
    RELAY_TICKET_SECONDS,
    VISION_DAILY_MINUTES,
    VISION_MIN_BATTERY_PERCENT,
    VISION_MIN_QUOTA_PERCENT,
    VISION_SNAPSHOT_HOLD_SECONDS,
    VISION_STREAM_LEASE_SECONDS,
    RelayErrorCode,
)

UNBOUND_LEASE_SECONDS: Final = 30
"""A viewer lease without confirmed media (no WHEP reader receiving bytes) is closed after this
long: it holds the session only while its client connects."""
START_TIMEOUT_SECONDS: Final = 30
"""A session that has no media this long after the start request has failed."""
BACKOFF_INITIAL_SECONDS: Final = 5
"""Wait after the first failed start before the next start on demand."""
BACKOFF_MAX_SECONDS: Final = 60
"""The wait doubles with every failure in a row up to this cap."""
CAMERA_SETTLE_SECONDS: Final = 15
"""A camera whose stream has not appeared this long after the session went live is reported
as not available (``RelayTile.available`` false): a listed camera that never publishes."""
MAX_LEASES_PER_SESSION: Final = 32
"""Open leases per session; one more is answered with ``unavailable`` (the relay is full)."""


@dataclass(frozen=True, slots=True)
class MowerStatus:
    """What the relay knows about a mower from its telemetry (never asked for: never wakes)."""

    online: bool
    asleep: bool = False
    battery_percent: float | None = None
    charging: bool = False
    quota_remaining_percent: float | None = None
    """Remaining share of the vendor's video budget, when the vendor reports one."""

    @property
    def reachable(self) -> bool:
        """Online and awake: the only state in which the relay starts or keeps a source."""
        return self.online and not self.asleep


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why the relay gives no lease (an error of the reply)."""

    code: RelayErrorCode
    detail: str


@dataclass(frozen=True, slots=True)
class RelayPolicy:
    """Timings and limits of one relay instance."""

    grace: timedelta = timedelta(seconds=RELAY_GRACE_SECONDS)
    lease_ttl: timedelta = timedelta(seconds=RELAY_LEASE_TTL_SECONDS)
    heartbeat: timedelta = timedelta(seconds=RELAY_HEARTBEAT_SECONDS)
    unbound_lease: timedelta = timedelta(seconds=UNBOUND_LEASE_SECONDS)
    ticket_lifetime: timedelta = timedelta(seconds=RELAY_TICKET_SECONDS)
    start_timeout: timedelta = timedelta(seconds=START_TIMEOUT_SECONDS)
    backoff_initial: timedelta = timedelta(seconds=BACKOFF_INITIAL_SECONDS)
    backoff_max: timedelta = timedelta(seconds=BACKOFF_MAX_SECONDS)
    vision_snapshot_hold: timedelta = timedelta(seconds=VISION_SNAPSHOT_HOLD_SECONDS)
    vision_stream_lease: timedelta = timedelta(seconds=VISION_STREAM_LEASE_SECONDS)
    vision_daily: timedelta = timedelta(minutes=VISION_DAILY_MINUTES)
    vision_min_quota_percent: float = VISION_MIN_QUOTA_PERCENT
    vision_min_battery_percent: float = VISION_MIN_BATTERY_PERCENT
    max_leases_per_session: int = MAX_LEASES_PER_SESSION
    camera_settle: timedelta = timedelta(seconds=CAMERA_SETTLE_SECONDS)

    def backoff(self, failures: int) -> timedelta:
        """The wait after ``failures`` failed starts in a row: 5, 10, 20, 40, 60, 60, ... s."""
        if failures < 1:
            return timedelta(0)
        return min(self.backoff_initial * (1 << (failures - 1)), self.backoff_max)

    def low_power_or_quota(self, status: MowerStatus | None) -> str | None:
        """The battery or budget limit the mower is below, or None."""
        if status is None:
            return None
        if (
            status.battery_percent is not None
            and status.battery_percent < self.vision_min_battery_percent
            and not status.charging
        ):
            return f"battery below {self.vision_min_battery_percent:g} % and not charging"
        if (
            status.quota_remaining_percent is not None
            and status.quota_remaining_percent < self.vision_min_quota_percent
        ):
            return f"less than {self.vision_min_quota_percent:g} % of the video budget left"
        return None

    def vision_refusal(self, status: MowerStatus | None, used_today: timedelta) -> Refusal | None:
        """Why a Vision lease without a viewer is refused (or must end), or None.

        A Vision lease never wakes a mower: an unknown status counts as not reachable.
        """
        if status is None:
            return Refusal(
                RelayErrorCode.UNAVAILABLE,
                "the mower's state is unknown; a vision lease never wakes a mower",
            )
        if not status.reachable:
            return Refusal(RelayErrorCode.UNAVAILABLE, "the mower is offline or asleep")
        limit = self.low_power_or_quota(status)
        if limit is not None:
            return Refusal(RelayErrorCode.BUDGET, limit)
        if used_today >= self.vision_daily:
            minutes = self.vision_daily.total_seconds() / 60
            return Refusal(
                RelayErrorCode.BUDGET,
                f"the daily {minutes:g} vision minutes of this mower are used",
            )
        return None
