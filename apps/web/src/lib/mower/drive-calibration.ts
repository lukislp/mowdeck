/**
 * Pure rules of the drive calibration and the driving speed (ADR 0042), next to the bridge in
 * the mower settings: the start conditions as green and red rows, the progress and the measured
 * steps as table rows, the fit as facts, the Fleet body that stores a result, the reasons in
 * words, and the snapping of the driving-speed slider. The flow itself (start, stop, save,
 * remove) is `drive-calibration.svelte.ts`; both are unit tested in `drive-calibration.test.ts`.
 */
import type { components as Telemetry } from '#lib/generated/telemetry.js';
import { formatNumber } from '#lib/i18n.js';
import { m } from '#lib/paraglide/messages.js';

import {
  bridgeSignal,
  linkLostLabel,
  signalCondition,
  signalConditionHint,
  signalConditionText,
} from './drive-link';
import { isOnline } from './presence';
import { isSpotRunActive, type SpotRunStatus } from './spot-sheet';

export type DriveCalibration = Telemetry['schemas']['DriveCalibration'];
export type DriveCalibrationStatus = Telemetry['schemas']['DriveCalibrationStatus'];
export type DriveCalibrationResult = Telemetry['schemas']['DriveCalibrationResult'];
export type DriveCalibrationStep = Telemetry['schemas']['DriveCalibrationStep'];
export type DriveCalibrationState = DriveCalibrationStatus['state'];
export type DriveSpeedSetting = Telemetry['schemas']['DriveSpeedSetting'];

/** The battery must be above this for a calibration (like a spot run). */
export const CALIB_BATTERY_MIN_PERCENT = 20;
/** The state must be younger than this (like a spot run). */
export const CALIB_FRESH_MS = 60_000;
/** Two speed settings closer than this count as equal (the vendor reports rounded values). */
export const DRIVE_SPEED_EPSILON_MPS = 0.005;

// --- the run -----------------------------------------------------------------------------------

const FINAL_STATES: ReadonlySet<string> = new Set(['done', 'aborted', 'failed']);

/** True while the gateway drives the calibration (preparing, measuring, stopping). */
export function isCalibrationActive(status: DriveCalibrationStatus | null | undefined): boolean {
  return !!status && !FINAL_STATES.has(status.state);
}

export function isCalibrationFinal(status: DriveCalibrationStatus | null | undefined): boolean {
  return !!status && FINAL_STATES.has(status.state);
}

export function calibrationStateLabel(state: DriveCalibrationState): string {
  switch (state) {
    case 'preparing':
      return m.calib_state_preparing();
    case 'measuring':
      return m.calib_state_measuring();
    case 'stopping':
      return m.calib_state_stopping();
    case 'done':
      return m.calib_state_done();
    case 'aborted':
      return m.calib_state_aborted();
    case 'failed':
      return m.calib_state_failed();
  }
}

function reasonCodeLabel(code: string, bridge: boolean): string | null {
  switch (code) {
    case 'stopped_by_user':
      return m.calib_reason_stopped_by_user();
    case 'emergency_stop':
      return m.calib_reason_emergency_stop();
    case 'sensor_stop':
      return m.calib_reason_sensor_stop();
    case 'geofence':
      return m.calib_reason_geofence();
    case 'rtk_lost':
      return m.calib_reason_rtk_lost();
    case 'activity_change':
      return m.calib_reason_activity_change();
    case 'link_lost':
      return linkLostLabel(bridge, m.calib_reason_link_lost);
    case 'frame_rejected':
      return m.calib_reason_frame_rejected();
    case 'no_fit':
      return m.calib_reason_no_fit();
    case 'turn_direction':
      return m.calib_reason_turn_direction();
    default:
      return null;
  }
}

/**
 * Why a calibration ended, in words. The gateway sends `code` or `code: detail`; a known code is
 * translated and its detail kept in brackets, an unknown reason is shown as it came. `bridge`:
 * the mower drives over the Bluetooth bridge, so a lost drive link names it.
 */
