/**
 * The heat layers of `MowerMap` (coverage, RTK fix share, WLAN and mobile signal): pure
 * functions that turn a Telemetry heatmap answer (`GET /telemetry/mowers/{id}/heatmap`) into
 * rectangles in local metres with a colour stop, the colour scale of each legend kind and the
 * outlines of missed spots (gaps). `MowerMap.svelte` only draws what these return, so the
 * colour decisions are unit tested without a DOM (`src/tests/heat.test.ts`).
 *
 * Colours are CSS expressions over the design tokens (never literal values), so a layer follows
 * the theme like everything else on the map. Texts are not part of the scale: a stop carries a
 * `key` and its numeric bounds, the app turns them into words ("schwach" ... "stark").
 *
 * The input types are structural copies of the API answer, so the library does not depend on
 * generated types.
 */
import type { MapCoord, Projection } from './projection';

/** How a cell's `value` reads: coverage ratio 0..1, RTK fixed share 0..1, median dBm. */
export type HeatLegendKind = 'ratio' | 'fix' | 'dbm';

export interface HeatLegend {
  kind: HeatLegendKind;
  min?: number | null;
  max?: number | null;
}

/** One cell of the answer: integer indexes in the local frame of the map. */
export interface HeatCellInput {
  x: number;
  y: number;
  value: number;
  /** Samples behind the value. */
  n?: number | null;
}

/** A missed spot: a polygon in local metres and its area. */
export interface HeatGap {
  polygon: readonly (readonly number[])[];
  area_m2: number;
}

/** What `MowerMap` needs to draw a heat layer (`heat` prop). */
export interface HeatLayerData {
  cells: readonly HeatCellInput[];
  /** Edge of a cell in metres, as answered (may be coarser than requested). */
  cellM: number;
  legend: HeatLegend;
  /** Missed spots, drawn as thin outlines (coverage only). */
  gaps?: readonly HeatGap[] | null;
}

/** One step of a colour scale: values in `[from, to)` get `color`. */
export interface HeatStop {
  /** Stable name of the step for texts and tests, e.g. `weak` or `fixed`. */
  key: string;
  /** Lower bound (inclusive); `-Infinity` for the first step. */
  from: number;
  /** Upper bound (exclusive); `Infinity` for the last step. */
  to: number;
  /** A CSS colour expression over the tokens (works as `fill`, `background`, `stroke`). */
  color: string;
}

export interface HeatScale {
  kind: HeatLegendKind;
  /** From the worst (or emptiest) step to the best. */
  stops: readonly HeatStop[];
}

/** A cell as a rectangle in local metres with its colour step. */
export interface HeatRect {
  ix: number;
  iy: number;
  /** South-west and north-east corners in local metres. */
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  value: number;
  n: number;
  /** Index into the scale's `stops`. */
  stop: number;
  color: string;
}

/** A gap prepared for drawing and for the list. */
export interface GapOutline {
  /** Position in the answer's `gaps` (stable id for selection). */
  index: number;
  areaM2: number;
  /** Ring in map coordinates. */
  ring: MapCoord[];
  /** Centre of the outline in local metres (vertex mean) and its map coordinate. */
  centre: [number, number];
  centreAt: MapCoord;
  /** South-west and north-east of the outline in map coordinates. */
  bounds: [MapCoord, MapCoord];
}

const INK = 'var(--md-color-ink-muted)';
const GOOD = 'var(--md-state-mowing)';
const WARN = 'var(--md-state-paused)';
const BAD = 'var(--md-state-error)';

const mix = (a: string, share: number, b: string): string =>
  share >= 100 ? a : share <= 0 ? b : `color-mix(in srgb, ${a} ${share}%, ${b})`;

/** Coverage: five steps from ink-muted (not cut) to the mowing green (always cut). */
const RATIO: HeatScale = {
  kind: 'ratio',
  stops: [0, 25, 50, 75, 100].map((share, i) => ({
    key: `r${i}`,
    from: i === 0 ? -Infinity : i * 0.2,
    to: i === 4 ? Infinity : (i + 1) * 0.2,
    color: mix(GOOD, share, INK),
  })),
};

/** Below this fixed share a cell reads as no fix (red), from `FIX_GOOD` as fixed (green). */
export const FIX_WEAK = 0.5;
export const FIX_GOOD = 0.9;

/** RTK: the fixed share in three bands, coloured like the RTK classes of the route tint. */
const FIX: HeatScale = {
  kind: 'fix',
  stops: [
    { key: 'none', from: -Infinity, to: FIX_WEAK, color: 'var(--md-rtk-none)' },
    { key: 'float', from: FIX_WEAK, to: FIX_GOOD, color: 'var(--md-rtk-float)' },
    { key: 'fixed', from: FIX_GOOD, to: Infinity, color: 'var(--md-rtk-fixed)' },
  ],
};

