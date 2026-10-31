/**
 * Translations of enum values and problems, and locale aware formatting. Every enum maps through
 * one exhaustive switch, so a new contract value fails the type check until it is translated.
 * Units are not translated (mm, m/s, %); numbers use the active locale (decimal comma in German).
 */
import type { Activity, RtkFix } from '@mowdeck/ui';

import type { ApiProblem } from '#lib/api/problem.js';
import type { components } from '#lib/generated/telemetry.js';
import type { LightReason } from '#lib/mower/light.js';
import type { ObstacleDetection, PathOrder } from '#lib/mower/job-options.js';
import {
  angleApplies,
  minLaps,
  type MowPattern,
  type PatternName,
} from '#lib/mower/mow-pattern.js';
import { isOnline, linkSeenAt, oldValuesAge } from '#lib/mower/presence.js';
import { m } from '#lib/paraglide/messages.js';
import { planProblemText } from '#lib/plan/problems.js';
import { getLocale } from '#lib/paraglide/runtime.js';
import type { CommandKind, CommandOutcome } from '#lib/stores/commands.svelte.js';

/** Why the light switch cannot be used (`lib/mower/light.ts`). */
export function lightReasonText(reason: LightReason): string {
  switch (reason) {
    case 'unsupported':
      return m.light_unsupported();
    case 'offline':
      return m.light_offline();
    case 'no_permission':
      return m.light_no_permission();
  }
}

export function activityLabel(activity: Activity): string {
  switch (activity) {
    case 'idle':
      return m.activity_idle();
    case 'mowing':
      return m.activity_mowing();
    case 'paused':
      return m.activity_paused();
    case 'returning':
      return m.activity_returning();
    case 'docked':
      return m.activity_docked();
    case 'charging':
      return m.activity_charging();
    case 'error':
      return m.activity_error();
    case 'offline':
      return m.activity_offline();
    case 'updating':
      return m.activity_updating();
    case 'unknown':
      return m.activity_unknown();
  }
}

export function rtkLabel(fix: RtkFix): string {
  switch (fix) {
    case 'fixed':
      return m.rtk_fixed();
    case 'float':
      return m.rtk_float();
    case 'none':
      return m.rtk_none();
    case 'unknown':
      return m.rtk_unknown();
  }
}

export type RtkSource = 'base_station' | 'network' | 'unknown';

export function rtkSourceLabel(source: RtkSource): string {
  switch (source) {
    case 'base_station':
      return m.rtk_source_base_station();
    case 'network':
      return m.rtk_source_network();
    case 'unknown':
      return m.rtk_source_unknown();
  }
}

/** The fix quality alone ("Fix"), for a readout that names the source next to it. */
export function rtkFixShort(fix: RtkFix): string {
  switch (fix) {
    case 'fixed':
      return m.map_rtk_fixed();
    case 'float':
      return m.map_rtk_float();
    case 'none':
      return m.map_rtk_none();
    case 'unknown':
      return m.map_rtk_unknown();
  }
}

/** The correction source in one word ("Netz", "Basis"); undefined when unknown. */
export function rtkSourceShort(source: RtkSource): string | undefined {
  switch (source) {
    case 'base_station':
      return m.rtk_source_short_base_station();
    case 'network':
      return m.rtk_source_short_network();
    case 'unknown':
      return undefined;
  }
}

export type ConnectionKind = 'ble_bridge' | 'wifi' | 'cloud' | 'simulated';

export function connectionLabel(kind: string): string {
  switch (kind as ConnectionKind) {
    case 'ble_bridge':
      return m.connection_ble_bridge();
    case 'wifi':
      return m.connection_wifi();
    case 'cloud':
      return m.connection_cloud();
    case 'simulated':
      return m.connection_simulated();
    default:
      return kind;
  }
}

export function patternLabel(name: PatternName): string {
  switch (name) {
    case 'stripes':
      return m.pattern_stripes();
    case 'diagonal':
      return m.pattern_diagonal();
    case 'checkerboard':
      return m.pattern_checkerboard();
    case 'edge_only':
      return m.pattern_edge_only();
    case 'random_angle':
      return m.pattern_random_angle();
  }
}

