"""Models of the telemetry API (requests are query parameters, except the blade reset)."""

import dataclasses
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from mowdeck_contracts.v1 import GeoReference, MowerCapabilities, MowerMap, MowerState, MowPath
from pydantic import BaseModel, ConfigDict, Field

from telemetry.application.queries import HeatmapResult
from telemetry.domain.health import BladeChange, DailyRow, HealthRow
from telemetry.domain.models import (
    CommandEvent,
    HistoryPoint,
    LatestRow,
    PositionRow,
    SnapshotRow,
)


class MowerStateSnapshot(BaseModel):
    """The latest state of a mower."""

    mower_id: UUID
    state: MowerState
    received_at: datetime = Field(description="When Telemetry stored this state (UTC).")
    stale: bool = Field(
        description="True when the mower is offline: the gateway reported its link lost "
        "(`state.connected` false) or the last link heartbeat (`state.heartbeat_at`, else "
        "`observed_at`) is older than the stale threshold. Old values of a connected mower are "
        "not stale (ADR 0040)."
    )

    @classmethod
    def of(cls, row: LatestRow, *, stale: bool) -> "MowerStateSnapshot":
        """Build from a stored row."""
        return cls(
            mower_id=row.mower_id,
            state=MowerState.model_validate(row.state),
            received_at=row.received_at,
            stale=stale,
        )


class MowerStateList(BaseModel):
    """Latest states of every mower the caller can view."""

    items: list[MowerStateSnapshot]
    truncated: bool = Field(description="True when more mowers exist than the configured limit.")


class HistoryPointOut(BaseModel):
    """One bin (or one row) of the history."""

    at: datetime
    activity: str
    battery_percent: int
    battery_min: int
    battery_max: int
    charging: bool
    rtk_fix: str
    error_code: str | None
    samples: int
    lat: float | None
    lon: float | None

    @classmethod
    def of(cls, point: HistoryPoint) -> "HistoryPointOut":
        """Build from a read model."""
        return cls(**dataclasses.asdict(point))


class StateHistory(BaseModel):
    """History of a mower in a time range."""

    mower_id: UUID
    start: datetime = Field(serialization_alias="from")
    end: datetime = Field(serialization_alias="to")
    step_seconds: float | None = Field(description="Bin width, null for raw rows.")
    points: list[HistoryPointOut]
    truncated: bool


class PositionOut(BaseModel):
    """One point of the position track."""

    observed_at: datetime
    lat: float
    lon: float
    alt: float | None
    heading: float | None
    rtk_fix: str
    rtk_source: str

    @classmethod
    def of(cls, row: PositionRow) -> "PositionOut":
        """Build from a stored row."""
        return cls(
            observed_at=row.observed_at,
            lat=row.lat,
            lon=row.lon,
            alt=row.alt,
            heading=row.heading,
            rtk_fix=row.rtk_fix,
            rtk_source=row.rtk_source,
        )


class PositionTrack(BaseModel):
    """Position track of a mower in a time range, oldest first."""

    mower_id: UUID
    start: datetime = Field(serialization_alias="from")
    end: datetime = Field(serialization_alias="to")
    positions: list[PositionOut]
    truncated: bool


class CommandEventOut(BaseModel):
    """One status transition of a command."""

    command_id: UUID
    status: str
    reason: str | None
    at: datetime

    @classmethod
    def of(cls, event: CommandEvent) -> "CommandEventOut":
        """Build from a stored row."""
        return cls(
            command_id=event.command_id, status=event.status, reason=event.reason, at=event.at
        )


class CommandTimeline(BaseModel):
    """The newest command transitions of a mower, newest first."""

    mower_id: UUID
    items: list[CommandEventOut]


class MowerMapOut(BaseModel):
    """The map of a mower as the Device-Gateway last read it (local metres plus reference)."""

    mower_id: UUID
    revision: str = Field(description="Map revision or content hash; also the ETag.")
    observed_at: datetime = Field(description="When the gateway read the map (UTC).")
    received_at: datetime = Field(description="When Telemetry stored it (UTC).")
    map: MowerMap

    @classmethod
    def of(cls, row: SnapshotRow) -> "MowerMapOut":
        """Build from a stored row."""
        return cls(
            mower_id=row.mower_id,
            revision=row.revision,
            observed_at=row.observed_at,
            received_at=row.received_at,
            map=MowerMap.model_validate(row.body),
        )


