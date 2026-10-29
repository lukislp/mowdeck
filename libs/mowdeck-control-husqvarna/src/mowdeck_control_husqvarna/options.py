"""Connection options of a Husqvarna mower (``ConnectionConfig.options``).

The gateway merges the linked account's credential fields (``app_key``, ``app_secret``) into the
mower's options; Fleet sets ``iot_id`` (the Husqvarna mower id) and ``device_name`` from the
device list. ``start_minutes`` is the duration of a start without one (see the connection);
every other option (``cloud_provider``, the Mammotion-specific ones of the ``cloud`` kind) is
ignored.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self

from mowdeck_control_husqvarna.descriptor import (
    APP_KEY,
    APP_SECRET,
    DEFAULT_START_MINUTES,
    MAX_START_MINUTES,
    START_MINUTES,
)


def _text(options: Mapping[str, Any], key: str) -> str | None:
    value = options.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"option {key} must be a string"
        raise ValueError(msg)
    return value.strip() or None


def minutes_problem(minutes: int) -> str | None:
    """Why ``minutes`` is no valid start duration; None when it is."""
    if not 1 <= minutes <= MAX_START_MINUTES:
        return f"a start lasts 1 to {MAX_START_MINUTES} minutes"
    return None


@dataclass(frozen=True, slots=True)
class HusqvarnaOptions:
    """The parsed options; the secret is kept out of ``repr``."""

    app_key: str
    app_secret: str = field(repr=False)
    mower_id: str | None = None
    device_name: str | None = None
    start_minutes: int = DEFAULT_START_MINUTES

    @classmethod
    def parse(cls, options: Mapping[str, Any]) -> Self:
        """Raises ``ValueError`` (never naming a credential value) for unusable options."""
        app_key = _text(options, APP_KEY)
        secret = options.get(APP_SECRET)
        if app_key is None or not isinstance(secret, str) or not secret:
            msg = "the linked Husqvarna account's application key and secret are missing"
            raise ValueError(msg)
        mower_id = _text(options, "iot_id")
        device_name = _text(options, "device_name")
        if mower_id is None and device_name is None:
            msg = "iot_id or device_name is needed to pick the mower"
            raise ValueError(msg)
        minutes = options.get(START_MINUTES, DEFAULT_START_MINUTES)
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            msg = "option start_minutes must be a whole number"
            raise ValueError(msg)
        problem = minutes_problem(minutes)
        if problem is not None:
            raise ValueError(f"option start_minutes: {problem}")
        return cls(
            app_key=app_key,
            app_secret=secret,
            mower_id=mower_id,
            device_name=device_name,
            start_minutes=minutes,
        )
