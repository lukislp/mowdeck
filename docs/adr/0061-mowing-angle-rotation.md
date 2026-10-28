# ADR 0061: Mowing angle rotation

## Status

Accepted (2026-10-28)

## Context

A mower that drives the same lines on every run presses permanent tracks into the lawn. The owner asked for the mowing direction to turn on every run ("Mähmuster-Wechsel: Winkel bei jedem Lauf drehen, damit keine Fahrspuren entstehen").

What exists: a schedule can carry a list of angles that its runs take round robin (`rotation_angles` with `rotation_index`, [ADR 0024](0024-scheduler-recurrence-rain-pause-and-command-issuing.md)). The index belongs to the schedule, so two schedules for the same lawn, a "run now" from another schedule or a manual start from the start sheet do not know about each other, and the user has to type the whole list.

What the mower supports (PyMammotion 0.10.9, the version the adapter pins): the route request `NavReqCoverPath` (`mctrl_nav.proto`) carries `toward` (int32, the path angle in degrees), `toward_mode` (`PathAngleSetting`: `relative_angle` 0, `absolute_angle` 1, `random_angle` 2) and `toward_included_angle` (the second pass of a double grid). `GenerateRouteInformation.toward` is documented as "is just angle"; `MammotionCommand.generate_route_information` copies it into `NavReqCoverPath.toward`. The adapter already maps `StartMow.pattern.angle_deg` to `toward = round(angle) % 180` with `toward_mode = absolute_angle` and `toward_included_angle = 90` (`mowdeck_control_mammotion.mapping.route_plan`; diagonal adds 45 degrees). So every start can already choose its angle; only the choice per run is missing. The vendor's own "Auto-reverse Mowing Direction" (`NavReqCoverPath.auto_change_direction`) only flips the direction, needs firmware 2.3.28.1 or later and is excluded for the Luba 1, the LUBA 2 and the original Yuka (`DeviceType.supports_auto_change_direction`), so it is no answer for the owner's LUBA 2 AWD X; MowDeck does not use it.

## Decision

**Step rotation, stored per mower and area.** A schedule gets `rotation_step_deg` (1 to 179 degrees, null: off). Its next run for one mower takes the last angle that mower mowed the run's areas with and turns it by the step, modulo 180 (a stripe at 190 degrees is the stripe at 10, and the adapter sends `toward` modulo 180 anyway). Without any history the run uses the base angle: the schedule's `pattern.angle_deg`, else the mower's mow defaults, else 0. Step rotation and the list exclude each other (422); the list keeps working as before.

The last angle lives in the Scheduler's new table `mow_angles` (mower, area, angle, time, source `schedule` or `manual`, schedule id), written by every started run, whatever its rotation, with the angle it sent. A job mows several areas with one angle: the run continues from the area mowed most recently (the newest direction on that lawn) and stores its angle for every area of the job. A group schedule works this out per mower. Patterns without a direction (`edge_only`, `random_angle`) never rotate and store nothing. The Scheduler owns this because it is the only place that issues starts on its own; Fleet stays a pass-through for commands.

**Manual starts.** A start from the start sheet does not pass the Scheduler. The sheet offers "Mähwinkel drehen" with the mower's step (`mower_rotation.manual_step_deg`, chips 30/45/60/90): it reads `GET /scheduler/mowers/{id}/angles`, sends the rotated angle in `StartMow.pattern.angle_deg` and, after the start went out, reports it with `POST /scheduler/mowers/{id}/angles`; a changed step is saved with `PUT .../angles/settings`. A manual start with rotation off (an explicit angle) is not reported, so it does not advance the rotation. The rotated angle never becomes the mow defaults; they keep the sheet's own angle. Reading needs `can_view`, reporting and the step need `can_control` on the mower.

**Where the angle shows.** `RunOut.angle_deg` and `ScheduleRunRecorded.angle_deg` (contracts, optional, 0 up to below 180) carry the angle of a started run; the run history prints it. `PreviewItemOut.angle_deg` gives the angle of each upcoming firing for the target's first mower (`angle_mower_id`), assuming every earlier firing starts; a skipped firing has none and does not advance. The schedule editor offers off / step / list with a dial of the next angles; the preview shows the angles beside the times.

Migration `0004` of the Scheduler adds `schedules.rotation_step_deg` and `schedule_runs.angle_deg` (both null for existing rows, so every schedule runs as before) and the tables `mow_angles` and `mower_rotation`.

Considered: a rotation index per mower instead of an angle (cannot continue across schedules with different steps); a new pattern key in `MowPattern` (would reach the controller and the adapter, which only need the final angle); rotating inside the Mammotion adapter (hidden from the preview and history, and the adapter would need state across starts); the vendor's auto-reverse (not offered on the LUBA 2 family).

## Consequences

- Rotation needs no adapter change: every adapter that honours `angle_deg` rotates. The Planner path ([ADR 0030](0030-mowing-patterns-and-planner.md)) gets the angle the same way.
- Not verified on hardware: whether the LUBA 2 AWD X firmware honours `toward` with `toward_mode = absolute_angle` (PyMammotion marks `toward_mode` "luba 2 yuka only"), and which reference 0 degrees has on the mower (the dial in the UI is schematic). The owner should start two jobs with 0 and 90 degrees and compare the planned paths in the Mammotion app or the path preview.
- Starts outside MowDeck (the Mammotion app, the vendor's own schedules, Home Assistant through another integration) are not seen and do not advance the rotation.
- `mow_angles` keeps rows of areas that were removed from the map; they are never read again for a job without that area.
