"""The Scheduler service and the internal Fleet requests it uses.

Commands of a schedule go through Fleet, so Fleet keeps the single command record and audit:

* :data:`~mowdeck_contracts.v1.subjects.FLEET_COMMANDS_ISSUE` (core NATS request/reply, internal
  only): an :class:`IssueCommandRequest` names the subject on whose behalf the command is sent
  (the schedule's owner, or the caller of a manual run). Fleet performs the same checks as its
  HTTP endpoint for that subject (the relation of the command kind on the mower, 404 semantics
  for invisible mowers), records the command with ``origin`` and ``schedule_id`` and answers
  with an :class:`IssueCommandReply`.
* :data:`~mowdeck_contracts.v1.subjects.FLEET_TARGETS_RESOLVE` (core NATS request/reply): a
  :class:`ResolveTargetRequest` asks for the mowers of a mower or group target of one account,
  with the time zone and the optional coordinates of their sites.

Events of the Scheduler (stream ``EVENTS``):

* ``scheduler.run.recorded`` (:class:`ScheduleRunRecorded`): one attempt of a schedule for one
  mower, whatever its outcome.
* ``scheduler.schedule.disabled`` (:class:`ScheduleDisabled`): the Scheduler disabled a schedule
  by itself, for example because its owner lost ``can_control`` on the target.
"""

from enum import StrEnum
from typing import Final, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime
from mowdeck_contracts.v1.capabilities import MowerCapabilities
from mowdeck_contracts.v1.commands import CommandAccepted, CommandRequest
from mowdeck_contracts.v1.mower import (
    MAX_BORDER_LAPS,
    MAX_OBSTACLE_LAPS,
    MowPattern,
    ObstacleDetection,
    PathOrder,
)
from mowdeck_contracts.v1.principal import PrincipalRef

SCHEDULER_RUN_RECORDED: Final = "scheduler.run.recorded"
"""Event type: one run of a schedule for one mower (payload :class:`ScheduleRunRecorded`)."""
SCHEDULER_SCHEDULE_DISABLED: Final = "scheduler.schedule.disabled"
"""Event type: the Scheduler disabled a schedule (payload :class:`ScheduleDisabled`)."""

CommandOrigin = Literal["scheduler", "vision"]
"""Services allowed to issue commands on behalf of a subject."""


class IssueCommandRequest(ContractModel):
    """Request on ``fleet.commands.issue``: send a command on behalf of a subject.

    ``account_id`` must be the mower's account (otherwise the answer is ``not_found``).
    Service principals never hold object rights, so ``on_behalf_of.kind = service`` is refused.
    """

    on_behalf_of: PrincipalRef
    account_id: UUID
    mower_id: UUID
    command: CommandRequest
    origin: CommandOrigin = "scheduler"
    schedule_id: UUID | None = None


class IssueCommandErrorCode(StrEnum):
    """Why Fleet refused to issue a command."""

    NOT_FOUND = "not_found"
    """The mower does not exist in the account, or the subject cannot see it."""
    FORBIDDEN = "forbidden"
    """The subject sees the mower but lacks the relation of the command kind."""
    INVALID = "invalid"
    """The request is not acceptable (unknown kind, service principal)."""
    UNAVAILABLE = "unavailable"
    """The command was recorded but the bus did not take it (the record is ``failed``)."""
    NOT_IN_PLAN = "not_in_plan"
    """The plan of the account does not include the command (feature ``control``, ADR 0028);
    unlike ``forbidden`` it says nothing about the subject's rights."""


class IssueCommandError(ContractModel):
    """The error of an :class:`IssueCommandReply`."""

    code: IssueCommandErrorCode
    detail: str = Field(max_length=500)


class IssueCommandReply(ContractModel):
    """Reply to :class:`IssueCommandRequest`: exactly one of ``accepted`` or ``error``."""

    accepted: CommandAccepted | None = None
    error: IssueCommandError | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.accepted is None) == (self.error is None):
            msg = "exactly one of accepted or error must be given"
            raise ValueError(msg)
        return self


SCHEDULE_PATTERN_KEYS: Final = (
    "name",
    "angle_deg",
    "border_laps",
    "spacing_m",
    "obstacle_detection",
    "obstacle_laps",
    "order",
)
"""The ``MowPattern`` keys a schedule may set (all but ``start_progress_pct``)."""


