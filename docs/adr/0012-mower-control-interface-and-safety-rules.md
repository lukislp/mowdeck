# ADR 0012: Mower control interface and safety rules

## Status

Accepted (2026-10-08)

## Context

The Device-Gateway is the only service that talks to mowers. The plan makes `MowerConnection` in the Apache-2.0 library `mowdeck-control` the extension point for mower models and links (simulated, BLE bridge, vendor cloud, later Wi-Fi and other vendors) and requires that the library is usable without the platform. [ADR 0011](0011-gpl-package-for-pymammotion-adapter.md) puts everything that imports PyMammotion into a separate GPL-3.0 package, so the interface must fit that adapter without importing it.

The spike (`docs/spikes/0001-ble-bridge-and-pymammotion.md`, sections 3.2 and 6) established what the real adapter can offer:

- Native mowing jobs only on existing zones (zone hashes) with pattern parameters (stripe angle, double grid, spacing, border laps, order). No known command uploads an arbitrary boundary or path.
- Manual driving over BLE is a stream of movement frames; over the cloud it is a `RemoteDriveSession` with a control token, one frame in flight and an idle timeout. Whether the mower stops by itself when frames stop is unknown (risk R7).
- The map comes as `(x, y)` metres in a device-local east-north frame, rotated by the RTK yaw, plus a transform to WGS84.
- RTK fix and correction source are reported; the camera is reachable only through the vendor cloud; capabilities differ per model and firmware.

The plan's reliability and safety rules apply to every link: commands carry a unique id (duplicates are detected), an expiry (a stale "start" is dropped) and the emergency stop has priority; manual driving needs a dead-man switch (stop after about one second without a signal), blades are off during transfer moves, and sensor stops are never bypassed. [ADR 0009](0009-command-and-event-envelopes.md) defines the envelopes these rules work on.

## Decision

**Interface.** `MowerConnection` is an async `typing.Protocol` with `kind`, `is_connected`, `connect()`, `disconnect()`, `capabilities()`, `get_state()`, `watch_state()` (push updates, current state first), `get_map()`, `execute(command)` for every command except `ManualDrive`, and `start_drive_session()`. Adapters answer `execute` with an `Outcome` (`executed`, `accepted`, `rejected`, `failed` plus reason) and raise `CommandRejected`, `SensorStop` or `NotConnected`. A registry maps `ConnectionKind` to factories; other packages register through the entry point group `mowdeck.connections` (the PyMammotion package registers `ble_bridge` and `cloud`, this package registers `simulated`).

