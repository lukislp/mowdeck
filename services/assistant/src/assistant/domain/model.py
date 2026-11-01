"""What the assistant may propose and what a proposal is (ADR 0075).

The assistant only proposes; the web app executes a confirmed proposal through the regular
Fleet command and Scheduler endpoints with the user's own token, so ADR 0016 authorization,
plans, the controller's safety rules (ADR 0012) and the audit apply unchanged.

Action kinds and the calls the web app makes for them:

==============  =====================================================  ===========================
kind            call after confirmation                                relations (ADR 0016)
==============  =====================================================  ===========================
``start_now``   ``POST /fleet/mowers/{id}/commands`` ``start_mow``      ``can_control``
``start_later`` ``POST /scheduler/schedules`` (kind ``once``)           ``can_control`` and
                                                                       ``can_edit_schedule``
``pause``       ``POST /fleet/mowers/{id}/commands`` ``pause_mow``      ``can_control``
``resume``      ``POST /fleet/mowers/{id}/commands`` ``resume_mow``     ``can_control``
``dock``        ``POST /fleet/mowers/{id}/commands`` ``return_to_dock`` ``can_control``
==============  =====================================================  ===========================

No manual driving, no spot runs, no settings, no emergency stop: none of them is in this list,
so neither the model nor the validator can produce one.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID


class ActionKind(StrEnum):
    """What a proposal can contain."""

    START_NOW = "start_now"
    START_LATER = "start_later"
    PAUSE = "pause"
    RESUME = "resume"
    DOCK = "dock"


IMMEDIATE: Final = frozenset(
    {ActionKind.START_NOW, ActionKind.PAUSE, ActionKind.RESUME, ActionKind.DOCK}
)
"""Kinds that run at once (at most one per proposal)."""

CAN_VIEW: Final = "can_view"
CAN_CONTROL: Final = "can_control"
CAN_EDIT_SCHEDULE: Final = "can_edit_schedule"

RELATIONS: Final[dict[ActionKind, frozenset[str]]] = {
    ActionKind.START_NOW: frozenset({CAN_CONTROL}),
    ActionKind.START_LATER: frozenset({CAN_CONTROL, CAN_EDIT_SCHEDULE}),
    ActionKind.PAUSE: frozenset({CAN_CONTROL}),
    ActionKind.RESUME: frozenset({CAN_CONTROL}),
    ActionKind.DOCK: frozenset({CAN_CONTROL}),
}
"""The relations on the mower each kind needs (the same the executing endpoints check)."""

ACTIVITIES: Final[dict[ActionKind, frozenset[str] | None]] = {
    ActionKind.START_NOW: frozenset({"idle", "docked", "charging"}),
    ActionKind.START_LATER: None,
    ActionKind.PAUSE: frozenset({"mowing", "returning"}),
    ActionKind.RESUME: frozenset({"paused"}),
    ActionKind.DOCK: frozenset({"idle", "mowing", "paused", "error"}),
}
"""Activities in which an immediate kind makes sense (None: any). With an unknown activity
every kind is offered and the controller decides (ADR 0012)."""

MAX_ACTIONS: Final = 3
MAX_LATER: Final = 2
MAX_AREAS: Final = 50
LATER_MIN_LEAD_SECONDS: Final = 120
"""A one-off start lies at least this far in the future."""
LATER_MAX_DAYS: Final = 7
"""... and at most this many days ahead."""
DURATION_DEFAULT: Final = 120
DURATION_RANGE: Final = (15, 720)
"""Window of a one-off start in minutes (the Scheduler's ``duration_minutes``)."""


class Note(StrEnum):
    """Why (part of) a request has no action, as the model or the phrase parser reports it.

    The web app explains each in the user's language; free text from the model is never
    shown (it could carry injected instructions or links)."""

    MANUAL_DRIVE = "manual_drive"
    """Driving by chat is not offered (safety, ADR 0075)."""
    SETTINGS = "settings"
    """Changing settings by chat is not offered in this version."""
    TIMED_RESUME = "timed_resume"
    """"Pause until 15:00": pausing works, resuming at a time does not (yet)."""
    OTHER_MOWER = "other_mower"
    """The request names another mower: proposals are per mower."""
    UNSUPPORTED = "unsupported"
    """Something the action list has no kind for."""
    UNCLEAR = "unclear"
    """The request could not be understood."""


class Unavailable(StrEnum):
    """Why a kind is not offered for this mower right now."""

    PERMISSION = "permission"
    NO_AREAS = "no_areas"
    STATE = "state"
    NOT_CONNECTED = "not_connected"


class Rejection(StrEnum):
    """Why one action of the model's answer was refused by the validator."""

    UNKNOWN_KIND = "unknown_kind"
    NOT_OFFERED = "not_offered"
    UNKNOWN_AREA = "unknown_area"
    TOO_MANY_AREAS = "too_many_areas"
    UNEXPECTED_FIELD = "unexpected_field"
    TIME_MISSING = "time_missing"
    TIME_INVALID = "time_invalid"
    TIME_PAST = "time_past"
    TIME_TOO_FAR = "time_too_far"
    DURATION_INVALID = "duration_invalid"
    TOO_MANY_ACTIONS = "too_many_actions"
    CONFLICT = "conflict"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class Area:
    """A map area of the mower with the name the user sees (custom name, else the mower's)."""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Catalog:
    """Everything the model may choose from for one mower and one request.

    Built from the mower's real state on the caller's token: its relations, connection
    (capabilities), map areas and activity. The model's schema is generated from it and the
    validator checks the answer against it again."""

    mower_id: UUID
    mower_name: str
    now: datetime
    timezone: str
    areas: tuple[Area, ...]
    offered: frozenset[ActionKind]
    unavailable: dict[ActionKind, Unavailable] = field(default_factory=dict)
    activity: str | None = None

    def area(self, area_id: str) -> Area | None:
        """The area with ``area_id``."""
        return next((a for a in self.areas if a.id == area_id), None)


@dataclass(frozen=True, slots=True)
class ProposedAction:
    """One validated action of a proposal, exactly as the web app will execute it."""

    kind: ActionKind
    area_ids: tuple[str, ...] = ()
    start_at: datetime | None = None
    """UTC instant of a ``start_later``."""
    start_local: str | None = None
    """``YYYY-MM-DDTHH:MM`` wall-clock time of a ``start_later`` in ``timezone``."""
    duration_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class RejectedAction:
    """An action of the model's answer that the validator refused."""

    code: Rejection
    kind: ActionKind | None = None
    """The kind when it was a known one (unknown kinds are never echoed back)."""


@dataclass(frozen=True, slots=True)
class Interpretation:
    """The validated outcome of one request.

    ``actions`` is empty whenever anything was rejected: a proposal is all or nothing, so a
    partly understood request never runs half of what the user meant."""

    actions: tuple[ProposedAction, ...]
    rejected: tuple[RejectedAction, ...] = ()
    notes: tuple[Note, ...] = ()

    @property
    def executable(self) -> bool:
        """True when there is something to confirm."""
        return bool(self.actions) and not self.rejected