export function calibrationReasonLabel(
  reason: string | null | undefined,
  { bridge = false }: { bridge?: boolean } = {},
): string | null {
  const text = reason?.trim() ?? '';
  if (text === '') return null;
  const colon = text.indexOf(': ');
  const code = colon < 0 ? text : text.slice(0, colon);
  const detail = colon < 0 ? '' : text.slice(colon + 2).trim();
  const label = reasonCodeLabel(code, bridge);
  if (label === null) return text;
  return detail ? `${label} (${detail})` : label;
}

/**
 * The gateway's default plan (`CalibrationConfig`): forward at these `linear` values, then turns
 * in place at these `angular` values (left positive). The current step is read from it.
 */
export const CALIB_FORWARD_COMMANDS: readonly number[] = [0.3, 0.5, 0.7, 1.0];
export const CALIB_TURN_COMMANDS: readonly number[] = [0.3, -0.3, 0.6, -0.6, 1.0, -1.0];

/** The step being driven, when the run follows the default plan; null otherwise or when over. */
export function currentCalibrationStep(
  status: DriveCalibrationStatus | null | undefined,
): { kind: DriveCalibrationStep['kind']; command: number } | null {
  if (!status || !isCalibrationActive(status) || status.state === 'preparing') return null;
  const plan = [
    ...CALIB_FORWARD_COMMANDS.map((command) => ({ kind: 'forward' as const, command })),
    ...CALIB_TURN_COMMANDS.map((command) => ({ kind: 'turn' as const, command })),
  ];
  if (status.steps_total !== plan.length) return null;
  return plan[status.step_index] ?? null;
}

export function stepKindLabel(kind: DriveCalibrationStep['kind']): string {
  return kind === 'forward' ? m.calib_kind_forward() : m.calib_kind_turn();
}

/** "Schritt 3 von 8", the step being driven counted from 1 and never past the total. */
export function calibrationProgress(status: DriveCalibrationStatus): {
  i: number;
  n: number;
  percent: number;
} {
  const n = Math.max(status.steps_total, 0);
  const done = isCalibrationFinal(status) ? n : Math.min(status.steps?.length ?? 0, n);
  const i = n === 0 ? 0 : Math.min(status.step_index + 1, n);
  return { i, n, percent: n === 0 ? 0 : Math.round((done / n) * 100) };
}

// --- formatting ----------------------------------------------------------------------------------

/** A stick value with its sign ("+0,50", "−1,00"); turns are left positive. */
export function formatCommand(value: number): string {
  const text = formatNumber(Math.abs(value), 2);
  if (value > 0) return `+${text}`;
  if (value < 0) return `−${text}`;
  return text;
}

export function formatSpeedMps(value: number, digits = 2): string {
  return `${formatNumber(value, digits)} m/s`;
}

export function formatYawRate(value: number): string {
  return `${formatNumber(value, 1)} °/s`;
}

/** What a step measured with its unit, or "zu wenige Meldungen". */
export function formatMeasured(step: Pick<DriveCalibrationStep, 'kind' | 'measured'>): string {
  if (step.measured == null) return m.calib_measured_none();
  return step.kind === 'forward' ? formatSpeedMps(step.measured, 3) : formatYawRate(step.measured);
}

export type StepOutcome = DriveCalibrationStep['outcome'];

/**
 * What became of a step. States from before the outcome existed carry only `skipped`, and a
 * missing value then means too few reports.
 */
export function stepOutcome(
  step: Pick<DriveCalibrationStep, 'skipped' | 'measured'> & { outcome?: StepOutcome | null },
): StepOutcome {
  if (step.outcome) return step.outcome;
  if (step.skipped) return 'skipped';
  return step.measured == null ? 'invalid' : 'measured';
}

