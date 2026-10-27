"""Open, close and read the deploy maintenance window (ADR 0053).

The deploy opens the window before it restarts services and closes it after the health checks;
it expires by itself, so a forgotten window cannot silence ``mower_offline`` for long. The rules
read the window through the store (``Watcher``); this module only writes it.
"""

from datetime import datetime, timedelta

from notifications.application.ports import Store
from notifications.domain.models import MaintenanceWindow


class InvalidWindowError(ValueError):
    """The requested window length is outside the allowed range."""


async def open_window(
    store: Store,
    now: datetime,
    *,
    minutes: int,
    max_minutes: int,
    reason: str = "deploy",
) -> MaintenanceWindow:
    """Open (or re-open) the window for ``minutes``, at most ``max_minutes``."""
    if minutes < 1 or minutes > max_minutes:
        msg = f"minutes must be between 1 and {max_minutes}"
        raise InvalidWindowError(msg)
    window = MaintenanceWindow(
        opened_at=now, expires_at=now + timedelta(minutes=minutes), reason=reason[:100]
    )
    await store.put_maintenance_window(window)
    return window


async def close_window(store: Store, now: datetime) -> MaintenanceWindow | None:
    """Close the window now; None when none is open (never opened, closed or expired)."""
    window = await store.maintenance_window()
    if window is None or not window.is_open(now):
        return None
    closed = MaintenanceWindow(
        opened_at=window.opened_at,
        expires_at=window.expires_at,
        closed_at=now,
        reason=window.reason,
    )
    await store.put_maintenance_window(closed)
    return closed