/** The dBm scale spans -90 (weak) to -40 (strong) in five steps of 10 dB. */
export const DBM_MIN = -90;
export const DBM_MAX = -40;
const DBM_STEP = (DBM_MAX - DBM_MIN) / 5;

/** Signal: five steps, red through amber to green (keys weak, poor, fair, good, strong). */
const DBM: HeatScale = {
  kind: 'dbm',
  stops: (
    [
      ['weak', BAD],
      ['poor', mix(BAD, 50, WARN)],
      ['fair', WARN],
      ['good', mix(WARN, 50, GOOD)],
      ['strong', GOOD],
    ] as const
  ).map(([key, color], i) => ({
    key,
    from: i === 0 ? -Infinity : DBM_MIN + i * DBM_STEP,
    to: i === 4 ? Infinity : DBM_MIN + (i + 1) * DBM_STEP,
    color,
  })),
};

const SCALES: Record<HeatLegendKind, HeatScale> = { ratio: RATIO, fix: FIX, dbm: DBM };

/** The colour scale of a legend kind (unknown kinds read as a ratio). */
export function heatScale(kind: HeatLegendKind | string): HeatScale {
  return SCALES[kind as HeatLegendKind] ?? RATIO;
}

/** Index of the step a value falls into. */
export function heatStop(scale: HeatScale, value: number): number {
  if (!Number.isFinite(value)) return 0;
  const i = scale.stops.findIndex((s) => value >= s.from && value < s.to);
  return i < 0 ? scale.stops.length - 1 : i;
}

/** The cells of an answer as rectangles in local metres, each with its colour step. */
export function heatCells(data: HeatLayerData | null | undefined): HeatRect[] {
  if (!data || !(data.cellM > 0)) return [];
  const scale = heatScale(data.legend.kind);
  const size = data.cellM;
  const rects: HeatRect[] = [];
  for (const cell of data.cells) {
    if (!Number.isFinite(cell.x) || !Number.isFinite(cell.y) || !Number.isFinite(cell.value))
      continue;
    const stop = heatStop(scale, cell.value);
    rects.push({
      ix: cell.x,
      iy: cell.y,
      x0: cell.x * size,
      y0: cell.y * size,
      x1: (cell.x + 1) * size,
      y1: (cell.y + 1) * size,
      value: cell.value,
      n: cell.n ?? 0,
      stop,
      color: scale.stops[stop]!.color,
    });
  }
  return rects;
}

/** The four corners of a rectangle in map coordinates (counter-clockwise from south-west). */
export function heatRing(rect: HeatRect, projection: Projection): MapCoord[] {
  return [
    projection.fromLocal(rect.x0, rect.y0),
    projection.fromLocal(rect.x1, rect.y0),
    projection.fromLocal(rect.x1, rect.y1),
    projection.fromLocal(rect.x0, rect.y1),
  ];
}

/** Gaps with at least three valid vertices, as outlines in map coordinates. */
export function gapOutlines(
  gaps: readonly HeatGap[] | null | undefined,
  projection: Projection,
): GapOutline[] {
  const result: GapOutline[] = [];
  (gaps ?? []).forEach((gap, index) => {
    const points: [number, number][] = gap.polygon
      .filter((p) => p.length >= 2 && Number.isFinite(p[0]) && Number.isFinite(p[1]))
      .map((p) => [p[0] as number, p[1] as number]);
    if (points.length < 3) return;
    const ring = points.map(([x, y]) => projection.fromLocal(x, y));
    const cx = points.reduce((s, p) => s + p[0], 0) / points.length;
    const cy = points.reduce((s, p) => s + p[1], 0) / points.length;
    const lats = ring.map((c) => c[0]);
    const lngs = ring.map((c) => c[1]);
    result.push({
      index,
      areaM2: gap.area_m2,
      ring,
      centre: [cx, cy],
      centreAt: projection.fromLocal(cx, cy),
      bounds: [
        [Math.min(...lats), Math.min(...lngs)],
        [Math.max(...lats), Math.max(...lngs)],
      ],
    });
  });
  return result;
}

/** Is local point `(x, y)` inside the polygon (even-odd rule)? */
export function pointInPolygon(
  x: number,
  y: number,
  polygon: readonly (readonly number[])[],
): boolean {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const [xi, yi] = polygon[i] as [number, number];
    const [xj, yj] = polygon[j] as [number, number];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** The ids of the areas that contain the gap's centre (to mow its area again). */
export function areasOfGap(
  gap: HeatGap,
  areas: readonly { id: string; boundary: readonly (readonly number[])[] }[] | null | undefined,
): string[] {
  const pts = gap.polygon.filter((p) => p.length >= 2);
  if (!pts.length) return [];
  const cx = pts.reduce((s, p) => s + (p[0] as number), 0) / pts.length;
  const cy = pts.reduce((s, p) => s + (p[1] as number), 0) / pts.length;
  return (areas ?? []).filter((a) => pointInPolygon(cx, cy, a.boundary)).map((a) => a.id);
}
