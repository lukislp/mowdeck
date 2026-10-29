"""What one mower model can do over one connection.

Capabilities are explicit data, reported by the device adapter through
``MowerConnection.capabilities()``. Clients and the controller decide with them instead of
guessing from the model name: for example whether joystick driving is offered, which mowing
patterns run natively on the mower, and which blade heights are valid.
"""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, PlainSerializer, model_validator

from mowdeck_contracts.v1._base import ContractModel
from mowdeck_contracts.v1.calibration import DriveSpeedSetting
from mowdeck_contracts.v1.maintenance import MaintenanceAction
from mowdeck_contracts.v1.mower import (
    MAX_OBSTACLE_LAPS,
    MAX_START_PROGRESS_PCT,
    ConnectionKind,
    MowPattern,
    ObstacleDetection,
    PathOrder,
    RtkSource,
)
from mowdeck_contracts.v1.settings import SettingSpec


class CameraAccess(StrEnum):
    """How a camera stream can be reached, if there is one."""

    NONE = "none"
    """The mower has no camera, or the adapter cannot reach it."""
    CLOUD = "cloud"
    """Only through the vendor cloud (for example Agora for Mammotion)."""
    LOCAL = "local"
    """Directly on the local network."""


def _sorted_strings(values: frozenset[str]) -> list[str]:
    return sorted(values)


def _sorted_sources(values: frozenset[RtkSource]) -> list[RtkSource]:
    return sorted(values)


def _sorted_kinds(values: frozenset[ConnectionKind]) -> list[ConnectionKind]:
    return sorted(values)


PatternNames = Annotated[
    frozenset[str], PlainSerializer(_sorted_strings, return_type=list[str], when_used="always")
]
"""A set of pattern names, serialised as a sorted list so dumps are deterministic."""

RtkSources = Annotated[
    frozenset[RtkSource],
    PlainSerializer(_sorted_sources, return_type=list[RtkSource], when_used="always"),
]

ConnectionKinds = Annotated[
    frozenset[ConnectionKind],
    PlainSerializer(_sorted_kinds, return_type=list[ConnectionKind], when_used="always"),
]


def _sorted_actions(values: frozenset[MaintenanceAction]) -> list[MaintenanceAction]:
    return sorted(values)


MaintenanceActions = Annotated[
    frozenset[MaintenanceAction],
    PlainSerializer(_sorted_actions, return_type=list[MaintenanceAction], when_used="always"),
]
"""A set of maintenance actions, serialised as a sorted list."""


def _check_range(name: str, value: tuple[int, int] | None, high: int) -> None:
    if value is not None and not 0 <= value[0] <= value[1] <= high:
        msg = f"{name} must be (min, max) with 0 <= min <= max <= {high}"
        raise ValueError(msg)


class StartOptions(ContractModel):
    """The job options a native start may carry besides the pattern (``MowPattern``), as one
    mower declares them (ADR 0030 amendment of 2026-10-24).

    Everything defaults to "not offered": a client shows an option only when it is declared
    here, and the connection refuses a start that sets an option it does not declare. An option
    a start leaves out (None) is not sent, so the mower keeps its own default.
    """

    obstacle_detection: list[ObstacleDetection] = Field(
        default_factory=list,
        max_length=8,
        description="Obstacle detection modes the mower offers, in the order a client shows "
        "them (`MowPattern.obstacle_detection`); empty when it offers no choice.",
    )
    obstacle_laps_range: tuple[int, int] | None = Field(
        default=None,
        description="Inclusive (min, max) laps around each no-go zone "
        "(`MowPattern.obstacle_laps`), if settable.",
    )
    orders: list[PathOrder] = Field(
        default_factory=list,
        max_length=2,
        description="Path orders the mower offers (`MowPattern.order`); empty when it offers "
        "no choice.",
    )
    start_progress_range_pct: tuple[int, int] | None = Field(
        default=None,
        description="Inclusive (min, max) progress in percent a job may start at "
        "(`MowPattern.start_progress_pct`), if settable.",
    )

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        if len(set(self.obstacle_detection)) != len(self.obstacle_detection):
            msg = "obstacle_detection must not list a mode twice"
            raise ValueError(msg)
        if len(set(self.orders)) != len(self.orders):
            msg = "orders must not list an order twice"
            raise ValueError(msg)
        _check_range("obstacle_laps_range", self.obstacle_laps_range, MAX_OBSTACLE_LAPS)
        _check_range(
            "start_progress_range_pct", self.start_progress_range_pct, MAX_START_PROGRESS_PCT
        )
        return self

    def problem(self, pattern: MowPattern) -> str | None:
        """Why the job options of ``pattern`` do not fit this mower; None when they do (or
        when it sets none). A start progress of 0 is the beginning and always fits."""
        if pattern.obstacle_detection is not None and (
            pattern.obstacle_detection not in self.obstacle_detection
        ):
            return f"obstacle detection {pattern.obstacle_detection.value!r} is not offered"
        if pattern.obstacle_laps is not None:
            if self.obstacle_laps_range is None:
                return "laps around no-go zones are not settable on this mower"
            low, high = self.obstacle_laps_range
            if not low <= pattern.obstacle_laps <= high:
                return f"obstacle_laps must be between {low} and {high} on this mower"
        if pattern.order is not None and pattern.order not in self.orders:
            return f"path order {pattern.order.value!r} is not offered"
        if pattern.start_progress_pct:
            if self.start_progress_range_pct is None:
                return "a start progress is not settable on this mower"
            low, high = self.start_progress_range_pct
            if not low <= pattern.start_progress_pct <= high:
                return f"start_progress_pct must be between {low} and {high} on this mower"
        return None


