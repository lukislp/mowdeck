"""Maintenance commands (ADR 0048): one-shot device actions next to mowing and driving.

Each action is its own command kind, so Fleet can require its own right per kind:

* ``restart_mower``: reboot the mower (it is offline for a minute or two);
* ``sync_map``: read the mower's map again in full and publish it;
* ``relocate_dock``: make the mower forget the stored position of its charging station, so it
  learns it again (after the station was moved);
* ``leave_dock``: drive out of the charging station and stop a short way in front of it;
* ``resync_rtk``: read the RTK origin and the dock pose again, so the map is rebuilt with them.

A connection lists the actions it supports in ``MowerCapabilities.maintenance``; clients only
offer those. When each action is allowed is the controller's rule
(:func:`mowdeck_control.safety.maintenance_problem`), the same for every adapter.

Nothing here is specific to a manufacturer.
"""

from typing import Final, Literal

from mowdeck_contracts.v1._base import ContractModel

MaintenanceAction = Literal[
    "restart_mower", "sync_map", "relocate_dock", "leave_dock", "resync_rtk"
]
"""The kind of a maintenance command."""

MAINTENANCE_ACTIONS: Final[tuple[MaintenanceAction, ...]] = (
    "restart_mower",
    "sync_map",
    "relocate_dock",
    "leave_dock",
    "resync_rtk",
)
"""Every maintenance action, in a stable order."""


class RestartMower(ContractModel):
    """Restart the mower. Only at rest (docked, charging or idle), without an open drive
    session or a command waiting for its confirmation; the mower is offline while it boots."""

    kind: Literal["restart_mower"] = "restart_mower"


class SyncMap(ContractModel):
    """Read the mower's map again in full (areas, zones, paths, dock) and publish it. No motion;
    refused while a manual drive session is open (the transfer occupies the mower's command
    queue)."""

    kind: Literal["sync_map"] = "sync_map"


class RelocateDock(ContractModel):
    """Delete the stored position of the charging station, so the mower learns it again (after
    the station was moved). Only while the mower stands in its dock (docked or charging): out on
    the lawn it would lose its way home."""

    kind: Literal["relocate_dock"] = "relocate_dock"


class LeaveDock(ContractModel):
    """Drive out of the charging station and stop a short way in front of it (needs
    ``MowerCapabilities.undock``). A motion command: only from the dock, never during a sensor
    stop; it ends an open drive session like a job command."""

    kind: Literal["leave_dock"] = "leave_dock"


class ResyncRtk(ContractModel):
    """Read the RTK origin and the dock pose again; the map is rebuilt with what the mower
    answers. No motion; allowed in every activity except during a firmware update."""

    kind: Literal["resync_rtk"] = "resync_rtk"


MAINTENANCE_COMMAND_MODELS: Final[tuple[type[ContractModel], ...]] = (
    RestartMower,
    SyncMap,
    RelocateDock,
    LeaveDock,
    ResyncRtk,
)
"""The maintenance command models, in the order of :data:`MAINTENANCE_ACTIONS`."""


__all__ = [
    "MAINTENANCE_ACTIONS",
    "MAINTENANCE_COMMAND_MODELS",
    "LeaveDock",
    "MaintenanceAction",
    "RelocateDock",
    "RestartMower",
    "ResyncRtk",
    "SyncMap",
]