class MowerCapabilitiesOut(BaseModel):
    """What the mower can do over its current connection (blade height range, speed, ...)."""

    mower_id: UUID
    observed_at: datetime = Field(description="When the gateway read the capabilities (UTC).")
    received_at: datetime = Field(description="When Telemetry stored them (UTC).")
    capabilities: MowerCapabilities

    @classmethod
    def of(cls, row: SnapshotRow) -> "MowerCapabilitiesOut":
        """Build from a stored row."""
        return cls(
            mower_id=row.mower_id,
            observed_at=row.observed_at,
            received_at=row.received_at,
            capabilities=MowerCapabilities.model_validate(row.body),
        )


class MowPathOut(BaseModel):
    """The route the mower planned for its running job, with the progress along it."""

    mower_id: UUID
    observed_at: datetime = Field(description="When the gateway read the path (UTC).")
    received_at: datetime = Field(description="When Telemetry stored it (UTC).")
    path: MowPath

    @classmethod
    def of(cls, row: SnapshotRow) -> "MowPathOut":
        """Build from a stored row."""
        return cls(
            mower_id=row.mower_id,
            observed_at=row.observed_at,
            received_at=row.received_at,
            path=MowPath.model_validate(row.body),
        )


class MowerCapabilitiesList(BaseModel):
    """Stored capabilities of every mower the caller can view (those that reported any)."""

    items: list[MowerCapabilitiesOut]
    truncated: bool = Field(description="True when more mowers exist than the configured limit.")


def _hours(seconds: float) -> float:
    return round(seconds / 3600, 4)


def _optional_hours(seconds: float | None) -> float | None:
    return None if seconds is None else _hours(seconds)


class BladeThresholdIn(BaseModel):
    """``PUT .../health/blade-threshold``: the mower's blade reminder threshold."""

    model_config = ConfigDict(extra="forbid")

    hours: float | None = Field(
        gt=0,
        le=10_000,
        description="Blade hours after which MowDeck reminds of a blade change; null returns "
        "to the installation's default.",
    )


