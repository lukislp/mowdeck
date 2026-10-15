/**
 * The snap logic of `BottomSheet`, free of the DOM so it is unit tested: three snap points
 * (`peek`, `half`, `full`), their heights for the space the sheet may use, the snap a drag ends
 * on (a fling goes on to the next snap in its direction, a slow release to the nearest one), the
 * cycle of a tap on the handle and the release velocity from pointer samples.
 */

export type SnapPoint = 'peek' | 'half' | 'full';

export const SNAP_ORDER: readonly SnapPoint[] = ['peek', 'half', 'full'];

/** `half` takes this share of the available height. */
export const HALF_RATIO = 0.45;
/** `full` takes this share of the available height. */
export const FULL_RATIO = 0.9;
/** The map (or whatever lies behind) keeps at least this many pixels above the sheet. */
export const MIN_UNCOVERED_PX = 56;
/** Peek never shrinks below this (handle plus one row and a 48 px action). */
export const PEEK_MIN_PX = 96;
/** A release faster than this (px/ms, about 500 px/s) is a fling. */
export const FLING_PX_PER_MS = 0.5;
/** A pointer that moved less than this is a tap, not a drag. */
export const TAP_SLOP_PX = 6;
/** Velocity looks at the pointer samples of this last span. */
export const VELOCITY_WINDOW_MS = 100;

export type SnapHeights = Record<SnapPoint, number>;

/**
 * The height of every snap point. `available` is the space between the top of the viewport and
 * the sheet's bottom edge, `peek` the height the peek content needs. The heights only grow from
 * peek to full, and full always leaves `MIN_UNCOVERED_PX` uncovered.
 */
export function snapHeights(available: number, peek: number): SnapHeights {
  const room = Math.max(0, Math.round(available));
  const top = Math.max(0, room - MIN_UNCOVERED_PX);
  const p = Math.min(Math.max(Math.round(peek), PEEK_MIN_PX), top);
  const half = Math.min(Math.max(Math.round(room * HALF_RATIO), p), top);
  const full = Math.min(Math.max(Math.round(room * FULL_RATIO), half), top);
  return { peek: p, half, full };
}

/** The allowed snap points in their order (peek is always allowed). */
export function allowedSnaps(snaps: readonly SnapPoint[] | undefined): SnapPoint[] {
  const wanted = new Set<SnapPoint>(snaps?.length ? snaps : SNAP_ORDER);
  wanted.add('peek');
  return SNAP_ORDER.filter((s) => wanted.has(s));
}

/** `snap` when allowed, otherwise the nearest allowed one below it (peek at the least). */
export function coerceSnap(snap: SnapPoint, allowed: readonly SnapPoint[]): SnapPoint {
  if (allowed.includes(snap)) return snap;
  const index = SNAP_ORDER.indexOf(snap);
  for (let i = index - 1; i >= 0; i -= 1) {
    const s = SNAP_ORDER[i]!;
    if (allowed.includes(s)) return s;
  }
  return 'peek';
}

/** The tap on the handle: peek, half, full, then peek again (only allowed snaps). */
export function nextSnap(current: SnapPoint, allowed: readonly SnapPoint[]): SnapPoint {
  const list = allowed.length ? allowed : (['peek'] as const);
  const index = list.indexOf(current);
  return list[(index + 1) % list.length] ?? 'peek';
}

/** A drag keeps the sheet between its lowest and highest allowed snap. */
export function clampHeight(
  height: number,
  heights: SnapHeights,
  allowed: readonly SnapPoint[],
): number {
  const values = allowed.map((s) => heights[s]);
  return Math.min(Math.max(height, Math.min(...values)), Math.max(...values));
}

/** The allowed snap whose height is closest to `height` (the lower one on a tie). */
export function nearestSnap(
  height: number,
  heights: SnapHeights,
  allowed: readonly SnapPoint[],
): SnapPoint {
  let best: SnapPoint = allowed[0] ?? 'peek';
  for (const s of allowed) {
    if (Math.abs(heights[s] - height) < Math.abs(heights[best] - height)) best = s;
  }
  return best;
}

/**
 * Where a drag ends. `velocity` is in px/ms, positive while the sheet grows (the finger moves
 * up). A fling goes to the next allowed snap beyond the current height in its direction (or
 * stays at the last one); a slow release goes to the nearest snap.
 */
export function releaseSnap(
  height: number,
  velocity: number,
  heights: SnapHeights,
  allowed: readonly SnapPoint[],
): SnapPoint {
  if (Math.abs(velocity) < FLING_PX_PER_MS) return nearestSnap(height, heights, allowed);
  const ordered = [...allowed].sort((a, b) => heights[a] - heights[b]);
  if (velocity > 0) {
    return ordered.find((s) => heights[s] > height + 1) ?? ordered[ordered.length - 1] ?? 'peek';
  }
  return [...ordered].reverse().find((s) => heights[s] < height - 1) ?? ordered[0] ?? 'peek';
}

export interface PointerSample {
  /** Time in ms (`event.timeStamp`). */
  t: number;
  /** `clientY` in px. */
  y: number;
}

/**
 * The growth speed of the sheet in px/ms from the pointer samples of the last
 * `VELOCITY_WINDOW_MS`: positive when the finger moved up. Zero with fewer than two samples.
 */
export function releaseVelocity(samples: readonly PointerSample[]): number {
  const last = samples[samples.length - 1];
  if (!last) return 0;
  const recent = samples.filter((s) => last.t - s.t <= VELOCITY_WINDOW_MS);
  const first = recent[0];
  if (!first || first === last || last.t === first.t) return 0;
  return (first.y - last.y) / (last.t - first.t);
}
