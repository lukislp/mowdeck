"""Blade rules for manual driving, kept as pure functions so they are easy to test and reuse.

Rules (plan: "Risiken", "Sicherheit beim Fahren"):

* Blades are off unless a ``ManualDrive`` frame explicitly asks for them (``blade_on``).
* Blades are forced off when the mower does not allow blades while driving manually
  (``MowerCapabilities.manual_drive_blades``).
* Blades are forced off during transfer moves. Leaving the dock is a transfer, so a frame that
  arrives while the mower is docked or charging never starts the blades.
* Blades never run while a safety sensor stop is active.

Firmware updates (ADR 0038, :func:`firmware_update_problem`), the same for every adapter:

* only an update the manufacturer offers right now (``update_state`` ``available``) and only
  the offered version (a stale client that asks for another one is refused);
* the mower stands in its dock (activity ``docked`` or ``charging``) with at least
  :data:`FIRMWARE_MIN_BATTERY_PERCENT` battery; it need not be charging, since a mower stops
  charging once it is full;
* no job runs or is paused, no manual drive session is open, no update is already installing;
* while the mower reports ``updating`` every command but the emergency stop is refused
  (:data:`FIRMWARE_UPDATE_RUNNING`).

Maintenance commands (ADR 0048, :func:`maintenance_problem`), the same for every adapter; a
running firmware update, spot run or drive calibration refuses them like every other command:

* ``restart_mower``: only at rest (``docked``, ``charging`` or ``idle``; never while a job runs,
  is paused or returns, never during an error), without an open drive session or a command
  waiting for its confirmation;
* ``relocate_dock``: only in the dock (``docked`` or ``charging``; out on the lawn the mower would
  forget its way home), without an open drive session or a pending confirmation;
* ``leave_dock``: a motion command: only in the dock, never during an error, not while a command
  waits for its confirmation (the controller ends an open drive session first, like for a job
  command);
* ``sync_map``: no motion, any activity, but not during an open drive session (the transfer
  occupies the mower's command queue for up to minutes);
* ``resync_rtk``: no motion, a read; allowed in every activity.
"""

from dataclasses import dataclass
from typing import Final

from mowdeck_contracts.v1 import (
    MaintenanceAction,
    ManualDrive,
    MowerActivity,
    MowerCapabilities,
    MowerState,
)

_TRANSFER_ACTIVITIES = frozenset({MowerActivity.DOCKED, MowerActivity.CHARGING})
FIRMWARE_MIN_BATTERY_PERCENT: Final = 70
"""Lowest battery level at which a firmware update may start."""
FIRMWARE_UPDATE_RUNNING: Final = "firmware update running"
"""Rejection reason of every command but the emergency stop while the mower updates."""
_FIRMWARE_ACTIVITIES: Final = frozenset({MowerActivity.DOCKED, MowerActivity.CHARGING})


@dataclass(frozen=True, slots=True)
class BladeDecision:
    """Whether the blades may run for one drive frame, and why not if forced off."""

    blade_on: bool
    forced_off_reason: str | None = None


def decide_manual_blade(
    frame: ManualDrive, capabilities: MowerCapabilities, activity: MowerActivity
) -> BladeDecision:
    """Apply the blade rules to one ``ManualDrive`` frame."""
    if not frame.blade_on:
        return BladeDecision(blade_on=False)
    if activity is MowerActivity.ERROR:
        return BladeDecision(False, "blades forced off: a sensor stop is active")
    if not capabilities.manual_drive_blades:
        return BladeDecision(
            False, "blades forced off: this mower does not allow blades while driving manually"
        )
    if activity in _TRANSFER_ACTIVITIES:
        return BladeDecision(False, "blades forced off: leaving the dock is a transfer move")
    return BladeDecision(blade_on=True)


def firmware_update_problem(
    version: str,
    state: MowerState,
    capabilities: MowerCapabilities,
    *,
    drive_session_open: bool = False,
) -> str | None:
    """Why a firmware update to ``version`` must not start now, or None when it may.

    The checks run in the order a user can fix them: the connection's capability, the offer
    itself (state and version), the mower's situation (dock, battery), then what else
    is going on (job, drive session, an update already running).
    """
    if not capabilities.firmware_update:
        return "firmware updates are not supported on this connection"
    info = state.firmware_info
    if info is None:
        return "no firmware check yet; check for updates first"
    if info.update_state == "installing" or state.activity is MowerActivity.UPDATING:
        return "a firmware update is already installing"
    if info.update_state != "available" or not info.available:
        return "no firmware update is available"
    if version != info.available:
        return f"version {version} is not the offered one ({info.available}); check again"
    if state.activity not in _FIRMWARE_ACTIVITIES:
        return f"the mower must be docked (it is {state.activity.value})"
    if state.battery_percent < FIRMWARE_MIN_BATTERY_PERCENT:
        return (
            f"battery {state.battery_percent} % is below the "
            f"{FIRMWARE_MIN_BATTERY_PERCENT} % a firmware update needs"
        )
    if drive_session_open:
        return "a manual drive session is open; stop driving first"
    return None


IN_DOCK_ACTIVITIES: Final = frozenset({MowerActivity.DOCKED, MowerActivity.CHARGING})
"""Activities of a mower standing in its charging station."""
AT_REST_ACTIVITIES: Final = IN_DOCK_ACTIVITIES | {MowerActivity.IDLE}
"""Activities in which a restart is allowed: no job runs, is paused or returns."""
_SESSION_BLOCKED: Final = frozenset({"restart_mower", "relocate_dock", "sync_map"})
_CONFIRMATION_BLOCKED: Final = frozenset({"restart_mower", "relocate_dock", "leave_dock"})


def maintenance_problem(
    action: MaintenanceAction,
    state: MowerState,
    *,
    drive_session_open: bool = False,
    confirmation_pending: bool = False,
) -> str | None:
    """Why the maintenance command ``action`` must not run now, or None when it may (ADR 0048,
    module docstring). The capability check is the caller's; the firmware update, spot run and
    calibration lock-outs are the controller's for every command."""
    activity = state.activity
    if action in ("restart_mower", "relocate_dock", "leave_dock"):
        if activity is MowerActivity.ERROR:
            code = f" ({state.error_code})" if state.error_code else ""
            return f"the mower reports an error{code}; clear it at the mower first"
        if action == "restart_mower" and activity not in AT_REST_ACTIVITIES:
            return (
                f"the mower is {activity.value}; a restart needs it docked, charging or idle "
                "(end the job first)"
            )
        if action != "restart_mower" and activity not in IN_DOCK_ACTIVITIES:
            return f"the mower is {activity.value}, not in its dock"
    if action == "relocate_dock" and state.job is not None:
        return "a job is still active; end it first"
    if drive_session_open and action in _SESSION_BLOCKED:
        return "a manual drive session is open; stop driving first"
    if confirmation_pending and action in _CONFIRMATION_BLOCKED:
        return "a command is still waiting for its confirmation"
    return None
