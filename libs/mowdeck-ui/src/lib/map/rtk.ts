/**
 * The RTK signal map of `MowerMap`: pure functions that bin the driven route's points (each
 * carries the RTK fix it was reported with) into a square metric grid in the mower's local
 * frame and give every cell one RTK class, so weak spots of the garden show up as coloured
 * squares. Unit tested without a DOM (`src/tests/rtk.test.ts`).
 *
 * A cell's class is either the `worst` fix seen in it (one `none` makes the cell red: where the
 * signal ever dropped) or the `majority` fix (ties go to the worse class). Points without a
 * known fix only decide a cell that has nothing else (`unknown`).
 */
import { fromWgs84, type GeoReference } from './geo';
import { rtkClass, type RoutePoint, type RtkClass } from './layers';
import type { MapCoord, Projection } from './projection';

/** Cell edge lengths offered in the layer panel, in metres. */
export const RTK_CELL_SIZES = [0.5, 1, 2] as const;
export type RtkCellSize = (typeof RTK_CELL_SIZES)[number];
export type RtkAggregate = 'worst' | 'majority';

export interface RtkCounts {
  fixed: number;
  float: number;
  none: number;
  unknown: number;
}

export interface RtkCell {
  /** Column and row index: the cell covers `[ix * size, (ix + 1) * size)` local metres. */
  ix: number;
  iy: number;
  size: number;
  counts: RtkCounts;
  total: number;
  rtk: RtkClass;
}

export interface RtkSummary {
  /** Points with a valid position. */
  total: number;
  counts: RtkCounts;
  /** Share of each class in `[0, 1]` (all 0 without points). */
  shares: Record<keyof RtkCounts, number>;
}

/** Known classes from worst to best; `unknown` is not part of the ranking. */
const RANK: readonly Exclude<RtkClass, 'unknown'>[] = ['none', 'float', 'fixed'];

const empty = (): RtkCounts => ({ fixed: 0, float: 0, none: 0, unknown: 0 });

/** The class of a cell from its counts. */
export function cellClass(counts: RtkCounts, aggregate: RtkAggregate): RtkClass {
  if (aggregate === 'worst') return RANK.find((c) => counts[c] > 0) ?? 'unknown';
  let best: RtkClass = 'unknown';
  let most = 0;
  for (const c of RANK) {
    // Strictly more wins, so on a tie the worse class (earlier in RANK) stays.
    if (counts[c] > most) {
      best = c;
      most = counts[c];
    }
  }
  return best;
}

function valid(p: RoutePoint): boolean {
  return Number.isFinite(p.lat) && Number.isFinite(p.lon);
}

/** Bin the points into cells of `size` metres in the local frame of `reference`. */
export function binRtk(
  points: readonly RoutePoint[],
  reference: GeoReference,
  options: { size?: number; aggregate?: RtkAggregate } = {},
): RtkCell[] {
  const size = options.size && options.size > 0 ? options.size : 1;
  const aggregate = options.aggregate ?? 'worst';
  const cells = new Map<string, RtkCell>();
  for (const p of points) {
    if (!valid(p)) continue;
    const [x, y] = fromWgs84(reference, p.lat, p.lon);
    // A tiny epsilon keeps points that sit on a cell edge (within float noise) in their cell.
    const ix = Math.floor(x / size + 1e-9);
    const iy = Math.floor(y / size + 1e-9);
    const key = `${ix}:${iy}`;
    let cell = cells.get(key);
    if (!cell) {
      cell = { ix, iy, size, counts: empty(), total: 0, rtk: 'unknown' };
      cells.set(key, cell);
    }
    cell.counts[rtkClass(p.rtk_fix)] += 1;
    cell.total += 1;
  }
  const result = [...cells.values()];
  for (const cell of result) cell.rtk = cellClass(cell.counts, aggregate);
  return result.sort((a, b) => a.iy - b.iy || a.ix - b.ix);
}

/** The four corners of a cell in map coordinates (counter-clockwise from the south-west). */
export function cellRing(cell: RtkCell, projection: Projection): MapCoord[] {
  const x0 = cell.ix * cell.size;
  const y0 = cell.iy * cell.size;
  const x1 = x0 + cell.size;
  const y1 = y0 + cell.size;
  return [
    projection.fromLocal(x0, y0),
    projection.fromLocal(x1, y0),
    projection.fromLocal(x1, y1),
    projection.fromLocal(x0, y1),
  ];
}

/** Share of fixed, float, none and unknown over all points with a position. */
export function rtkSummary(points: readonly RoutePoint[]): RtkSummary {
  const counts = empty();
  let total = 0;
  for (const p of points) {
    if (!valid(p)) continue;
    counts[rtkClass(p.rtk_fix)] += 1;
    total += 1;
  }
  const share = (n: number) => (total ? n / total : 0);
  return {
    total,
    counts,
    shares: {
      fixed: share(counts.fixed),
      float: share(counts.float),
      none: share(counts.none),
      unknown: share(counts.unknown),
    },
  };
}
