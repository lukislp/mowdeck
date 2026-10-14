/**
 * Blade height and speed in the start sheet: the values the sheet opens with (the mower's
 * reported setpoints, limited to the model's range) and which commands a start sends before
 * `start_mow` (only the values the user changed). Both are mower setpoints the mower keeps
 * itself, so they are not part of the mow defaults. Pure, unit tested (`setpoints.test.ts`).
 */
import { clampTo, type Range } from './limits';

/** Step of the blade height stepper (mm) and of the speed stepper (m/s). */
export const BLADE_STEP_MM = 5;
export const SPEED_STEP_MPS = 0.1;
const SPEED_EPSILON = 0.001;

/** The sheet's blade height (mm) and speed (m/s); null where the value or range is unknown. */
export interface SetpointDraft {
  bladeMm: number | null;
  speedMps: number | null;
}

export type SetpointCommand =
  { kind: 'set_blade_height'; mm: number } | { kind: 'set_speed'; mps: number };

function round(value: number, decimals: number): number {
  const factor = 10 ** decimals;
  return Math.round(value * factor) / factor;
}

/** The values the sheet opens with: the reported setpoints within the model's ranges. */
export function initialSetpoints(
  bladeMm: number | null | undefined,
  speedMps: number | null | undefined,
  blade: Range | null,
  speed: Range | null,
): SetpointDraft {
  const b = blade ? clampTo(bladeMm, blade) : null;
  const s = speed ? clampTo(speedMps, speed) : null;
  return { bladeMm: b === null ? null : Math.round(b), speedMps: s === null ? null : round(s, 2) };
}

/** One step of a stepper within ``range``; an unknown value starts at the range's minimum. */
export function stepSetpoint(
  value: number | null,
  direction: 1 | -1,
  step: number,
  range: Range,
  decimals = 0,
): number {
  if (value === null) return range[0];
  return round(Math.min(range[1], Math.max(range[0], value + direction * step)), decimals);
}

/** The setpoint commands a start sends first: only values that differ from the opening ones. */
export function setpointCommands(draft: SetpointDraft, initial: SetpointDraft): SetpointCommand[] {
  const commands: SetpointCommand[] = [];
  if (draft.bladeMm !== null && draft.bladeMm !== initial.bladeMm) {
    commands.push({ kind: 'set_blade_height', mm: draft.bladeMm });
  }
  if (
    draft.speedMps !== null &&
    (initial.speedMps === null || Math.abs(draft.speedMps - initial.speedMps) > SPEED_EPSILON)
  ) {
    commands.push({ kind: 'set_speed', mps: draft.speedMps });
  }
  return commands;
}
