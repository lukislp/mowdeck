"""Device settings declared as capabilities (ADR 0046).

A mower keeps settings of its own that change how it behaves, not what it does right now: its
rain sensor, its battery levels, its lights, its voice, the hours it must not work. Which of
them a mower has differs per model and firmware, so the adapter declares them: each
:class:`SettingSpec` in ``MowerCapabilities.settings`` names a key, the type of its value and
its range or options. The current values arrive in ``MowerState.settings`` (only the keys the
mower has reported), and the generic command ``SetSetting {key, value}`` changes one of them;
the controller checks it against the declaration (:meth:`SettingSpec.value_problem`) before it
reaches the adapter.

A setting never makes the mower move: an adapter only declares keys whose change leaves the
wheels and the blades as they are.

Keys are manufacturer neutral (:data:`SETTING_KEYS` lists the ones MowDeck knows); an adapter
may only declare those, so clients can label and group them without knowing the vendor.
"""

import re
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import Field, StringConstraints, model_validator

from mowdeck_contracts.v1._base import ContractModel

SETTING_KEY_PATTERN: Final = r"^[a-z][a-z0-9_]{0,47}$"
SettingKey = Annotated[str, StringConstraints(pattern=SETTING_KEY_PATTERN)]
"""A setting key: lower case, digits and underscores, at most 48 characters."""

_CLOCK = r"^([01]\d|2[0-3]):[0-5]\d$"
_CLOCK_RE: Final = re.compile(_CLOCK)


class SettingType(StrEnum):
    """The type of a setting's value."""

    BOOL = "bool"
    """``true`` or ``false``."""
    INT = "int"
    """A whole number within ``min``..``max`` on the ``step`` grid (counted from ``min``)."""
    ENUM = "enum"
    """One of ``options``."""
    TIME_WINDOW = "time_window"
    """A daily window (:class:`TimeWindow`), or null when the setting is off."""


class SettingGroup(StrEnum):
    """Where a client shows a setting."""

    WEATHER = "weather"
    BATTERY = "battery"
    LIGHTS = "lights"
    DRIVING = "driving"
    MOWING = "mowing"
    SAFETY = "safety"
    VOICE = "voice"
    SCHEDULE = "schedule"


class TimeWindow(ContractModel):
    """A daily window in the mower's local time, ``HH:MM`` on a 24-hour clock. ``end`` before
    ``start`` crosses midnight (``22:00`` to ``06:00``)."""

    start: str = Field(pattern=_CLOCK, description="Start, `HH:MM` (24-hour clock).")
    end: str = Field(pattern=_CLOCK, description="End, `HH:MM` (24-hour clock).")

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if self.start == self.end:
            msg = "start and end must differ (send null to switch the window off)"
            raise ValueError(msg)
        return self

    @staticmethod
    def minutes(clock: str) -> int:
        """Minutes after midnight of an ``HH:MM`` value."""
        if not _CLOCK_RE.match(clock):
            msg = f"not a time of day: {clock!r}"
            raise ValueError(msg)
        hours, minutes = clock.split(":")
        return int(hours) * 60 + int(minutes)


SettingValue = bool | int | str | TimeWindow | None
"""The value of a setting: ``bool``, ``int``, an option (``str``), a :class:`TimeWindow`, or null
(a ``time_window`` setting that is off)."""


class SettingSpec(ContractModel):
    """One setting a mower has, as its connection declares it (``MowerCapabilities.settings``)."""

    key: SettingKey = Field(description="Manufacturer-neutral key, one of the known keys.")
    type: SettingType
    group: SettingGroup
    min: int | None = Field(default=None, description="Smallest value of an `int` setting.")
    max: int | None = Field(default=None, description="Largest value of an `int` setting.")
    step: int | None = Field(
        default=None, gt=0, description="Grid of an `int` setting, counted from `min`."
    )
    unit: str | None = Field(
        default=None, max_length=8, description="Unit of an `int` setting, e.g. `%` or `h`."
    )
    options: list[str] = Field(
        default_factory=list,
        max_length=16,
        description="The options of an `enum` setting, in the order a client offers them.",
    )

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        if self.key not in SETTING_KEYS:
            msg = f"unknown setting key {self.key!r}"
            raise ValueError(msg)
        if self.type is SettingType.INT:
            if self.min is None or self.max is None or self.min > self.max:
                msg = "an int setting needs min <= max"
                raise ValueError(msg)
        elif self.min is not None or self.max is not None or self.step is not None:
            msg = "min, max and step belong to int settings"
            raise ValueError(msg)
        if self.type is SettingType.ENUM:
            if not self.options or len(set(self.options)) != len(self.options):
                msg = "an enum setting needs distinct options"
                raise ValueError(msg)
        elif self.options:
            msg = "options belong to enum settings"
            raise ValueError(msg)
        return self

    def value_problem(self, value: SettingValue) -> str | None:
        """Why ``value`` is not valid for this setting; None when it is."""
        match self.type:
            case SettingType.BOOL:
                if not isinstance(value, bool):
                    return f"{self.key} takes true or false"
            case SettingType.INT:
                if isinstance(value, bool) or not isinstance(value, int):
                    return f"{self.key} takes a whole number"
                assert self.min is not None and self.max is not None  # noqa: S101 - validated
                if not self.min <= value <= self.max:
                    return f"{self.key} must lie within {self.min}..{self.max}"
                if self.step is not None and (value - self.min) % self.step:
                    return f"{self.key} must be {self.min} plus a multiple of {self.step}"
            case SettingType.ENUM:
                if not isinstance(value, str) or value not in self.options:
                    return f"{self.key} takes one of {', '.join(self.options)}"
            case SettingType.TIME_WINDOW:
                if value is not None and not isinstance(value, TimeWindow):
                    return f"{self.key} takes a window {{start, end}} or null"
        return None


SETTING_KEYS: Final[dict[str, str]] = {
    "rain_detection": "Stop mowing and return to the dock when the rain sensor detects rain.",
    "recharge_level": "Return to the dock to charge at this battery level (percent).",
    "smart_recharge_level": "Let the mower choose when it returns to charge.",
    "resume_level": "Resume an interrupted job once charged to this level (percent).",
    "smart_resume_level": "Let the mower choose when it resumes after charging.",
    "night_light": "Switch the light on by itself while working in the dark.",
    "side_led": "The side lights of the body.",
    "turn_style": "How the mower turns at lane ends in its own jobs (ADR 0043).",
    "traversal_mode": "How the mower drives between areas and back to the dock.",
    "blade_speed": "Speed of the blade motor while mowing (slow, normal or fast).",
    "wildlife_safety": "What the mower does when it detects an animal.",
    "voice_enabled": "Spoken announcements of the mower.",
    "voice_volume": "Volume of the spoken announcements (percent).",
    "voice_gender": "Voice of the spoken announcements.",
    "non_work_hours": "Daily window in which the mower must not work (null: none).",
}
"""Every setting key MowDeck knows, with what it means. An adapter declares a subset."""


__all__ = [
    "SETTING_KEYS",
    "SETTING_KEY_PATTERN",
    "SettingGroup",
    "SettingKey",
    "SettingSpec",
    "SettingType",
    "SettingValue",
    "TimeWindow",
]