/** The marker of a step the fit does not use, in words; empty for a measured step. */
export function stepOutcomeLabel(outcome: StepOutcome): string {
  switch (outcome) {
    case 'measured':
      return '';
    case 'skipped':
      return m.calib_skipped();
    case 'invalid':
      return m.calib_outcome_invalid();
    case 'inconsistent':
      return m.calib_outcome_inconsistent();
    case 'outlier':
      return m.calib_outcome_outlier();
  }
}

export interface StepRow {
  /** 1-based. */
  index: number;
  kind: DriveCalibrationStep['kind'];
  kindLabel: string;
  command: string;
  measured: string;
  /** No value: the step was skipped or had too few reports (never shown as 0). */
  missing: boolean;
  reports: string;
  interval: string;
  outcome: StepOutcome;
  /** The marker for the status column; empty for a measured step. */
  outcomeLabel: string;
  /** Not driven or left out of the fit (skipped, invalid, inconsistent, outlier). */
  dropped: boolean;
}

export function stepRows(steps: readonly DriveCalibrationStep[] | null | undefined): StepRow[] {
  return (steps ?? []).map((step, i) => {
    const outcome = stepOutcome(step);
    const missing = outcome === 'skipped' || outcome === 'invalid' || step.measured == null;
    return {
      index: i + 1,
      kind: step.kind,
      kindLabel: stepKindLabel(step.kind),
      command: formatCommand(step.command),
      measured: missing ? '–' : formatMeasured(step),
      missing,
      reports: formatNumber(step.reports),
      interval:
        step.report_interval_s == null ? '–' : `${formatNumber(step.report_interval_s, 2)} s`,
      outcome,
      outcomeLabel: stepOutcomeLabel(outcome),
      dropped: outcome !== 'measured',
    };
  });
}

export type FitFactId =
  'full_speed' | 'full_yaw' | 'dead_zone' | 'turn_dead_zone' | 'speed_setting' | 'forward';

export interface FitFact {
  id: FitFactId;
  label: string;
  value: string;
}

/** The fit as label and value; the distance only when the result carries it. */
export function fitFacts(calibration: DriveCalibration, forwardM?: number | null): FitFact[] {
  const facts: FitFact[] = [
    {
      id: 'full_speed',
      label: m.calib_fit_full_speed(),
      value: formatSpeedMps(calibration.full_speed_mps, 3),
    },
    {
      id: 'full_yaw',
      label: m.calib_fit_full_yaw(),
      value: formatYawRate(calibration.full_yaw_rate_dps),
    },
    {
      id: 'dead_zone',
      label: m.calib_fit_dead_zone(),
      value: formatNumber(calibration.dead_zone ?? 0, 2),
    },
    {
      id: 'turn_dead_zone',
      label: m.calib_fit_turn_dead_zone(),
      value: formatNumber(calibration.turn_dead_zone ?? 0, 2),
    },
    {
      id: 'speed_setting',
      label: m.calib_fit_speed_setting(),
      value:
        calibration.drive_speed_setting == null
          ? m.drive_speed_unknown()
          : formatSpeedMps(calibration.drive_speed_setting),
    },
  ];
  if (forwardM != null) {
    facts.push({
      id: 'forward',
      label: m.calib_fit_forward(),
      value: `${formatNumber(forwardM, 2)} m`,
    });
  }
  return facts;
}

// --- storing a result ------------------------------------------------------------------------------

/** The body of Fleet's `PUT /mowers/{id}/drive-calibration`. */
export interface DriveCalibrationIn {
  full_speed_mps: number;
  full_yaw_rate_dps: number;
  dead_zone: number;
  turn_dead_zone: number;
  drive_speed_setting: number | null;
  calibrated_at: string;
}

export function calibrationPayload(calibration: DriveCalibration): DriveCalibrationIn {
  return {
    full_speed_mps: calibration.full_speed_mps,
    full_yaw_rate_dps: calibration.full_yaw_rate_dps,
    dead_zone: calibration.dead_zone ?? 0,
    turn_dead_zone: calibration.turn_dead_zone ?? 0,
    drive_speed_setting: calibration.drive_speed_setting ?? null,
    calibrated_at: calibration.calibrated_at,
  };
}

