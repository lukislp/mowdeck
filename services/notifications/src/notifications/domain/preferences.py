"""Per-user filtering: enabled kinds, mower selection and quiet hours (time zone aware)."""

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from notifications.domain.models import NotificationEvent, Preferences, QuietHours

DEFAULT_TIMEZONE = "UTC"
"""Time zone of the defaults; it only matters for quiet hours, which the defaults do not have."""


def default_preferences(user_id: UUID, account_id: UUID, *, language: str) -> Preferences:
    """What a user who never saved preferences gets.

    Every mower the user can view (``all_mowers``, still re-checked with ``can_view`` per
    event), the default kinds (``job_started`` off), the default channels, no quiet hours,
    ``language`` (the service's default language) and UTC. ``stored`` stays False so the app
    can tell the user that these are the defaults.
    """
    return Preferences(
        user_id=user_id,
        account_id=account_id,
        all_mowers=True,
        timezone=DEFAULT_TIMEZONE,
        language=language,
    )


def valid_timezone(name: str) -> bool:
    """True for an IANA time zone name such as ``Europe/Berlin``."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def in_quiet_hours(quiet: QuietHours | None, timezone: str, at: datetime) -> bool:
    """True when ``at`` falls into the daily quiet window in ``timezone``.

    The window may cross midnight (22:00 to 07:00); ``start == end`` is never quiet. An unknown
    time zone falls back to UTC.
    """
    if quiet is None or quiet.start == quiet.end:
        return False
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    local = at.astimezone(zone).time().replace(tzinfo=None)
    if quiet.start < quiet.end:
        return quiet.start <= local < quiet.end
    return local >= quiet.start or local < quiet.end


def wants(preferences: Preferences, event: NotificationEvent) -> bool:
    """True when the user follows the event's mower and enabled its kind."""
    return event.kind in preferences.enabled_kinds and preferences.subscribed(event.mower_id)
