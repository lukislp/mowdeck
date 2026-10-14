/**
 * A rough estimate of how long a mow job takes, for the start sheet's rain warning. Pure and
 * unit tested (`duration.test.ts`). It is a guess and the UI says so ("geschätzt").
 */
export const MIN_JOB_MINUTES = 30;
export const MAX_JOB_MINUTES = 6 * 60;
/** Overhead for turns, borders and slow sections on top of the pure swath time. */
export const AREA_OVERHEAD = 1.3;
/** Metres a path point stands for when only the point count is known (rough). */
export const METRES_PER_POINT = 1;
/** Fallbacks when the mower reports no speed / cutting width. */
export const FALLBACK_SPEED_MPS = 0.5;
export const FALLBACK_CUTTING_WIDTH_M = 0.4;

export interface DurationInput {
  /** `points_total` of a computed path preview, or null/undefined without one. */
  pointsTotal?: number | null;
  /** Total size of the selected areas in square metres. */
  areaM2?: number | null;
  /** Cutting width in metres (null: fallback). */
  cuttingWidthM?: number | null;
  /** Mowing speed in m/s (null: fallback). */
  speedMps?: number | null;
}

const positive = (value: number | null | undefined): value is number =>
  typeof value === 'number' && Number.isFinite(value) && value > 0;

const clamp = (minutes: number) => Math.min(MAX_JOB_MINUTES, Math.max(MIN_JOB_MINUTES, minutes));

/**
 * Estimated minutes. With a path preview: `points_total * 1 m / speed`. Else
 * `area / (cutting width * speed) * 1.3`. Capped to 30 min - 6 h; null without either input.
 */
export function estimateMinutes(input: DurationInput): number | null {
  const speed = positive(input.speedMps) ? input.speedMps : FALLBACK_SPEED_MPS;
  if (positive(input.pointsTotal)) {
    return clamp((input.pointsTotal * METRES_PER_POINT) / speed / 60);
  }
  if (positive(input.areaM2)) {
    const width = positive(input.cuttingWidthM) ? input.cuttingWidthM : FALLBACK_CUTTING_WIDTH_M;
    return clamp(((input.areaM2 / (width * speed)) * AREA_OVERHEAD) / 60);
  }
  return null;
}

/** "1 h 20 min", "45 min", "2 h": rounded to 5 minutes. */
export function formatDuration(minutes: number): string {
  const total = Math.max(0, Math.round(minutes / 5) * 5);
  const h = Math.floor(total / 60);
  const min = total % 60;
  if (h === 0) return `${min} min`;
  return min === 0 ? `${h} h` : `${h} h ${min} min`;
}