START_PLAN_REF_PATTERN = r"^[a-z][a-z0-9_]{0,31}(:[A-Za-z0-9_.-]{1,64})?$"
"""A start plan reference: a lower case word, optionally ``:`` and an argument (a zone id)."""


class StartPlanKind(StrEnum):
    """What a start plan does, so a client can label it without knowing the vendor."""

    NOW = "now"
    """Mow now, wherever the mower itself decides (its whole garden)."""
    ZONE = "zone"
    """Mow one of the mower's own zones (a vendor zone or work area without map geometry)."""
    EDGE = "edge"
    """Cut along the boundary only."""
    SCHEDULE = "schedule"
    """Hand the mower back to its own schedule."""


class StartPlan(ContractModel):
    """One way to start a mower that has no map areas to choose (ADR 0066 amendment).

    A mower whose connection knows no area geometry (its vendor keeps the garden to itself)
    cannot be started by tapping areas. Its connection declares the starts it offers instead; a
    client sends ``StartMow {plan_ref: ref}`` for the one the user picks and labels it by
    ``kind`` (plus ``name`` for a zone).
    """

    ref: str = Field(
        pattern=START_PLAN_REF_PATTERN, description="Sent as `StartMow.plan_ref` to start this."
    )
    kind: StartPlanKind
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="The mower's own name of a zone, shown as is; null for the other kinds.",
    )