**Manual driving is a `DriveSession` object** with `update(linear, angular, blade_on)`, `stop()` and `close()`, not a fire-and-forget command. The cloud link needs a token-holding session anyway (as PyMammotion's own design does), BLE benefits from an explicit start and end, and an explicit `stop()` lets the controller stop the mower even when the device would keep driving.

**The controller owns the rules.** `MowerController` (one per mower) wraps any connection and enforces, the same way for every adapter: rejection of commands for another mower; de-duplication by `command_id` in a bounded LRU (the first result is returned again, in-flight duplicates wait); `expired` results for envelopes past `expires_at`, checked again after queueing; `EmergencyStop` outside the queue, ending the drive session first and rejecting everything queued before it; serialization of all other commands; the dead-man timer per `ManualDrive` frame (`ttl_ms`), which calls `stop()` and `close()` when it fires; the blade rules (off unless requested, forced off when the capability forbids it, while leaving the dock and during a sensor stop); checks of blade height and speed against capabilities and of `StartMow` against the map and the native patterns; ending the drive session when a state reports a sensor stop. Clock and timers are injected, so the rules are tested without sleeping. Adapters stay thin translators.

**Capabilities are explicit data.** `MowerCapabilities` (in `mowdeck-contracts`) lists native patterns, manual driving, blades while driving, custom paths (False for LUBA today), camera access (`none`, `cloud`, `local`), RTK sources, blade height range, maximum speed and connection kinds. Defaults mean "not supported".

**Maps are local metres plus a geo reference.** `MowerMap` (in `mowdeck-contracts`) holds areas, no-go zones, obstacles, paths and the dock as points in local metres and a `GeoReference` (origin latitude and longitude, rotation). Conversion to WGS84 lives in `mowdeck_control.geo`.

**Native jobs first.** `plan_to_native_job(request, capabilities)` maps a neutral "mow these areas with this pattern" request to a `NativeJob` and raises `UnsupportedPattern` when the mower cannot run it. Own routes are only considered once an adapter reports `custom_paths`.

**A simulated mower ships with the library** (`SimulatedMower`, `ConnectionKind.SIMULATED`): deterministic, tick driven, with a default map, battery, RTK, error and lift sensor hooks, so Phase 1 needs no hardware.

Alternatives considered:

1. *Manual drive as plain `ManualDrive` commands passed to `execute`.* Rejected: it hides the session the cloud link needs, and the adapter would have to own the timer.
2. *Dead-man switch and de-duplication inside each adapter.* Rejected: every adapter would re-implement safety-critical logic, and a bug in one adapter would bypass the rules. Device side timeouts remain a second safety net, not the primary one.
3. *Rely on JetStream de-duplication only (ADR 0009).* Kept as the first line, but it only covers the stream's duplicate window and not redeliveries after a missing ack or callers outside the bus (scripts, tests).
4. *Infer features from the model name.* Rejected: capabilities differ per model, firmware and link (camera only over the cloud), and a name table goes stale.
5. *Maps in WGS84 only.* Rejected: planning, distances and polygon checks are simpler and exact in metres, and the mower itself works in its local frame; converting back would lose precision.
6. *An abstract base class instead of a Protocol.* Rejected: a Protocol lets the GPL package implement the interface without inheriting code, and test fakes need no base class.

## Consequences

- New adapters implement a small interface and get expiry, de-duplication, emergency stop priority, the dead-man switch and the blade rules for free; the README of `mowdeck-control` has a checklist.
- The PyMammotion package (`mowdeck-control-mammotion`) fits without interface changes: `execute` maps to the `MctlNav` and `MctlDriver` builders, `DriveSession` maps to `send_movement` over BLE or `RemoteDriveSession` over the cloud, `get_map` converts `HashList` geometry, and `EmergencyStop` uses `Priority.EMERGENCY`.
- `mowdeck-contracts` gains `MowerCapabilities`, `CameraAccess`, `MowerMap`, `MapArea`, `MapZone`, `MapPath`, `DockPose` and `GeoReference` (backward compatible additions, JSON Schemas regenerated).
- The controller is per mower and in memory: de-duplication beyond its LRU and across Device-Gateway restarts relies on JetStream and on idempotent commands.
- The simulator is a model, not a digital twin: its path planner is a simple stripe pattern and does not simulate border laps. Real coverage planning belongs to `mowdeck-patterns`.
- Spike step 6 still has to show how the real mower reacts when frames stop; the controller's explicit `stop()` covers the case where it keeps driving.

## Amendment (2026-10-14): return to dock with or without ending the job, and path previews

**Decision: `ReturnToDock` gains `end_job`.** `ReturnToDock{end_job: bool = false}` is one command with a flag, not a second command kind. With `end_job = false` the mower drives home and the job keeps whatever state the mower gives it (a LUBA continues a paused job, for example after charging); with `end_job = true` the adapter ends the job first, so nothing resumes later. The adapter owns the sequence: the Mammotion adapter sends `cancel_job`, waits a short, bounded time until the reported work mode has left working or paused (a missing change is not an error, the return follows anyway) and then `return_to_dock`; the simulator ends its job either way, because it cannot resume after a return, and `end_job` only words the outcome. The controller gets no new rule: `ReturnToDock` stays a job command of rule 9 (it supersedes a pending confirmation, is serialized in the queue and de-duplicated by `command_id`), and Fleet keeps it under `can_control` ([ADR 0016](0016-fleet-commands-and-authorization-per-command-kind.md)); `CommandView.end_job` lets clients show "returned, job ended". The web app asks while a job runs, is paused or in error whether the job is to be ended, otherwise it sends the plain return.

Alternatives: a new kind `cancel_job` sent before `return_to_dock` would make every client send two commands with a race between them (a resume or a schedule in between), double the audit, and move an adapter-specific wait into the clients. A separate `end_job` command without a return would leave the mower standing on the lawn. The flag keeps old messages valid (`false` is the previous behaviour; the JSON Schema is regenerated as an additive change).

**Decision: previews are a question to the mower, not a command.** An adapter that can let the mower plan the route of a start without starting implements `PathPreviewCapable` (`preview_path(area_ids, pattern) -> MowPath`); one that can report the route of the running job implements `MowPathSource` (the Device-Gateway publishes it as `device.mower.<id>.path`, cleared when the job ends). `MowerController.preview_path` is rule 10: it is refused while a job runs, while a start waits for its confirmation and while a drive session is open; its areas and pattern are checked exactly like a `StartMow` (map areas, `native_patterns`); it does not take the command queue, carries no envelope and is not de-duplicated, because the mower only plans and a start sent meanwhile simply plans the same route again. The Mammotion adapter plans with PyMammotion's `MowPathSaga` without `start_job` and fetches the line list; the simulator answers with its own stripe route. The relay from Fleet to the gateway, its timeouts and the HTTP contract are decided in the amendment of [ADR 0016](0016-fleet-commands-and-authorization-per-command-kind.md).

Reasoning: native jobs are planned by the mower (this ADR, "Native jobs first"), so the only truthful preview of a native start is the mower's own plan; a route computed by `mowdeck-patterns` would show a different path than the one the LUBA drives. Planning changes nothing on the mower, so the command rules (expiry, de-duplication, result events) would add weight without protecting anything; the start checks of rule 10 remain, because a preview that the start would refuse is misleading.

Consequences: `MowerCapabilities` is not extended; whether a preview is possible follows from the adapter's protocol (`MowerController.can_preview_path`). A preview occupies the mower's planner for seconds to about a minute over 4G; the controller refuses it during a job rather than queueing it. `mowdeck-contracts` gains `MowPath`, `MowerPathSnapshot`, `PathPreviewGatewayRequest` and `PathPreviewReply` (additive).
