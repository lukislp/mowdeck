"""Manufacturer-neutral categories for Mammotion error codes, and the codes a mower reports.

Pure, no I/O. Every code of PyMammotion's error table (``pymammotion/data/error_codes.csv``, about
660 rows, plus the firmware-only codes PyMammotion folds in) gets one category:

* recoverable, the mower can be freed by driving it or by clearing its way: ``trapped``,
  ``obstacle``, ``outside`` (out of the task area or in a no-go zone), ``wheel_slip``. Only the
  curated codes in :data:`RECOVERABLE_CODES` get one of these, never a keyword guess, because a
  recoverable state is marked ``drive_recoverable`` (manual driving allowed, Vision's automatic
  analysis may start);
* never recoverable: ``lift_tilt`` (:data:`LIFT_TILT_CODES` and the lift, tilt or turned over
  texts), ``collision`` (bumper), ``blade_motor`` (blade, cutter, wheel and other motor faults,
  also the cutting height lift motor), ``battery``, ``rain``, ``positioning`` (RTK, satellites,
  visual positioning), ``docking`` (the charging station and the way back to it),
  ``communication`` (Wi-Fi, 4G, Bluetooth, the vendor's servers), ``firmware`` (updates),
  ``other``.

The non-recoverable categories come from the English text and the module column of the table, in
the order above, so a lift sensor that stopped the blade is ``lift_tilt``, not ``blade_motor``.

Codes arrive negative from the firmware (``-1201``); the table keys them positive, so every
function here drops the sign.
"""

import json
import re
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any, Final

from mowdeck_control_mammotion.port import bundled_error_codes, describe_error, error_info


class ErrorCategory(StrEnum):
    """What kind of fault a code names (``MowerState.raw["error_category"]``)."""

    TRAPPED = "trapped"
    OBSTACLE = "obstacle"
    OUTSIDE = "outside"
    WHEEL_SLIP = "wheel_slip"
    LIFT_TILT = "lift_tilt"
    COLLISION = "collision"
    BATTERY = "battery"
    BLADE_MOTOR = "blade_motor"
    RAIN = "rain"
    POSITIONING = "positioning"
    COMMUNICATION = "communication"
    DOCKING = "docking"
    FIRMWARE = "firmware"
    OTHER = "other"


RECOVERABLE_CATEGORIES: Final = frozenset(
    {
        ErrorCategory.TRAPPED,
        ErrorCategory.OBSTACLE,
        ErrorCategory.OUTSIDE,
        ErrorCategory.WHEEL_SLIP,
    }
)
"""Categories a person can resolve by driving the mower or clearing its way."""

RECOVERABLE_CODES: Final[Mapping[int, ErrorCategory]] = {
    # stuck or trapped
    1201: ErrorCategory.TRAPPED,  # the robot is stuck
    1421: ErrorCategory.TRAPPED,  # stuck during recharge
    11112: ErrorCategory.TRAPPED,  # robot is trapped
    2000701: ErrorCategory.TRAPPED,  # front wheels may be stuck
    # blocked by an obstacle
    1206: ErrorCategory.OBSTACLE,  # obstacles on the channel
    1214: ErrorCategory.OBSTACLE,  # the channel (recharge route) is obstructed
    11113: ErrorCategory.OBSTACLE,  # channel blocked by obstacles
    11121: ErrorCategory.OBSTACLE,  # obstacle ahead, unable to proceed
    11122: ErrorCategory.OBSTACLE,  # channel to the target area blocked
    # outside the task area or inside a no-go zone
    100: ErrorCategory.OUTSIDE,
    1100: ErrorCategory.OUTSIDE,
    1145: ErrorCategory.OUTSIDE,
    1411: ErrorCategory.OUTSIDE,
    1422: ErrorCategory.OUTSIDE,  # failed to return and out of a task area
    # wheel slip
    1032: ErrorCategory.WHEEL_SLIP,
    1218: ErrorCategory.WHEEL_SLIP,  # slippage when leaving the charging station
    11132: ErrorCategory.WHEEL_SLIP,  # wheel slip while reversing
    11134: ErrorCategory.WHEEL_SLIP,  # wheel slip while turning on the spot
}
"""The curated recoverable codes (PyMammotion 0.10.9 table, checked by hand)."""

LIFT_TILT_CODES: Final = frozenset({1004, 1030, 2502, 2801, 11131, 11133})
"""Lifted or tilted: 1004 tilt sensor, 1030 front wheels lifted or tilted, 2502 tilted too much,
2801 lift sensor, 11131 and 11133 rear lift sensor while reversing or turning."""

