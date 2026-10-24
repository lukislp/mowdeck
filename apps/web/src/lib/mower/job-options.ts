/**
 * The job options of a native start besides the pattern (ADR 0030, amendment 2026-10-24):
 * obstacle detection, laps around no-go zones, path order and the start progress. A mower
 * declares what it offers in `capabilities.start_options`; the start sheet shows only that,
 * under "Mehr". An option left empty (null) is not sent, so the mower keeps its own setting.
 * Pure, unit tested (`job-options.test.ts`); labels live in `#lib/i18n.js`.
 */

/** Obstacle detection modes in the order of the contracts' `ObstacleDetection`. */
export const OBSTACLE_MODES = ['off', 'slow_touch', 'less_touch', 'standard', 'sensitive'] as const;
export type ObstacleDetection = (typeof OBSTACLE_MODES)[number];

export const PATH_ORDERS = ['border_first', 'grid_first'] as const;
export type PathOrder = (typeof PATH_ORDERS)[number];

export type OptionRange = readonly [min: number, max: number];

/** Steps of the start progress dial, in percent. */
export const PROGRESS_STEP = 5;

/** `MowerCapabilities.start_options` as the sheet uses it. */
export interface JobOptionsSpec {
  /** Offered modes, in the mower's order (empty: no choice). */
  detection: ObstacleDetection[];
  obstacleLaps: OptionRange | null;
  /** Offered orders (empty: no choice). */
  orders: PathOrder[];
  /** Start progress in percent; null: not settable. */
  progress: OptionRange | null;
}

/** What the sheet edits; null keeps the mower's own setting, progress 0 starts at the start. */
export interface JobOptionsDraft {
  detection: ObstacleDetection | null;
  obstacleLaps: number | null;
  order: PathOrder | null;
  progress: number;
}

/** The job option keys of `MowPattern` (contracts), as a start or the mow defaults send them. */
export interface JobOptionKeys {
  obstacle_detection?: ObstacleDetection | null;
  obstacle_laps?: number | null;
  order?: PathOrder | null;
  start_progress_pct?: number | null;
}

export const NO_OPTIONS: JobOptionsDraft = {
  detection: null,
  obstacleLaps: null,
  order: null,
  progress: 0,
};

function finite(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function range(value: unknown, cap: number): OptionRange | null {
  if (!Array.isArray(value) || value.length !== 2) return null;
  const [min, max] = value as unknown[];
  if (!finite(min) || !finite(max) || min < 0 || min > max || max > cap) return null;
  return [min, max];
}

function picks<T extends string>(value: unknown, known: readonly T[]): T[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<T>();
  for (const item of value) {
    if (typeof item === 'string' && (known as readonly string[]).includes(item)) {
      seen.add(item as T);
    }
  }
  return [...seen];
}

/** Read `capabilities.start_options`; null when the mower offers none of the options. */
export function jobOptionsSpec(raw: unknown): JobOptionsSpec | null {
  if (!raw || typeof raw !== 'object') return null;
  const value = raw as Record<string, unknown>;
  const spec: JobOptionsSpec = {
    detection: picks(value.obstacle_detection, OBSTACLE_MODES),
    obstacleLaps: range(value.obstacle_laps_range, 4),
    orders: picks(value.orders, PATH_ORDERS),
    progress: range(value.start_progress_range_pct, 99),
  };
  // A progress range that only allows 0 is no choice.
  if (spec.progress && spec.progress[1] <= 0) spec.progress = null;
  const offered =
    spec.detection.length > 0 ||
    spec.obstacleLaps !== null ||
    spec.orders.length > 0 ||
    spec.progress !== null;
  return offered ? spec : null;
}

/**
 * The options the sheet opens with: the saved defaults where this mower offers them, else the
 * mower's own (null). The start progress always opens at 0: it belongs to one start.
 */
export function initialOptions(
  saved: JobOptionKeys | null | undefined,
  spec: JobOptionsSpec,
): JobOptionsDraft {
  const detection =
    saved?.obstacle_detection && spec.detection.includes(saved.obstacle_detection)
      ? saved.obstacle_detection
      : null;
  const laps = saved?.obstacle_laps;
  const obstacleLaps =
    spec.obstacleLaps &&
    finite(laps) &&
    laps >= spec.obstacleLaps[0] &&
    laps <= spec.obstacleLaps[1]
      ? Math.round(laps)
      : null;
  const order = saved?.order && spec.orders.includes(saved.order) ? saved.order : null;
  return { detection, obstacleLaps, order, progress: 0 };
}

/** The draft as `MowPattern` keys: only what is set (progress only above 0). */
export function optionKeys(draft: JobOptionsDraft | null | undefined): JobOptionKeys {
  const keys: JobOptionKeys = {};
  if (!draft) return keys;
  if (draft.detection !== null) keys.obstacle_detection = draft.detection;
  if (draft.obstacleLaps !== null) keys.obstacle_laps = draft.obstacleLaps;
  if (draft.order !== null) keys.order = draft.order;
  if (draft.progress > 0) keys.start_progress_pct = draft.progress;
  return keys;
}

/** One lap more or fewer; from the mower's own setting the first step goes to 1 or 0. */
export function stepObstacleLaps(
  current: number | null,
  direction: 1 | -1,
  limits: OptionRange,
): number {
  if (current === null) return clamp(direction > 0 ? 1 : 0, limits[0], limits[1]);
  return clamp(current + direction, limits[0], limits[1]);
}

/** Five percent more or less, within the range (99 % is the last step up). */
export function stepProgress(current: number, direction: 1 | -1, limits: OptionRange): number {
  const next =
    direction > 0
      ? (Math.floor(current / PROGRESS_STEP) + 1) * PROGRESS_STEP
      : (Math.ceil(current / PROGRESS_STEP) - 1) * PROGRESS_STEP;
  return clamp(next, limits[0], limits[1]);
}

/** How many options the draft sets (for the "Mehr" badge). */
export function optionsSet(draft: JobOptionsDraft | null | undefined): number {
  return Object.keys(optionKeys(draft)).length;
}
