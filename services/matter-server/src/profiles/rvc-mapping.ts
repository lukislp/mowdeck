/**
 * The Robotic Vacuum Cleaner mapping as plain data, without matter.js, so it is tested on its own
 * (`test/rvc-mapping.test.ts` also checks the numbers against matter.js' enums).
 *
 * | MowDeck activity | RVC Operational State          | RVC Run Mode |
 * | ---------------- | ------------------------------ | ------------ |
 * | mowing           | Running                        | Mowing       |
 * | paused           | Paused                         | Mowing       |
 * | returning        | SeekingCharger                 | Idle         |
 * | charging         | Charging                       | Idle         |
 * | docked, updating | Docked (Charging when charging)| Idle         |
 * | idle             | Stopped                        | Idle         |
 * | error            | Error (Stuck)                  | Idle         |
 * | offline, unknown | Error (UnableToStartOrResume)  | Idle         |
 */
import type { MowerSnapshot, RefuseReason } from '../model.ts';

/** Matter device type id of the Robotic Vacuum Cleaner. */
export const RVC_DEVICE_TYPE = 0x74;

/** RVC Operational State values (Matter 1.2 Application Clusters § 7.4). */
export const OpState = {
  Stopped: 0,
  Running: 1,
  Paused: 2,
  Error: 3,
  SeekingCharger: 64,
  Charging: 65,
  Docked: 66,
} as const;

/** RVC Operational State error ids. */
export const ErrorId = {
  NoError: 0,
  UnableToStartOrResume: 1,
  UnableToCompleteOperation: 2,
  CommandInvalidInState: 3,
  Stuck: 65,
} as const;

/** The two run modes: the spec requires one with the Idle tag and one with the Cleaning tag. */
export const RunMode = { Idle: 0, Mowing: 1 } as const;
export const RunModeTag = { Idle: 0x4000, Cleaning: 0x4001, Auto: 0 } as const;

/** Mode Base `ChangeToMode` status. */
export const ModeStatus = {
  Success: 0,
  UnsupportedMode: 1,
  GenericFailure: 2,
  InvalidInMode: 3,
} as const;

/** Power Source enums. */
export const ChargeLevel = { Ok: 0, Warning: 1, Critical: 2 } as const;
export const ChargeState = { Unknown: 0, IsCharging: 1, IsAtFullCharge: 2, IsNotCharging: 3 } as const;
export const SourceStatus = { Unspecified: 0, Active: 1 } as const;

/** Below these the battery shows as "warning" and "critical". */
export const BATTERY_WARNING = 20;
export const BATTERY_CRITICAL = 10;

export interface OperationalView {
  operationalState: number;
  errorStateId: number;
}

export function operationalView(snapshot: MowerSnapshot): OperationalView {
  const ok = (operationalState: number): OperationalView => ({
    operationalState,
    errorStateId: ErrorId.NoError,
  });
  switch (snapshot.activity) {
    case 'mowing':
      return ok(OpState.Running);
    case 'paused':
      return ok(OpState.Paused);
    case 'returning':
      return ok(OpState.SeekingCharger);
    case 'charging':
      return ok(OpState.Charging);
    case 'docked':
    case 'updating':
      return ok(snapshot.charging ? OpState.Charging : OpState.Docked);
    case 'idle':
      return ok(OpState.Stopped);
    case 'error':
      return { operationalState: OpState.Error, errorStateId: ErrorId.Stuck };
    case 'offline':
    case 'unknown':
      return { operationalState: OpState.Error, errorStateId: ErrorId.UnableToStartOrResume };
  }
}

export function runModeOf(snapshot: MowerSnapshot): number {
  return snapshot.activity === 'mowing' || snapshot.activity === 'paused'
    ? RunMode.Mowing
    : RunMode.Idle;
}

export interface BatteryView {
  /** Half percent (0..200) as Matter counts, null while unknown. */
  batPercentRemaining: number | null;
  batChargeLevel: number;
  batChargeState: number;
}

export function batteryView(snapshot: MowerSnapshot): BatteryView {
  const percent = snapshot.batteryPercent;
  const level =
    percent === null || percent >= BATTERY_WARNING
      ? ChargeLevel.Ok
      : percent >= BATTERY_CRITICAL
        ? ChargeLevel.Warning
        : ChargeLevel.Critical;
  let state: number = ChargeState.Unknown;
  if (snapshot.charging) state = ChargeState.IsCharging;
  else if (percent === 100 && (snapshot.activity === 'docked' || snapshot.activity === 'charging'))
    state = ChargeState.IsAtFullCharge;
  else if (percent !== null) state = ChargeState.IsNotCharging;
  return {
    batPercentRemaining: percent === null ? null : percent * 2,
    batChargeLevel: level,
    batChargeState: state,
  };
}

/** The Operational State error id a refused or failed command answers with. */
export function commandErrorId(reason: RefuseReason | 'failed'): number {
  switch (reason) {
    case 'invalid_in_state':
      return ErrorId.CommandInvalidInState;
    case 'failed':
      return ErrorId.UnableToCompleteOperation;
    default:
      return ErrorId.UnableToStartOrResume;
  }
}

/** The Run Mode status a refused or failed mode change answers with. */
export function modeChangeStatus(reason: RefuseReason | 'failed'): number {
  return reason === 'failed' ? ModeStatus.GenericFailure : ModeStatus.InvalidInMode;
}