class MowerHealthOut(BaseModel):
    """Running health totals of a mower (since the first state Telemetry received)."""

    mower_id: UUID
    since: datetime | None = Field(
        description="observed_at of the first state counted; null after a blade reset before "
        "any telemetry."
    )
    blade_hours: float = Field(description="Hours with running blades (heuristic, see README).")
    blade_hours_since_change: float
    blade_changed_at: datetime | None = Field(description="The last recorded blade change.")
    blade_hours_threshold: float = Field(
        description="Hours after which a reminder is sent: the mower's own threshold set in "
        "MowDeck, else the installation's default."
    )
    blade_hours_threshold_custom: bool = Field(
        default=False,
        description="The mower has its own threshold (`PUT .../health/blade-threshold`); false: "
        "the default applies.",
    )
    device_blade_hours: float | None = Field(
        description="The mower's own blade counter in hours (reset in the vendor app), as the "
        "vendor app shows it; null when the mower does not report one.",
    )
    device_blade_warn_hours: float | None = Field(
        description="The mower's own blade warning threshold in hours; null when not reported.",
    )
    blade_source: Literal["device", "telemetry"] = Field(
        description="Which blade counter to show as hours since the blade change: the mower's "
        "own (device) or MowDeck's (telemetry, blade_hours_since_change).",
    )
    mowing_hours: float
    distance_m: float
    battery_cycles: int = Field(
        description="Battery charge cycles to show: the mower's own lifetime counter when it "
        "reports one (battery_cycles_source device), else MowDeck's count "
        "(battery_cycles_tracked).",
    )
    battery_cycles_tracked: int = Field(
        description="MowDeck's own count since the first state it received: discharges below "
        "20 % followed by a charge above 80 %.",
    )
    device_battery_cycles: int | None = Field(
        description="The mower's own lifetime battery cycle counter; null when the mower does "
        "not report one.",
    )
    battery_cycles_source: Literal["device", "telemetry"] = Field(
        description="Where battery_cycles comes from: the mower's own counter (device) or "
        "MowDeck's count (telemetry, battery_cycles_tracked).",
    )
    charge_sessions: int
    errors: dict[str, int] = Field(description="Error count per error code (unknown: none).")
    updated_at: datetime

    @classmethod
    def of(cls, row: HealthRow, *, threshold_hours: float) -> "MowerHealthOut":
        """Build from the stored totals; ``threshold_hours`` is the default threshold."""
        custom = row.blade_threshold_seconds is not None
        return cls(
            mower_id=row.mower_id,
            since=row.since,
            blade_hours=_hours(row.blade_seconds),
            blade_hours_since_change=_hours(row.blade_seconds_since_change),
            blade_changed_at=row.blade_changed_at,
            blade_hours_threshold=(
                _hours(row.blade_threshold_seconds)
                if row.blade_threshold_seconds is not None
                else threshold_hours
            ),
            blade_hours_threshold_custom=custom,
            device_blade_hours=_optional_hours(row.device_blade_seconds),
            device_blade_warn_hours=_optional_hours(row.device_blade_warn_seconds),
            blade_source="device" if row.device_blade_seconds is not None else "telemetry",
            mowing_hours=_hours(row.mowing_seconds),
            distance_m=round(row.distance_m, 2),
            battery_cycles=(
                row.device_battery_cycles
                if row.device_battery_cycles is not None
                else row.battery_cycles
            ),
            battery_cycles_tracked=row.battery_cycles,
            device_battery_cycles=row.device_battery_cycles,
            battery_cycles_source="device"
            if row.device_battery_cycles is not None
            else "telemetry",
            charge_sessions=row.charge_sessions,
            errors=dict(row.errors),
            updated_at=row.updated_at,
        )


class HealthDayOut(BaseModel):
    """Totals of one UTC day (battery_cycles is MowDeck's own count)."""

    day: date
    blade_hours: float
    mowing_hours: float
    distance_m: float
    battery_cycles: int
    charge_sessions: int
    errors: dict[str, int]
    jobs: int = Field(
        default=0, description="Jobs started: transitions into mowing (not from paused)."
    )
    online_hours: float = Field(
        default=0.0, description="Time with states arriving and the mower not offline."
    )

    @classmethod
    def of(cls, row: DailyRow) -> "HealthDayOut":
        """Build from a stored day row."""
        return cls(
            day=row.day,
            blade_hours=_hours(row.blade_seconds),
            mowing_hours=_hours(row.mowing_seconds),
            distance_m=round(row.distance_m, 2),
            battery_cycles=row.battery_cycles,
            charge_sessions=row.charge_sessions,
            errors=dict(row.errors),
            jobs=row.jobs,
            online_hours=_hours(row.online_seconds),
        )


class HealthDaily(BaseModel):
    """Day totals of an inclusive range of UTC days, oldest first (days without data absent)."""

    mower_id: UUID
    start: date = Field(serialization_alias="from")
    end: date = Field(serialization_alias="to")
    days: list[HealthDayOut]


class BatteryPointOut(BaseModel):
    """One bin (or one row) of the battery series."""

    at: datetime
    battery_percent: int
    battery_min: int
    battery_max: int
    charging: bool

    @classmethod
    def of(cls, point: HistoryPoint) -> "BatteryPointOut":
        """Project a history point."""
        return cls(
            at=point.at,
            battery_percent=point.battery_percent,
            battery_min=point.battery_min,
            battery_max=point.battery_max,
            charging=point.charging,
        )


class BatterySeries(BaseModel):
    """Battery percentage of a mower in a time range."""

    mower_id: UUID
    start: datetime = Field(serialization_alias="from")
    end: datetime = Field(serialization_alias="to")
    step_seconds: float | None = Field(description="Bin width, null for raw rows.")
    points: list[BatteryPointOut]
    truncated: bool


class BladeResetIn(BaseModel):
    """Body of the blade reset."""

    note: str | None = Field(default=None, max_length=500, description="Free text, optional.")


