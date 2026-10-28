/**
 * Mowing angle rotation (ADR 0061), mirroring `scheduler.domain.rotation`: a new direction on
 * every run, so the lawn gets no permanent tracks. A schedule rotates by a step (each run turns
 * the mower's last angle on its areas) or by a list (round robin); the start sheet may rotate
 * manual starts by the mower's step and reports the angle it sent. Pure and unit tested
 * (`rotation.test.ts`).
 */
import type { components } from '#lib/generated/scheduler.js';

export type MowerAngles = components['schemas']['MowerAnglesOut'];
export type AreaAngle = components['schemas']['AreaAngleOut'];

/** How a schedule rotates its angle. */
export type RotationMode = 'off' | 'step' | 'list';

/** Angles repeat after half a turn: a stripe at 10° is the stripe at 190°. */
export const ANGLE_PERIOD = 180;
/** Smallest and largest step (0 and 180 would never change the angle). */
export const STEP_RANGE = [1, 179] as const;
/** The steps offered as chips; others can be typed in. */
export const STEP_PRESETS = [30, 45, 60, 90] as const;
/** The step a schedule starts with when rotation by step is switched on. */
export const DEFAULT_STEP = 45;

/** `angle` in `[0, 180)`, rounded to a tenth of a degree. */
export function normalised(angle: number): number {
  const value = Math.round((((angle % ANGLE_PERIOD) + ANGLE_PERIOD) % ANGLE_PERIOD) * 10) / 10;
  return value >= ANGLE_PERIOD ? 0 : value;
}

/** A step that changes the angle on every run. */
export function validStep(step: number | null | undefined): step is number {
  return (
    typeof step === 'number' &&
    Number.isFinite(step) &&
    step >= STEP_RANGE[0] &&
    step <= STEP_RANGE[1]
  );
}

/** The rotation of a stored schedule. */
export function rotationModeOf(schedule: {
  rotation_step_deg?: number | null;
  rotation_angles?: readonly number[];
}): RotationMode {
  if (validStep(schedule.rotation_step_deg)) return 'step';
  return schedule.rotation_angles?.length ? 'list' : 'off';
}

/** The angle after `last` turned by `step`; the base angle when nothing was mowed yet. */
export function stepped(last: number | null, step: number, base: number): number {
  return last === null ? normalised(base) : normalised(last + step);
}

/** The next `count` angles of step rotation, each run continuing from the one before. */
export function stepSequence(
  last: number | null,
  step: number,
  base: number,
  count: number,
): number[] {
  const result: number[] = [];
  let current = last;
  for (let i = 0; i < count; i++) {
    current = stepped(current, step, base);
    result.push(current);
  }
  return result;
}

/** The angle of the area of `areaIds` mowed most recently; null without any. */
export function lastAngle(
  areas: readonly Pick<AreaAngle, 'area_id' | 'angle_deg' | 'used_at'>[],
  areaIds: readonly string[],
): number | null {
  let newest: Pick<AreaAngle, 'angle_deg' | 'used_at'> | null = null;
  for (const area of areas) {
    if (!areaIds.includes(area.area_id)) continue;
    if (newest === null || Date.parse(area.used_at) > Date.parse(newest.used_at)) newest = area;
  }
  return newest?.angle_deg ?? null;
}

/**
 * The angle a manual start with rotation chosen sends: the last angle of the picked areas
 * turned by the step, else `base` (the sheet's angle). Null without a step or without areas.
 */
export function nextManualAngle(
  state: Pick<MowerAngles, 'areas'> | null,
  areaIds: readonly string[],
  step: number | null,
  base: number,
): number | null {
  if (!validStep(step) || areaIds.length === 0) return null;
  return stepped(lastAngle(state?.areas ?? [], areaIds), step, base);
}

/** An angle as the UI prints it: whole degrees, a tenth when needed. */
export function angleText(angle: number, format: (n: number, digits?: number) => string): string {
  return `${format(angle, angle % 1 ? 1 : 0)}°`;
}
