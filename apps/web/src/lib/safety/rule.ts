// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * Which user actions need the safety acknowledgement ("the area is clear of people, children and
 * animals, and I supervise the mower"): the one place that decides it. A feature asks
 * `safety.ask(kind)` (`gate.svelte.ts`) and never decides on its own; `SafetyCheck.svelte` words
 * it. A client safeguard only: the commands themselves are validated by the server as before
 * (ADR 0012).
 *
 * The table is exhaustive over `SafetyKind`, so a new command kind does not compile until
 * someone decides whether it starts blade movement or remote motion.
 */
import type { CommandKind } from '#lib/stores/commands.svelte.js';

/** What the user is about to do: a Fleet command kind, or a flow that is no single command. */
export type SafetyKind =
  | CommandKind
  /** A manual drive session (joystick over the drive link). */
  | 'drive_session'
  /** Creating an enabled schedule, or enabling one: the mower will start unattended. */
  | 'schedule_enable'
  /** "Run now" on a schedule: an immediate start. */
  | 'schedule_run_now';

/** The acknowledgement for a mower that starts now, or one that starts by itself later. */
export type SafetyVariant = 'now' | 'unattended';

const NEEDS: Record<SafetyKind, boolean> = {
  // Starts blade movement or remote motion.
  start_mow: true,
  resume_mow: true,
  spot_mow: true,
  leave_dock: true,
  drive_calibrate: true,
  drive_session: true,
  schedule_enable: true,
  schedule_run_now: true,
  // Stops, parks or does not move the mower; never delayed (the emergency stop above all).
  pause_mow: false,
  return_to_dock: false,
  emergency_stop: false,
  spot_stop: false,
  // Settings, reports and maintenance without motion.
  set_blade_height: false,
  set_speed: false,
  set_light: false,
  set_drive_speed: false,
  set_setting: false,
  firmware_update: false,
  restart_mower: false,
  sync_map: false,
  relocate_dock: false,
  resync_rtk: false,
};

/**
 * Whether `kind` needs the acknowledgement. `covered` is true when the flow's own, stricter
 * confirmation already says the same (the spot run with blades on: "nobody is near the spot");
 * then asking again would nag.
 */
export function needsSafetyAcknowledgement(kind: SafetyKind, covered = false): boolean {
  return NEEDS[kind] && !covered;
}

/** The wording variant: a schedule's mower starts without the user, everything else now. */
export function safetyVariant(kind: SafetyKind): SafetyVariant {
  return kind === 'schedule_enable' ? 'unattended' : 'now';
}