class BladeChangeOut(BaseModel):
    """A recorded blade change."""

    id: UUID
    changed_at: datetime
    note: str | None
    changed_by: UUID | None = Field(description="Subject of the principal who recorded it.")

    @classmethod
    def of(cls, change: BladeChange) -> "BladeChangeOut":
        """Build from a stored row."""
        return cls(
            id=change.id,
            changed_at=change.changed_at,
            note=change.note,
            changed_by=change.changed_by,
        )


class BladeChangeList(BaseModel):
    """Recorded blade changes of a mower, newest first."""

    mower_id: UUID
    items: list[BladeChangeOut]


class ReportMowerOut(BaseModel):
    """The day totals of one mower in a report (days without data are absent)."""

    mower_id: UUID
    days: list[HealthDayOut]


class ReportSummary(BaseModel):
    """``GET /reports/summary``: day totals of every visible mower with data in the range."""

    start: date = Field(serialization_alias="from")
    end: date = Field(serialization_alias="to")
    items: list[ReportMowerOut]
    truncated: bool = Field(description="More rows than one report returns; narrow the range.")

    @classmethod
    def of(cls, first: date, last: date, rows: list[DailyRow], truncated: bool) -> "ReportSummary":
        """Group day rows (ordered by mower and day) per mower."""
        grouped: dict[UUID, list[HealthDayOut]] = {}
        for row in rows:
            grouped.setdefault(row.mower_id, []).append(HealthDayOut.of(row))
        return cls(
            start=first,
            end=last,
            items=[ReportMowerOut(mower_id=k, days=v) for k, v in grouped.items()],
            truncated=truncated,
        )


class HeatmapCellOut(BaseModel):
    """One cell of a heatmap: integer indexes in the map's local frame (``floor(metres /
    cell_m)``), the value and the number of samples in it."""

    x: int
    y: int
    value: float = Field(description="Cut share or fixed share (0..1), or median dBm.")
    n: int = Field(description="Samples located in the cell.")


class HeatmapLegendOut(BaseModel):
    """How to colour the values: ``ratio`` and ``fix`` span 0..1, ``dbm`` the response's range
    (null without cells)."""

    kind: Literal["ratio", "fix", "dbm"]
    min: float | None
    max: float | None


class HeatmapGapOut(BaseModel):
    """A piece of the map's areas the blades missed (local metres, implicit closing edge)."""

    polygon: list[tuple[float, float]]
    area_m2: float


class MowerHeatmap(BaseModel):
    """A heatmap layer of a mower over its map's local frame."""

    mower_id: UUID
    layer: Literal["coverage", "rtk", "wifi", "mnet"]
    cell_m: float = Field(
        description="Cell size in metres; larger than requested when the cells were coarsened "
        "to stay within 20 000."
    )
    start: datetime = Field(serialization_alias="from")
    end: datetime = Field(serialization_alias="to")
    reference: GeoReference = Field(description="Geo reference of the map's local frame.")
    cells: list[HeatmapCellOut]
    legend: HeatmapLegendOut
    gaps: list[HeatmapGapOut] = Field(description="Missed map areas (coverage only).")
    samples: int = Field(description="Track rows that contributed to the layer.")
    truncated: bool = Field(description="True when the row limit cut off older positions.")

    @classmethod
    def of(cls, mower_id: UUID, result: HeatmapResult) -> "MowerHeatmap":
        """Build from the use case result."""
        heatmap = result.heatmap
        return cls(
            mower_id=mower_id,
            layer=heatmap.layer.value,
            cell_m=heatmap.cell_m,
            start=result.start,
            end=result.end,
            reference=result.mower_map.reference,
            cells=[HeatmapCellOut(x=c.x, y=c.y, value=c.value, n=c.n) for c in heatmap.cells],
            legend=HeatmapLegendOut(
                kind=heatmap.legend.kind.value, min=heatmap.legend.min, max=heatmap.legend.max
            ),
            gaps=[HeatmapGapOut(polygon=g.polygon, area_m2=g.area_m2) for g in heatmap.gaps],
            samples=heatmap.samples,
            truncated=result.truncated,
        )
