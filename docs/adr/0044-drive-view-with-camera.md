# ADR 0044: Drive view with camera

## Status

Accepted (2026-10-16); amends [ADR 0026](0026-manual-drive-channel.md) (the drive page) and [ADR 0032](0032-live-camera-over-agora.md) (the camera player is shared).

## Context

The drive page (ADR 0026) shows a small map, the session strip, the joystick, the speed limit, the blade switch and the emergency stop. Driving by the map alone is driving blind: the map knows where the mower is, not what is in front of it. The live camera (ADR 0032) exists on the mower page, but switching pages while driving ends the session, and two viewers of one mower cannot run at once, because a camera session carries a single viewer uid.

A camera picture brings its own risks. A stream over 4G freezes, and a frozen frame looks exactly like a live one: a driver who trusts it steers into whatever moved since. Phones are held sideways to drive, where a page that scrolls hides the stop. And the picture must not swallow touches meant for the stick floating over it.

## Decision

**One stage, shared.** The camera's picture becomes `CameraStage.svelte`: it owns the `CameraViewer` (one session per mounted stage), the tiles, the stereo guard and the freeze detection. `CameraPanel` wraps it with the picker, notes, diagnostics and options; the drive page uses it with `compact` (fills its box, minimal captions). Exactly one camera session per drive page visit: switching to the map or leaving the page unmounts the stage and leaves the channel. A viewer shared across pages was considered and left out: it would keep a session (and 4G minutes) alive on pages that do not show it.

**The view from the capabilities.** `driveView(caps, stored, cameraError)` in `lib/drive/layout.ts`: the map without a `camera` capability (or `none`) and after a camera error during the visit (with the note "Kamera nicht verfügbar – Karte"), otherwise the stored choice, the camera by default. A "Kamera | Karte" switch appears only with a camera capability. The view, the map thumbnail's visibility and the drive camera's layout (front left by default, "Both" on request) are remembered per browser, every storage access guarded.

**Three layouts.** Phones upright: the camera 16:9 on top, a one-line readout (state lamp, battery, speed, video latency), a 240 px stick, speed limit and the session Start/Stop, the session details folded; the stop bar fixed at the bottom. Phones held sideways (`(orientation: landscape) and (max-height: 520px)`): a full-bleed cockpit that covers the screen and never scrolls, the picture (or the map) edge to edge, a translucent overlay joystick for the right thumb (`Joystick` `overlay`, size through `--md-stick-size`), the emergency stop upright in a rail on the left with the session Start/Stop above it (`CommandBar`/`StopBar` `rail`). Desktop: the picture fills the centre, the map is a corner thumbnail, the console column stays on the right. The emergency stop and the session Stop are fully visible and reachable in all three; the picture's video and canvas never take pointer events. Overlays over video use theme-independent glass tokens (`--md-hud-*`).

**A frozen picture holds the mower.** The stage reads every shown tile's frame counter five times a second (`FreezeWatch`): no new frame for more than 1 s, or a picture that went away after it was live, is frozen. The picture turns grey under a hatched "Bild steht" plate, so it never looks live, and the stage reports `stalled`. The drive page then holds the pilot: one forced zero frame at once, then zero frames at the idle cadence for every stick sample, never with blades, and the blades are disarmed; the session stays open so driving can continue. When the picture runs again a stick nobody holds is free at once; a stick held through the freeze stays latched at zero until it is centred or let go and touched again, so the mower never jumps to a deflection made blind. A camera that is still connecting does not hold the mower (nothing is shown that could pass for live); the driver can switch to the map.

Unchanged: the dead-man timer of the Device-Gateway stays the only safety authority, and the session still ends on leaving the page, hiding the tab and losing the socket. Turning the phone lets go of a held stick (one zero frame) rather than carrying it over to the other layout. A lost drive link still ends a joystick session as `mower_refused`; the drive channel has no `link_lost` reason, so the end labels need none.

## Manufacturer independence

Generic, the same for every manufacturer: the `camera` capability as the only switch for the camera view, `manual_drive`, `drive_reverse` and `links` as before, `CameraStage`, the freeze detection on frame counters of the page's own `<video>` or canvas elements, the pilot's hold and latch, the three layouts, the HUD tokens, the overlay joystick and the stop rail. The web app reads no vendor field to decide anything here. The simulator's `fake` camera (a test pattern with a `data-freeze` hook) and its `camera` option (default on, off for a mower without a camera) carry the tests, which import no manufacturer package.

Adapter work, once per manufacturer: none beyond the camera session of ADR 0032. A manufacturer whose player is not Agora adds a player that fills the tile's `<video>` element; the freeze detection then works unchanged.

## Consequences

Easier: driving with sight of what is in front of the mower, on the phone held sideways as on the desktop, with the stops always in reach. A stalled stream can no longer pass for a live one, and it stops the mower instead of letting it drive on blind.

Harder: the camera view costs video data (4G minutes) for as long as the drive page shows it; the map view and leaving the page end it. A flaky stream holds the mower often; the driver then switches to the map. The hold depends on the browser reporting decoded frames (`getVideoPlaybackQuality`), with the playback time as the fallback.

To check on hardware: the freeze detection and the latency readout with the LUBA 2 AWD X's Agora stream over 4G (how often a real stream stalls for more than a second), and the sideways layout on the owner's iPhone in the Capacitor shell (safe areas, the stop rail under the thumb).