/** The calibration Fleet stores for a mower (`MowerOut.drive_calibration`), or null. */
export function storedCalibrationOf(mower: unknown): DriveCalibration | null {
  if (!mower || typeof mower !== 'object') return null;
  const raw = (mower as { drive_calibration?: unknown }).drive_calibration;
  if (!raw || typeof raw !== 'object') return null;
  const c = raw as Partial<DriveCalibration>;
  if (
    typeof c.full_speed_mps !== 'number' ||
    typeof c.full_yaw_rate_dps !== 'number' ||
    typeof c.calibrated_at !== 'string'
  ) {
    return null;
  }
  return {
    full_speed_mps: c.full_speed_mps,
    full_yaw_rate_dps: c.full_yaw_rate_dps,
    dead_zone: typeof c.dead_zone === 'number' ? c.dead_zone : 0,
    turn_dead_zone: typeof c.turn_dead_zone === 'number' ? c.turn_dead_zone : 0,
    drive_speed_setting: typeof c.drive_speed_setting === 'number' ? c.drive_speed_setting : null,
    calibrated_at: c.calibrated_at,
  };
}

/** The result on screen is the stored one (same measurement time). */
export function isResultStored(
  result: DriveCalibration | null | undefined,
  stored: DriveCalibration | null | undefined,
): boolean {
  if (!result || !stored) return false;
  return Date.parse(result.calibrated_at) === Date.parse(stored.calibrated_at);
}

/**
 * The speed setting changed since the stored calibration was measured: a run then rescales the
 * calibrated speed proportionally, and recalibrating is recommended. False when either is unknown.
 */
export function speedSettingDiffers(
  current: number | null | undefined,
  stored: DriveCalibration | null | undefined,
): boolean {
  const at = stored?.drive_speed_setting;
  if (current == null || at == null) return false;
  return Math.abs(current - at) > DRIVE_SPEED_EPSILON_MPS;
}

// --- start conditions ------------------------------------------------------------------------------

export type CalibConditionId = 'idle' | 'rtk' | 'fresh' | 'battery' | 'manual' | 'signal' | 'free';

export interface CalibCondition {
  id: CalibConditionId;
  ok: boolean;
  /** Not known yet: shown neutral, never blocks (the bridge signal without a value). */
  neutral?: boolean;
  /** The bridge signal in dBm (`signal`). */
  rssi?: number | null;
}

/** The parts of a mower state the conditions read (`MowerState` of Telemetry). */
export interface CalibConditionState {
  connection: string;
  links?: Record<string, { connected: boolean; rssi?: number | null } | undefined> | null;
  activity: string;
  rtk_fix: string;
  position_source: string;
  battery_percent: number;
  observed_at: string;
  heartbeat_at?: string | null;
  connected?: boolean | null;
  spot_run?: SpotRunStatus | null;
  drive_calibration?: DriveCalibrationStatus | null;
}

export interface CalibConditionInput {
  state: CalibConditionState | null;
  /** `capabilities.manual_drive`. */
  manualDrive: boolean | null | undefined;
  /** Epoch ms of now. */
  now: number;
}

/**
 * The rows the browser can check. `idle` means standing on the lawn: docked and charging are
 * other activities, so the dock is ruled out by the same row.
 */
export function calibrationConditions(input: CalibConditionInput): CalibCondition[] {
  const s = input.state;
  const age = s ? input.now - Date.parse(s.observed_at) : Number.POSITIVE_INFINITY;
  const signal = bridgeSignal(s);
  return [
    { id: 'idle', ok: s?.activity === 'idle' },
    { id: 'rtk', ok: !!s && s.rtk_fix === 'fixed' && s.position_source === 'rtk' },
    {
      id: 'fresh',
      ok: !!s && isOnline(s, input.now) && Number.isFinite(age) && age <= CALIB_FRESH_MS,
    },
    { id: 'battery', ok: !!s && s.battery_percent > CALIB_BATTERY_MIN_PERCENT },
    { id: 'manual', ok: input.manualDrive === true },
    // Only when the bridge carries the driving; the gateway refuses below LINK_MIN_RSSI_DBM.
    ...(signal ? [signalCondition(signal)] : []),
    {
      id: 'free',
      ok: !!s && !isSpotRunActive(s.spot_run) && !isCalibrationActive(s.drive_calibration),
    },
  ];
}

