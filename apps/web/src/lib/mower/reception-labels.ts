/**
 * Words of the reception and health panel and of the error category (de and en through
 * Paraglide). Every enum is translated exhaustively; a self-check reason the app does not know
 * is shown readable with its vendor name.
 */
import { m } from '#lib/paraglide/messages.js';

import {
  readableKey,
  type ErrorCategory,
  type GnssSolution,
  type NetworkKind,
  type SelfCheck,
} from './reception';

export function gnssSolutionLabel(solution: GnssSolution): string {
  switch (solution) {
    case 'fixed':
      return m.rx_solution_fixed();
    case 'float':
      return m.rx_solution_float();
    case 'single':
      return m.rx_solution_single();
    case 'none':
      return m.rx_solution_none();
    case 'unknown':
      return m.rx_solution_unknown();
  }
}

export function networkLabel(network: NetworkKind): string {
  switch (network) {
    case 'wifi':
      return m.rx_network_wifi();
    case 'mobile':
      return m.rx_network_mobile();
    case 'ble':
      return m.rx_network_ble();
    case 'none':
      return m.rx_network_none();
  }
}

export function errorCategoryLabel(category: ErrorCategory): string {
  switch (category) {
    case 'trapped':
      return m.errcat_trapped();
    case 'obstacle':
      return m.errcat_obstacle();
    case 'outside':
      return m.errcat_outside();
    case 'wheel_slip':
      return m.errcat_wheel_slip();
    case 'lift_tilt':
      return m.errcat_lift_tilt();
    case 'collision':
      return m.errcat_collision();
    case 'battery':
      return m.errcat_battery();
    case 'blade_motor':
      return m.errcat_blade_motor();
    case 'rain':
      return m.errcat_rain();
    case 'positioning':
      return m.errcat_positioning();
    case 'communication':
      return m.errcat_communication();
    case 'docking':
      return m.errcat_docking();
    case 'firmware':
      return m.errcat_firmware();
    case 'other':
      return m.errcat_other();
  }
}

/** Self-check reasons the adapters name (`SelfCheck.detail`), with their words. */
const CHECK_REASONS: Record<string, () => string> = {
  emergency_stop: m.rx_check_emergency_stop,
  stop_button: m.rx_check_stop_button,
  safety_key_missing: m.rx_check_safety_key_missing,
  lifted: m.rx_check_lifted,
  tilted: m.rx_check_tilted,
  tipped_over: m.rx_check_tipped_over,
  bumper_missing: m.rx_check_bumper_missing,
  rain: m.rx_check_rain,
  low_battery: m.rx_check_low_battery,
  non_working_hours: m.rx_check_non_working_hours,
  rtk_station_moved: m.rx_check_rtk_station_moved,
  outside_task_area: m.rx_check_outside_task_area,
  wheel_motor_error: m.rx_check_wheel_motor_error,
  blade_motor_overheated: m.rx_check_blade_motor_overheated,
  dock_moved: m.rx_check_dock_moved,
  no_task_area: m.rx_check_no_task_area,
  night_protection: m.rx_check_night_protection,
  rain_protection: m.rx_check_rain_protection,
  rtk_not_ready: m.rx_check_rtk_not_ready,
  weak_satellite_signal: m.rx_check_weak_satellite_signal,
  positioning_failure: m.rx_check_positioning_failure,
  inavi_unavailable: m.rx_check_inavi_unavailable,
  weak_rtk_signal: m.rx_check_weak_rtk_signal,
};

/**
 * Why the self-check blocks a start, in words: a known reason translated, `other_<n>` (a code the
 * adapter has no name for) as "unknown reason (n)", any other text made readable; null for `ok`.
 */
export function selfCheckReason(check: SelfCheck): string | null {
  if (check.result === 'ok') return null;
  const detail = check.detail?.trim();
  if (!detail) return m.rx_check_blocked_unknown();
  const known = Object.hasOwn(CHECK_REASONS, detail) ? CHECK_REASONS[detail] : undefined;
  if (known) return known();
  const other = /^other_(.+)$/.exec(detail);
  if (other) return m.rx_check_other({ detail: other[1]! });
  return readableKey(detail);
}