class SchedulePattern(ContractModel):
    """The start settings one schedule sets for each of its runs (ADR 0024 amendment of
    2026-10-24): the keys of :class:`MowPattern` that make sense for a recurring job, each
    optional. A key left out (None) is not part of the schedule: the run takes it from the
    mower's mow defaults in Fleet, and without those the mower keeps its own setting (for the
    pattern, angle and border laps the clients' fallback, stripes at 0 degrees with one lap).

    ``start_progress_pct`` is not offered: it skips the first part of one job's route, which
    only makes sense to resume an interrupted job, never for every run of a schedule.

    Which values fit one mower is decided with its :class:`MowerCapabilities`
    (:meth:`problem`); a group schedule must fit every mower of the group.
    """

    name: Literal["stripes", "diagonal", "checkerboard", "edge_only", "random_angle"] | None = (
        Field(default=None, description="The native pattern; null: the mow defaults'.")
    )
    angle_deg: float | None = Field(
        default=None,
        ge=0,
        le=180,
        description="Base angle in degrees; null: the mow defaults'. A schedule's "
        "`rotation_angles` replace it run by run; with `rotation_step_deg` it is the angle of "
        "the first run, later runs turn the last angle by the step (ADR 0061).",
    )
    border_laps: int | None = Field(default=None, ge=0, le=MAX_BORDER_LAPS)
    spacing_m: float | None = Field(
        default=None,
        ge=0.05,
        le=2.0,
        description="Path spacing in metres; needs `MowerCapabilities.path_spacing_range_mm`.",
    )
    obstacle_detection: ObstacleDetection | None = Field(
        default=None, description="Needs the mode in `start_options.obstacle_detection`."
    )
    obstacle_laps: int | None = Field(
        default=None,
        ge=0,
        le=MAX_OBSTACLE_LAPS,
        description="Needs `start_options.obstacle_laps_range`.",
    )
    order: PathOrder | None = Field(default=None, description="Needs the order in `orders`.")

    def keys(self) -> dict[str, object]:
        """The keys the schedule sets, as JSON values (``StartMow.pattern`` keys)."""
        return self.model_dump(mode="json", exclude_none=True)

    def problem(self, capabilities: MowerCapabilities) -> str | None:
        """Why these settings do not fit a mower with ``capabilities``; None when they do.

        The angle and the border laps belong to every native pattern and need no declaration;
        the pattern name needs ``native_patterns``, the spacing ``path_spacing_range_mm`` and
        the job options ``start_options`` (:meth:`StartOptions.problem`).
        """
        if self.name is not None and not capabilities.supports_pattern(self.name):
            return f"pattern {self.name!r} is not offered"
        if self.spacing_m is not None:
            if capabilities.path_spacing_range_mm is None:
                return "the path spacing is not settable on this mower"
            low, high = capabilities.path_spacing_range_mm
            if not low <= round(self.spacing_m * 1000) <= high:
                return f"spacing_m must be between {low / 1000:g} and {high / 1000:g} on this mower"
        options = MowPattern(
            obstacle_detection=self.obstacle_detection,
            obstacle_laps=self.obstacle_laps,
            order=self.order,
        )
        return capabilities.start_options.problem(options)


class ResolveTargetRequest(ContractModel):
    """Request on ``fleet.targets.resolve``: the mowers of a schedule target."""

    account_id: UUID
    target_type: Literal["mower", "group"]
    target_id: UUID


class TargetMower(ContractModel):
    """One mower of a resolved target with the place of its site."""

    mower_id: UUID
    name: str = Field(min_length=1, max_length=100)
    site_id: UUID
    timezone: str = Field(min_length=1, max_length=64, description="IANA name of the site.")
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    mow_pattern: MowPattern | None = Field(
        default=None,
        description="The mower's saved mowing defaults (`MowerOut.mow_defaults.pattern`); "
        "None when none are saved. A scheduled start begins with them and lays the keys the "
        "schedule sets (`SchedulePattern`) over them.",
    )


class ResolveTargetReply(ContractModel):
    """Reply to :class:`ResolveTargetRequest`.

    ``found`` is False when the target does not exist in the account. ``timezone`` is the
    target's own time zone: the mower's site, the group's site, or for a cross-site group the
    account's default site.
    """

    found: bool
    timezone: str | None = Field(default=None, max_length=64)
    mowers: list[TargetMower] = Field(default_factory=list)


class ScheduleRunOutcome(StrEnum):
    """What one run of a schedule did for one mower."""

    STARTED = "started"
    """Fleet accepted the start command."""
    SKIPPED_RAIN = "skipped_rain"
    """The rain pause held the run."""
    SKIPPED_PERMISSION = "skipped_permission"
    """The schedule's owner no longer has ``can_control`` on the mower."""
    SKIPPED_WINDOW = "skipped_window"
    """The window had closed before the run could start (for example after downtime)."""
    SKIPPED_MOWER_BUSY = "skipped_mower_busy"
    """The mower was already mowing, paused or returning."""
    SKIPPED_MANUAL = "skipped_manual"
    """A user skipped this run (``skip-next``)."""
    FAILED = "failed"
    """Fleet refused or could not be reached, or the mower has no areas to mow."""


class ScheduleRunRecorded(ContractModel):
    """Payload of ``scheduler.run.recorded``."""

    run_id: UUID
    schedule_id: UUID
    account_id: UUID
    mower_id: UUID | None = Field(
        default=None, description="None when the mowers of the target were not known."
    )
    trigger: Literal["schedule", "manual"]
    outcome: ScheduleRunOutcome
    details: str | None = Field(default=None, max_length=500)
    command_id: UUID | None = None
    scheduled_for: UtcDateTime
    started_at: UtcDateTime
    angle_deg: float | None = Field(
        default=None,
        ge=0,
        lt=180,
        description="For a started run: the mowing angle the start sent, 0 up to below 180 "
        "degrees (ADR 0061); None when the pattern has no direction or the run did not start.",
    )


class ScheduleDisabled(ContractModel):
    """Payload of ``scheduler.schedule.disabled``."""

    schedule_id: UUID
    account_id: UUID
    name: str = Field(max_length=100)
    reason: str = Field(max_length=500)
    owner: PrincipalRef
    disabled_at: UtcDateTime