export function calibConditionLabel(id: CalibConditionId): string {
  switch (id) {
    case 'idle':
      return m.calib_cond_idle();
    case 'rtk':
      return m.calib_cond_rtk();
    case 'fresh':
      return m.calib_cond_fresh();
    case 'battery':
      return m.calib_cond_battery();
    case 'manual':
      return m.calib_cond_manual();
    case 'signal':
      return signalConditionText({});
    case 'free':
      return m.calib_cond_free();
  }
}

/** The words of a condition row with its value (the bridge signal shows its dBm). */
export function calibConditionText(condition: CalibCondition): string {
  return condition.id === 'signal'
    ? signalConditionText(condition)
    : calibConditionLabel(condition.id);
}

/** The line under a row, or null: why the bridge signal is red or neutral. */
export function calibConditionHint(condition: CalibCondition): string | null {
  return condition.id === 'signal'
    ? signalConditionHint({ ...condition, id: 'signal', rssi: condition.rssi ?? null })
    : null;
}

/** Start only with every checkable row green, the free room confirmed and the permission. */
export function canStartCalibration(
  conditions: readonly CalibCondition[],
  roomConfirmed: boolean,
  allowed: boolean,
): boolean {
  return allowed && roomConfirmed && conditions.every((c) => c.ok);
}

// --- the driving speed -------------------------------------------------------------------------------

function decimals(step: number): number {
  if (!Number.isFinite(step) || step <= 0) return 2;
  const text = String(step);
  const dot = text.indexOf('.');
  return dot < 0 ? 0 : Math.min(text.length - dot - 1, 4);
}

/** A slider value on the setting's grid (from its minimum) within min..max. */
export function snapDriveSpeed(value: number, setting: DriveSpeedSetting): number {
  const { min_mps: min, max_mps: max } = setting;
  const step = setting.step_mps > 0 ? setting.step_mps : 0.1;
  const digits = Math.max(decimals(step), decimals(min));
  if (!Number.isFinite(value)) return min;
  const clamped = Math.min(max, Math.max(min, value));
  // The epsilon keeps a value on a half step from rounding down through float noise.
  const stepped = min + Math.round((clamped - min) / step + 1e-9) * step;
  const factor = 10 ** digits;
  return Math.round(Math.min(max, Math.max(min, stepped)) * factor) / factor;
}

/** The current speed in words: "0,40 m/s", or "unbekannt". */
export function formatDriveSpeed(
  value: number | null | undefined,
  setting?: DriveSpeedSetting | null,
): string {
  if (value == null || !Number.isFinite(value)) return m.drive_speed_unknown();
  return formatSpeedMps(value, Math.max(2, setting ? decimals(setting.step_mps) : 2));
}

/** The slider is locked while a spot run or a calibration drives the mower. */
export function driveSpeedLocked(
  state: Pick<CalibConditionState, 'spot_run' | 'drive_calibration'> | null,
): boolean {
  return (
    !!state && (isSpotRunActive(state.spot_run) || isCalibrationActive(state.drive_calibration))
  );
}

/** The command for a released slider, or null when nothing changes. */
export function driveSpeedCommand(
  value: number,
  current: number | null | undefined,
  setting: DriveSpeedSetting,
): { kind: 'set_drive_speed'; speed_mps: number } | null {
  const speed = snapDriveSpeed(value, setting);
  if (current != null && Math.abs(current - speed) <= DRIVE_SPEED_EPSILON_MPS) return null;
  return { kind: 'set_drive_speed', speed_mps: speed };
}