_KEYWORD_RULES: Final[tuple[tuple[ErrorCategory, re.Pattern[str]], ...]] = (
    (
        ErrorCategory.LIFT_TILT,
        re.compile(
            r"lift sensor|\blifted|\btilt|turned over|overturn|roll ?over|upside down|tipped",
            re.I,
        ),
    ),
    (ErrorCategory.COLLISION, re.compile(r"bumper|collision|collided", re.I)),
    (
        ErrorCategory.BLADE_MOTOR,
        re.compile(
            r"blade|cutter|cutting (disk|disc|motor|head)|knife|motor|wheel hub|driver board",
            re.I,
        ),
    ),
    (
        ErrorCategory.BATTERY,
        re.compile(r"battery|low power|power is low|insufficient power|voltage", re.I),
    ),
    (ErrorCategory.RAIN, re.compile(r"\brain", re.I)),
    (
        ErrorCategory.POSITIONING,
        re.compile(r"\brtk\b|gnss|gps|satellite|positioning|reference station|base station", re.I),
    ),
    (
        ErrorCategory.DOCKING,
        re.compile(
            r"charging station|charging area|recharg|\bdock|infrared|charging error|return home",
            re.I,
        ),
    ),
    (
        ErrorCategory.COMMUNICATION,
        re.compile(
            r"wi-?fi|\b4g\b|bluetooth|network|server|connection|\blora\b|service (timeout|exc)",
            re.I,
        ),
    ),
    (ErrorCategory.FIRMWARE, re.compile(r"firmware|upgrad|version mismatch", re.I)),
)
_MODULE_RULES: Final[Mapping[str, ErrorCategory]] = {
    "motor": ErrorCategory.BLADE_MOTOR,
    "power": ErrorCategory.BATTERY,
    "position": ErrorCategory.POSITIONING,
    "upgrade": ErrorCategory.FIRMWARE,
}


SELF_CHECK_NORMAL: Final = frozenset({0, 10})
"""``report_data.dev.self_check_status`` values that mean nothing stops the mower."""
SELF_CHECK_STATES: Final[Mapping[int, str]] = {
    11: "emergency_stop",
    12: "stop_button",
    13: "safety_key_missing",
    14: "lifted",
    15: "tilted",
    16: "tipped_over",
    17: "bumper_missing",
    20: "rain",
    21: "low_battery",
    23: "non_working_hours",
    24: "rtk_station_moved",
    25: "outside_task_area",
    26: "wheel_motor_error",
    27: "blade_motor_overheated",
    29: "dock_moved",
    30: "no_task_area",
    31: "night_protection",
    34: "rain_protection",
    40: "rtk_not_ready",
    41: "weak_satellite_signal",
    42: "positioning_failure",
    43: "inavi_unavailable",
    99: "weak_rtk_signal",
}
"""The one condition the self-check names as stopping the mower from starting (the vendor app's
titles, as Mammotion-HA's ``SELF_CHECK_STATES`` lists them)."""


def self_check_name(status: int) -> str:
    """The name of a self-check status, ``other_<n>`` for one the table does not know."""
    return SELF_CHECK_STATES.get(status, f"other_{status}")


def normalise_code(code: int | str) -> int | None:
    """The positive integer code (``"-1201"`` gives 1201), None when it is no number."""
    try:
        return abs(int(str(code).strip()))
    except ValueError:
        return None


def category_of(code: int | str) -> ErrorCategory:
    """The category of ``code`` (see the module docstring); ``other`` for unknown codes."""
    number = normalise_code(code)
    if number is None:
        return ErrorCategory.OTHER
    if number in RECOVERABLE_CODES:
        return RECOVERABLE_CODES[number]
    if number in LIFT_TILT_CODES:
        return ErrorCategory.LIFT_TILT
    info = error_info(number)
    text = describe_error(number)
    if info is not None:
        text = f"{text} {info.description}"
    for category, pattern in _KEYWORD_RULES:
        if pattern.search(text):
            return category
    if info is not None and info.module in _MODULE_RULES:
        return _MODULE_RULES[info.module]
    return ErrorCategory.OTHER


def recoverable(code: int | str) -> bool:
    """True for a code of a recoverable category (trapped, obstacle, outside, wheel slip)."""
    return category_of(code) in RECOVERABLE_CATEGORIES


def known_codes() -> list[int]:
    """Every code of PyMammotion's bundled table, ascending (for coverage checks)."""
    return sorted(n for c in bundled_error_codes() if (n := normalise_code(c)) is not None)


def warning_codes(identifier: str, value: Mapping[str, Any] | None) -> list[int]:
    """The codes of a ``thing/event`` warning post, decoded as Mammotion-HA decodes them.

    ``device_warning_event``: ``value`` is ``{"code": 1201}``. ``device_warning_code_event``:
    ``value["data"]`` is a JSON list ``[{"c": -1201, "ct": 1, "ft": <ms>}]``. Other identifiers
    carry no warning (empty list). The posts' own times are not used (``ft`` is the mower's
    local wall clock): the adapter stamps a warning with its receive time.
    """
    if not value:
        return []
    if identifier == "device_warning_event":
        number = normalise_code(value.get("code", ""))
        return [number] if number else []
    if identifier != "device_warning_code_event":
        return []
    data: Any = value.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return []
    items: Iterable[Any] = data if isinstance(data, list) else [data]
    codes: list[int] = []
    for item in items:
        if isinstance(item, Mapping):
            number = normalise_code(item.get("c", item.get("code", "")))
            if number:
                codes.append(number)
    return codes


__all__ = [
    "LIFT_TILT_CODES",
    "RECOVERABLE_CATEGORIES",
    "RECOVERABLE_CODES",
    "SELF_CHECK_NORMAL",
    "SELF_CHECK_STATES",
    "ErrorCategory",
    "category_of",
    "known_codes",
    "normalise_code",
    "recoverable",
    "self_check_name",
    "warning_codes",
]