function lapsLabel(laps: number): string {
  if (laps <= 0) return m.start_laps_none();
  if (laps === 1) return m.start_laps_one();
  return m.start_laps_many({ count: laps });
}

/** The app's name of an obstacle detection mode (as in the Mammotion app). */
export function obstacleLabel(mode: ObstacleDetection): string {
  switch (mode) {
    case 'off':
      return m.start_obstacle_off();
    case 'slow_touch':
      return m.start_obstacle_slow_touch();
    case 'less_touch':
      return m.start_obstacle_less_touch();
    case 'standard':
      return m.start_obstacle_standard();
    case 'sensitive':
      return m.start_obstacle_sensitive();
  }
}

export function orderLabel(order: PathOrder): string {
  return order === 'border_first' ? m.start_order_border_first() : m.start_order_grid_first();
}

/** A whole percentage in the locale's style ("40 %" in German, "40%" in English). */
export function formatPercent(value: number): string {
  return new Intl.NumberFormat(getLocale(), { style: 'percent', maximumFractionDigits: 0 }).format(
    value / 100,
  );
}

/** "Streifen 45° · 1 Randrunde" (plus the spacing when one was chosen and "ab 40 %"). */
export function patternSummary(pattern: MowPattern): string {
  const head = angleApplies(pattern.name)
    ? `${patternLabel(pattern.name)} ${formatNumber(pattern.angle_deg)}°`
    : patternLabel(pattern.name);
  const parts = [head, lapsLabel(Math.max(pattern.border_laps, minLaps(pattern.name)))];
  if (pattern.spacing_m != null) {
    parts.push(m.start_summary_spacing({ cm: formatNumber(pattern.spacing_m * 100) }));
  }
  if (pattern.start_progress_pct) {
    parts.push(m.start_summary_progress({ percent: formatPercent(pattern.start_progress_pct) }));
  }
  return parts.join(' · ');
}

export function commandLabel(
  kind: CommandKind | 'manual_drive',
  endJob?: boolean | null,
  pattern?: MowPattern | null,
): string {
  switch (kind) {
    case 'start_mow':
      return pattern
        ? `${m.command_start_mow()} · ${patternSummary(pattern)}`
        : m.command_start_mow();
    case 'pause_mow':
      return m.command_pause_mow();
    case 'resume_mow':
      return m.command_resume_mow();
    case 'return_to_dock':
      return endJob ? m.command_return_to_dock_end() : m.command_return_to_dock();
    case 'emergency_stop':
      return m.command_emergency_stop();
    case 'set_blade_height':
      return m.command_set_blade_height();
    case 'set_speed':
      return m.command_set_speed();
    case 'set_light':
      return m.command_set_light();
    case 'firmware_update':
      return m.command_firmware_update();
    case 'spot_mow':
      return m.command_spot_mow();
    case 'spot_stop':
      return m.command_spot_stop();
    case 'drive_calibrate':
      return m.command_drive_calibrate();
    case 'set_drive_speed':
      return m.command_set_drive_speed();
    case 'set_setting':
      return m.command_set_setting();
    case 'restart_mower':
      return m.command_restart_mower();
    case 'sync_map':
      return m.command_sync_map();
    case 'relocate_dock':
      return m.command_relocate_dock();
    case 'leave_dock':
      return m.command_leave_dock();
    case 'resync_rtk':
      return m.command_resync_rtk();
    case 'manual_drive':
      return m.command_manual_drive();
  }
}

export function outcomeLabel(outcome: CommandOutcome): string {
  switch (outcome) {
    case 'pending':
      return m.result_pending();
    case 'accepted':
      return m.result_accepted();
    case 'executed':
      return m.result_executed();
    case 'rejected':
      return m.result_rejected();
    case 'failed':
      return m.result_failed();
    case 'expired':
      return m.result_expired();
  }
}

export function roleLabel(key: string, fallback: string): string {
  switch (key) {
    case 'owner':
      return m.role_owner();
    case 'admin':
      return m.role_admin();
    case 'manager':
      return m.role_manager();
    case 'operator':
      return m.role_operator();
    case 'viewer':
      return m.role_viewer();
    default:
      return fallback;
  }
}

