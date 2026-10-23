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
from mowdeck_contracts.v1.mower import ConnectionKind, RtkSource
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
        description="The mower can follow an arbitrary path or boundary (False for LUBA today).",
    )
    camera: CameraAccess = CameraAccess.NONE
    light: bool = Field(
        default=False,
        description="The mower's light (the camera's headlamp on Mammotion mowers) can be "
        "switched with `set_light`.",
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
        return self

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
