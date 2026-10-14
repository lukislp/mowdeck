/**
 * The setpoint limits of a mower model as the capabilities snapshot reports them. Nothing is
 * invented: when the snapshot carries no range, the helpers return `null` and the detail page
 * shows the controls as unavailable. Pure, unit tested (`limits.test.ts`).
 */
export type LimitCaps = {
  blade_height_range_mm?: number[] | null;
  min_speed_mps?: number | null;
  max_speed_mps?: number | null;
  model?: string | null;
};

export type Range = readonly [min: number, max: number];

/** Fallback minimum speed while the backend does not report one. */
export const FALLBACK_MIN_SPEED = 0.1;

function finite(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

/** The blade height range in mm, or `null` when the capabilities carry no valid range. */
export function bladeRange(caps: LimitCaps | null | undefined): Range | null {
  const range = caps?.blade_height_range_mm;
  if (!range || range.length !== 2) return null;
  const [min, max] = range;
  if (!finite(min) || !finite(max) || min > max) return null;
  return [min, max];
}

/** The speed range in m/s, or `null` when the maximum is unknown (the minimum falls back). */
export function speedRange(caps: LimitCaps | null | undefined): Range | null {
  const max = caps?.max_speed_mps;
  if (!finite(max)) return null;
  const reported = caps?.min_speed_mps;
  const min = finite(reported) ? reported : FALLBACK_MIN_SPEED;
  return [Math.min(min, max), max];
}

/** `value` limited to `range`; `null` stays `null`. */
export function clampTo(value: number | null | undefined, range: Range): number | null {
  if (!finite(value)) return null;
  return Math.min(Math.max(value, range[0]), range[1]);
}

/** The model to show: the capabilities' own name when reported, else Fleet's registered one. */
export function displayModel(
  caps: Pick<LimitCaps, 'model'> | null | undefined,
  mower: { model?: string | null } | null | undefined,
): string | null {
  return caps?.model?.trim() || mower?.model?.trim() || null;
}