/** The translated message of a problem, chosen by its type, then by its status. */
export function problemMessage(problem: ApiProblem): string {
  switch (problem.slug) {
    case 'not-found':
      return m.problem_not_found();
    case 'forbidden':
      return m.problem_forbidden();
    case 'unauthorized':
      return m.problem_unauthorized();
    case 'validation-failed':
    case 'bad-request':
      return m.problem_validation();
    case 'conflict':
      return m.problem_conflict();
    case 'unavailable':
      return m.problem_unavailable();
    case 'network':
      return m.problem_network();
    case 'plan-limit':
    case 'feature-not-in-plan':
      return planProblemText(problem)?.title ?? m.problem_generic();
    case 'demo-mode':
      return m.problem_demo_mode();
    default:
      break;
  }
  if (problem.status === 429) return m.problem_rate_limited();
  if (problem.status === 404) return m.problem_not_found();
  if (problem.status === 403) return m.problem_forbidden();
  if (problem.status === 422 || problem.status === 400) return m.problem_validation();
  if (problem.status === 503 || problem.status === 502) return m.problem_unavailable();
  return m.problem_generic();
}

export function formatNumber(value: number, digits = 0): string {
  return new Intl.NumberFormat(getLocale(), {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
}

export function formatDateTime(value: string | number | Date): string {
  return new Intl.DateTimeFormat(getLocale(), {
    dateStyle: 'short',
    timeStyle: 'medium',
  }).format(new Date(value));
}

export function formatDate(value: string | number | Date): string {
  return new Intl.DateTimeFormat(getLocale(), { dateStyle: 'medium' }).format(new Date(value));
}

export function formatTime(value: string | number | Date): string {
  return new Intl.DateTimeFormat(getLocale(), { timeStyle: 'medium' }).format(new Date(value));
}

/** The time of day as "HH:MM" (no seconds), e.g. the hour a rain warning names. */
export function formatClock(value: string | number | Date, locale: string = getLocale()): string {
  return new Intl.DateTimeFormat(locale, { hour: '2-digit', minute: '2-digit' }).format(
    new Date(value),
  );
}

/**
 * A moment close to now, short: the time of day when it falls on today (local time), else the
 * date and the time, so "Endet automatisch 23:59" never hides that it means tomorrow.
 */
export function formatMoment(
  value: string | number | Date,
  now: number | Date = Date.now(),
  locale: string = getLocale(),
): string {
  const at = new Date(value);
  const today = new Date(now);
  const sameDay =
    at.getFullYear() === today.getFullYear() &&
    at.getMonth() === today.getMonth() &&
    at.getDate() === today.getDate();
  return new Intl.DateTimeFormat(
    locale,
    sameDay ? { timeStyle: 'short' } : { dateStyle: 'short', timeStyle: 'short' },
  ).format(at);
}

/**
 * The link line of a mower (ADR 0040): "Verbunden" while the gateway holds a live connection,
 * else "Offline, zuletzt verbunden vor 3 min" from the last link heartbeat.
 */
export function presenceLabel(state: components['schemas']['MowerState'], now: number): string {
  return isOnline(state, now)
    ? m.presence_online()
    : m.presence_offline({ age: formatAge(linkSeenAt(state), now) });
}

/**
 * "Werte von vor 4 min" when an online mower's values are older than a few minutes (a docked
 * mower reports rarely); null otherwise. Shown apart from the link line, subdued.
 */
export function valuesAgeLabel(
  state: components['schemas']['MowerState'],
  now: number,
): string | null {
  return oldValuesAge(state, now) === null
    ? null
    : m.presence_values_age({ age: formatAge(state.observed_at, now) });
}

/** "vor 12 s" from an ISO time and the current time. */
export function formatAge(at: string | number, now: number): string {
  const seconds = Math.max(0, Math.round((now - new Date(at).getTime()) / 1000));
  if (seconds < 3) return m.age_now();
  if (seconds < 60) return m.age_seconds({ count: seconds });
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return m.age_minutes({ count: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return m.age_hours({ count: hours });
  return m.age_days({ count: Math.floor(hours / 24) });
}
