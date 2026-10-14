"""Rows and read models of telemetry (plain data, no I/O)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class LatestRow:
    """The newest known state of one mower (table ``mower_latest``).

    ``state`` is the full ``MowerState`` as JSON, served as is. The three trailing fields
    remember what was last written to history and to the position track, so the next state can
    be compared without reading those tables.
    """

    mower_id: UUID
    account_id: UUID
    observed_at: datetime
    received_at: datetime
    activity: str
    battery_percent: int
    state: dict[str, Any]
    history_written_at: datetime | None = None
    history_fingerprint: str | None = None
    position_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class HistoryRow:
    """One stored change of state (table ``mower_history``)."""

    account_id: UUID
    mower_id: UUID
    observed_at: datetime
    activity: str
    battery_percent: int
    charging: bool
    rtk_fix: str
    connection: str
    blade_height_mm: int | None = None
    speed_mps: float | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class PositionRow:
    """One point of the position track (table ``mower_positions``)."""

    account_id: UUID
    mower_id: UUID
    observed_at: datetime
    lat: float
    lon: float
    rtk_fix: str
    rtk_source: str
    alt: float | None = None
    heading: float | None = None
    wifi_rssi: int | None = None
    """Wi-Fi signal in dBm when the adapter reported one (``raw.wifi_rssi``), migration 0007."""
    mnet_rssi: int | None = None
    """Mobile network signal in dBm (``raw.mnet_rssi``), migration 0007."""
    blades_on: bool | None = None
    """Whether the blades ran (``raw.blade_on``, else activity ``mowing``); null in older rows."""


@dataclass(frozen=True, slots=True)
class CommandEvent:
    """One status transition of a command (table ``command_timeline``)."""

    account_id: UUID
    mower_id: UUID
    command_id: UUID
    status: str
    at: datetime
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    """Which account owns a mower (table ``mower_directory``)."""

    mower_id: UUID
    account_id: UUID
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class HistoryPoint:
    """One bin of the downsampled history (or one raw row when no step was asked for)."""

    at: datetime
    activity: str
    battery_percent: int
    battery_min: int
    battery_max: int
    charging: bool
    rtk_fix: str
    error_code: str | None
    samples: int
    lat: float | None = None
    lon: float | None = None


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    """The newest device snapshot of one mower and kind (table ``mower_snapshots``).

    ``kind`` is ``map``, ``capabilities`` or ``path``; ``body`` is the map, the capabilities or
    the mow path as JSON (``{}`` for a path the gateway cleared),
    served as is; ``revision`` is the map revision (or a content hash) and the HTTP ETag.
    """

    mower_id: UUID
    kind: str
    account_id: UUID
    observed_at: datetime
    received_at: datetime
    revision: str
    body: dict[str, Any]
