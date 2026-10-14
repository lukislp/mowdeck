"""Automatic analysis on errors (ADR 0035): which states start one, and the limits.

Pure functions, no I/O. A state starts an automatic analysis when the mower *enters* a
recoverable error: activity ``error``, not lifted (``raw.lifted``), and either its error code is
in the configured set (stuck, blocked by an obstacle, outside the work area, wheel slip) or the
adapter marked it ``drive_recoverable``. Low battery, blade and motor errors and every lift or
tilt stop never do. The error code is normalised (trimmed, lower case, a leading minus sign
dropped: Mammotion firmware reports faults as negative numbers).
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from mowdeck_contracts.v1 import MowerActivity, MowerState

DRIVE_RECOVERABLE = "drive_recoverable"
"""The code recorded when a ``drive_recoverable`` state carries no error code."""


def normalise_code(code: str) -> str:
    """``" -1201 "`` becomes ``"1201"``, ``"Stuck"`` becomes ``"stuck"``."""
    return code.strip().lower().lstrip("-").strip()


def parse_codes(text: str | Iterable[str]) -> frozenset[str]:
    """A comma separated list (or an iterable) of codes, normalised, empty entries dropped."""
    items = text.split(",") if isinstance(text, str) else list(text)
    return frozenset(c for c in (normalise_code(i) for i in items) if c)


@dataclass(frozen=True, slots=True)
class AutoConfig:
    """Limits of automatic analyses (from the settings)."""

    enabled: bool = True
    cooldown_minutes: int = 10
    daily_per_mower: int = 20
    codes: frozenset[str] = field(default_factory=frozenset)
    max_state_age_seconds: float = 120.0
    watch_refresh_seconds: float = 60.0
    """A replica that believes a mower is fine still writes a non-error state to the shared watch
    this often, so an error another replica recorded is cleared (multi-replica safety)."""


def recoverable_error(state: MowerState, codes: frozenset[str]) -> str | None:
    """The normalised code when ``state`` is a recoverable error, else None."""
    if state.activity is not MowerActivity.ERROR:
        return None
    if state.raw.get("lifted") is True:
        return None
    code = normalise_code(state.error_code) if state.error_code else ""
    if code and code in codes:
        return code[:64]
    if state.drive_recoverable:
        return (code or DRIVE_RECOVERABLE)[:64]
    return None


class Skip(StrEnum):
    """Why an error did not start an automatic analysis (logged)."""

    DISABLED = "disabled"
    STALE = "stale"
    COOLDOWN = "cooldown"
    OPEN_PROPOSAL = "open_proposal"
    DAILY_BUDGET = "daily_budget"
    NOT_IN_PLAN = "not_in_plan"
    NO_SUBSCRIBER = "no_subscriber"
    NO_PHOTO = "no_photo"


__all__ = [
    "DRIVE_RECOVERABLE",
    "AutoConfig",
    "Skip",
    "normalise_code",
    "parse_codes",
    "recoverable_error",
]
