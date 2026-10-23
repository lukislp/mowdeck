# ADR 0048: Maintenance commands

## Status

Accepted (2026-10-23)

## Context

The Mammotion Home Assistant integration offers a row of maintenance buttons next to its mowing controls: restart the mower, sync the map, resync the RTK and dock position, release the mower from its dock, relocate the charging station, cancel the task, and reset the blade time. Owners use them when the vendor app is not at hand: after the station was moved, after zones were edited in the vendor app, when the dock pose on the map is wrong, or when the mower hangs and needs a reboot.

MowDeck had none of them as commands. The adapter could already sync the map (on first use and after a job) and leave the dock (only as the first step of a spot run, ADR 0041), and it read the dock buffer on its own when the dock was unknown. Each of these actions changes or resets the device, some move it, and every command in MowDeck needs a right per kind, an audit record and safety rules that hold for every adapter.

## Decision

**One command kind per action.** `restart_mower`, `sync_map`, `relocate_dock`, `leave_dock` and `resync_rtk` are separate members of the `MowerCommand` union, each without parameters, instead of one `maintenance` command with an `action` field. Separate kinds give each its own right in Fleet's `COMMAND_RELATION`, its own label in clients and its own line in the command record, and they follow the pattern of every other command.

**Declared support.** `MowerCapabilities.maintenance` lists the kinds a connection can send (a sorted list in JSON, empty by default); clients offer only those. `leave_dock` is listed only together with `undock` (a model validator enforces it). Adapters implement the new optional protocol `MaintenanceCapable.run_maintenance(command)` for restart, map sync, dock relocation and RTK resync; `leave_dock` is the existing `UndockCapable.leave_dock()`.

**Rights and plan.** Restart, map sync, dock relocation and RTK resync change or reset the device itself, like a firmware update: they need `can_manage_mowers` (the `fleet.manage` permission). Leaving the dock is what a start does first: it needs `can_control`. All five need the plan feature `control` and are single-mower kinds (a bulk command answers 422): they are deliberate actions on one device. The Fleet command record of each command is its audit, as for every command kind.

**Safety rules** (`mowdeck_control.safety.maintenance_problem`, enforced by the controller for every adapter and applied once more by the adapters for direct callers). A running firmware update, spot run or drive calibration refuses them like every other command.

| Command | Allowed when | Why |
| --- | --- | --- |
| `restart_mower` | docked, charging or idle; no error; no open drive session; no command waiting for its confirmation; no map sync running | A reboot in the middle of a job, a return or a drive would leave the mower standing somewhere with its job half done; during an error the cause must be cleared at the mower. |
| `relocate_dock` | docked or charging; no job; no error; no open drive session; no pending confirmation | The mower forgets the stored station position and learns it again in the dock. Out on the lawn it would lose its way home. |
| `leave_dock` | docked or charging; no error; no pending confirmation | A motion command: like a job command it ends an open drive session first, and a sensor stop blocks it. |
| `sync_map` | any activity; no open drive session | No motion. The transfer occupies the mower's command queue for up to minutes, which must not starve a drive session. |
| `resync_rtk` | any activity | No motion, a read; it keeps an open drive session. |

The controller holds its queue only for the checks of `sync_map`; the transfer runs outside it (one at a time, a second one is refused), so later commands are neither delayed nor expired by it. Afterwards the controller reads the map again from the adapter, and the Device-Gateway publishes the map snapshot at once after a `sync_map`, `resync_rtk` or `relocate_dock` instead of waiting for its periodic recheck.

**Results.** State-changing actions without a reply (`restart_mower`, `relocate_dock`, `leave_dock`) are answered `accepted` once sent. `sync_map` is `executed` when the map arrived, `accepted` when the adapter's own timeout passed. `resync_rtk` is `executed` when a new RTK origin or dock pose arrived within the adapter's wait, `accepted` otherwise (an unchanged answer looks like none).

**Mammotion** (the GPL adapter, PyMammotion 0.10.9, the calls Mammotion-HA's buttons make): `remote_restart` (default `force_reset=1`, every model but the Luba 1, as Mammotion-HA offers it), `delete_charge_point` (`NavGetCommData(pver=1, action=6, type=5)`), `leave_dock` (`todev_one_touch_leave_pile`) and the dock buffer request `read_write_device(rw_id=5, context=1, rw=1)` are sent once with `Priority.USER`, never through `send_and_wait`; `sync_map` runs PyMammotion's map sync saga even when the held map is fresh. Every link can send them; a dual link sends them over the cloud.

**Left out.** Mammotion-HA's "cancel task" is `ReturnToDock(end_job=true)` and `PauseMow` in MowDeck already. Its blade time reset is a vendor service; MowDeck tracks blade hours itself in Telemetry, and resetting the vendor's counter is a separate decision. Its schedule sync and emergency nudges are not maintenance (schedules live in MowDeck's Scheduler; nudges are manual driving).

## Consequences

- Clients can offer the maintenance actions per mower from the capabilities; the web app shows their labels in the command history and no buttons yet.
- A manager (or the owner) can restart or reconfigure a mower; an operator can release it from its dock but not restart it.
- A restarted mower drops its connection for a minute or two; the Device-Gateway reconnects as after any lost link.
- The simulator implements all five (a restart keeps it `offline` for `restart_seconds`), so the rules run without hardware. Not verified on the real mower yet: that `delete_charge_point` makes the LUBA 2 learn the station again on its next docking, and how long a remote restart takes.