class MowerCapabilities(ContractModel):
    """Feature flags and limits of one mower behind one connection.

    All fields default to "not supported", so an adapter only declares what it really offers.
    """

    model: str | None = Field(default=None, max_length=64, description="Mower model, if known.")
    native_patterns: PatternNames = Field(
        default_factory=frozenset,
        description=(
            "Mowing pattern names the mower runs natively on its own map areas, for example "
            "`stripes`, `diagonal`, `checkerboard`, `edge_only`."
        ),
    )
    manual_drive: bool = Field(default=False, description="Joystick driving is supported.")
    manual_drive_blades: bool = Field(
        default=False,
        description="Blades may run during manual driving when a frame explicitly asks for it.",
    )
    drive_reverse: bool = Field(
        default=False,
        description="The drive link that carries the joystick frames can drive backwards "
        "(a negative `ManualDrive.linear`). Without it the web joystick offers no reverse and "
        "a spot run's `multi_point` turns fall back to `arc` (ADR 0043).",
    )
    undock: bool = Field(
        default=False,
        description="The connection can make the mower leave its charging station by itself "
        "(`leave_dock`), so a spot run may start from the dock (ADR 0041).",
    )
    drive_speed_setting: DriveSpeedSetting | None = Field(
        default=None,
        description="The vendor's speed setting for manual driving can be read "
        "(`MowerState.drive_speed_mps`) and changed (`set_drive_speed`) within this range; null "
        "when the connection cannot (ADR 0042).",
    )
    custom_paths: bool = Field(
        default=False,
        description="The mower can follow an arbitrary path or boundary.",
    )
    camera: CameraAccess = CameraAccess.NONE
    light: bool = Field(
        default=False,
        description="The mower's light (e.g. the camera's headlamp) can be switched with "
        "`set_light`.",
    )
    rtk_sources: RtkSources = Field(
        default_factory=frozenset, description="RTK correction sources the mower can report."
    )
    blade_height_range_mm: tuple[int, int] | None = Field(
        default=None, description="Inclusive (min, max) blade height in millimetres, if settable."
    )
    path_spacing_range_mm: tuple[int, int] | None = Field(
        default=None,
        description="Inclusive (min, max) distance between neighbouring mowing paths in "
        "millimetres (`MowPattern.spacing_m`), if the model has a settable spacing.",
    )
    cutting_width_mm: int | None = Field(
        default=None,
        gt=0,
        le=2000,
        description="Width of the cut swath in millimetres, if known (coverage heatmaps).",
    )
    min_speed_mps: float | None = Field(
        default=None,
        gt=0,
        le=5,
        description="Lowest settable driving speed while mowing, if the model has one.",
    )
    max_speed_mps: float | None = Field(
        default=None, gt=0, le=5, description="Highest settable driving speed while mowing."
    )
    connection_kinds: ConnectionKinds = Field(
        default_factory=frozenset, description="Connection kinds this mower can use."
    )
    links_supported: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Additional link kinds this connection can open next to its primary one "
        "(e.g. `ble_bridge`); empty when the mower has only its primary connection.",
    )
    firmware_update: bool = Field(
        default=False,
        description="The connection can ask the manufacturer's firmware service for updates "
        "and install them (`firmware_check`, `firmware_update`).",
    )
    settings: list[SettingSpec] = Field(
        default_factory=list,
        max_length=32,
        description="The mower's own settings this connection can read and change with "
        "`set_setting` (ADR 0046), each with its type and range or options; current values in "
        "`MowerState.settings`. Empty when the connection offers none.",
    )
    maintenance: MaintenanceActions = Field(
        default_factory=frozenset,
        description="Maintenance commands this connection can send (ADR 0048): `restart_mower`, "
        "`sync_map`, `relocate_dock`, `leave_dock` (only together with `undock`), "
        "`resync_rtk`. Clients offer only these; empty when the connection has none.",
    )
    base_station_firmware_update: bool = Field(
        default=False,
        description="The connection can ask the manufacturer's firmware service for the linked "
        "RTK base station and install its updates (`firmware_check` with target "
        "`base_station`, `base_station_firmware_update`; ADR 0047).",
    )
    start_options: StartOptions = Field(
        default_factory=StartOptions,
        description="Job options a native start may carry besides the pattern (obstacle "
        "detection, laps around no-go zones, path order, start progress; ADR 0030). Clients "
        "show only what is declared here.",
    )
    start_plans: list[StartPlan] = Field(
        default_factory=list,
        max_length=16,
        description="Starts the mower offers without map areas (ADR 0066 amendment), in the "
        "order a client lists them; each is sent as `StartMow.plan_ref`. Empty for mowers that "
        "start on their map areas.",
    )

    @model_validator(mode="after")
    def _check_ranges(self) -> Self:
        if "leave_dock" in self.maintenance and not self.undock:
            msg = "maintenance action leave_dock needs undock"
            raise ValueError(msg)
        if self.blade_height_range_mm is not None:
            low, high = self.blade_height_range_mm
            if not 0 <= low <= high:
                msg = "blade_height_range_mm must be (min, max) with 0 <= min <= max"
                raise ValueError(msg)
        if self.path_spacing_range_mm is not None:
            low, high = self.path_spacing_range_mm
            if not 0 < low <= high:
                msg = "path_spacing_range_mm must be (min, max) with 0 < min <= max"
                raise ValueError(msg)
        if (
            self.min_speed_mps is not None
            and self.max_speed_mps is not None
            and self.min_speed_mps > self.max_speed_mps
        ):
            msg = "min_speed_mps must not exceed max_speed_mps"
            raise ValueError(msg)
        keys = [spec.key for spec in self.settings]
        if len(set(keys)) != len(keys):
            msg = "settings must not declare a key twice"
            raise ValueError(msg)
        refs = [plan.ref for plan in self.start_plans]
        if len(set(refs)) != len(refs):
            msg = "start_plans must not declare a reference twice"
            raise ValueError(msg)
        return self

    def start_plan(self, ref: str) -> StartPlan | None:
        """The declared start plan ``ref``; None when the connection does not offer it."""
        return next((plan for plan in self.start_plans if plan.ref == ref), None)

    def setting(self, key: str) -> SettingSpec | None:
        """The declared setting ``key``; None when the connection does not offer it."""
        return next((spec for spec in self.settings if spec.key == key), None)

    def supports_pattern(self, name: str) -> bool:
        """Return True if ``name`` is a native pattern of this mower."""
        return name in self.native_patterns

    def blade_height_allowed(self, mm: int) -> bool:
        """Return True if ``mm`` lies within :attr:`blade_height_range_mm`.

        A mower without a settable blade height allows nothing.
        """
        if self.blade_height_range_mm is None:
            return False
        low, high = self.blade_height_range_mm
        return low <= mm <= high

    def speed_allowed(self, mps: float) -> bool:
        """Return True if ``mps`` lies within :attr:`min_speed_mps` (if set, else just above 0)
        and :attr:`max_speed_mps`.

        A mower without a settable speed allows nothing.
        """
        if self.max_speed_mps is None or not 0 < mps <= self.max_speed_mps:
            return False
        return self.min_speed_mps is None or mps >= self.min_speed_mps
