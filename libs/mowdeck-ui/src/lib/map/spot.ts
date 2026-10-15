/**
 * The spot of a spot-mowing run on the map: a circle around a tapped point or a drawn polygon,
 * both in the mower's local metres. Pure geometry, unit tested without a DOM
 * (`src/tests/spot.test.ts`): the circle as a polygon, which mapped area contains the spot, how
 * far the mower stands from it, and the rings `MowerMap` draws.
 */
import { pointInPolygon } from './heat';
import type { MapCoord, Projection } from './projection';

/** A local point `[x, y]` in metres. */
export type LocalPoint = readonly [number, number];

/** The spot as the user chose it (`spot` prop of `MowerMap`). */
export type MapSpot =
  | { kind: 'circle'; center: LocalPoint; radius_m: number }
  | { kind: 'polygon'; points: readonly LocalPoint[] };

/** Corners a circle is drawn and planned with. */
export const CIRCLE_CORNERS = 32;

/** A regular polygon approximating the circle (counter-clockwise, first corner east). */
export function circlePolygon(
  center: LocalPoint,
  radiusM: number,
  corners: number = CIRCLE_CORNERS,
): LocalPoint[] {
  const n = Math.max(3, Math.floor(corners));
  const out: LocalPoint[] = [];
  for (let i = 0; i < n; i += 1) {
    const a = (2 * Math.PI * i) / n;
    out.push([
      round2(center[0] + radiusM * Math.cos(a)),
      round2(center[1] + radiusM * Math.sin(a)),
    ]);
  }
  return out;
}

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}

/** The spot's outline as a polygon (a circle becomes a 32-gon). Fewer than three corners: []. */
export function spotPolygon(spot: MapSpot | null | undefined): LocalPoint[] {
  if (!spot) return [];
  if (spot.kind === 'circle') return circlePolygon(spot.center, spot.radius_m);
  return spot.points.length >= 3 ? spot.points.map((p) => [p[0], p[1]] as LocalPoint) : [];
}

/** True when the spot has a usable shape (a circle, or a polygon of three or more corners). */
export function spotComplete(spot: MapSpot | null | undefined): boolean {
  return spotPolygon(spot).length >= 3;
}

/** The centre of the spot: the circle's centre or the polygon's vertex mean. */
export function spotCenter(spot: MapSpot): LocalPoint {
  if (spot.kind === 'circle') return spot.center;
  const pts = spot.points;
  if (!pts.length) return [0, 0];
  return [
    pts.reduce((s, p) => s + p[0], 0) / pts.length,
    pts.reduce((s, p) => s + p[1], 0) / pts.length,
  ];
}

/** Area of a polygon in square metres (shoelace). */
export function polygonAreaM2(points: readonly LocalPoint[]): number {
  let sum = 0;
  for (let i = 0; i < points.length; i += 1) {
    const a = points[i] as LocalPoint;
    const b = points[(i + 1) % points.length] as LocalPoint;
    sum += a[0] * b[1] - b[0] * a[1];
  }
  return Math.abs(sum) / 2;
}

/**
 * The one mapped area that contains the whole spot (every corner of its polygon), or null when
 * the spot straddles a boundary, lies outside the map, or has no shape yet. The Planner clips
 * the plan to that area.
 */
export function containingArea(
  spot: MapSpot | null | undefined,
  areas: readonly { id: string; boundary: readonly (readonly number[])[] }[] | null | undefined,
): string | null {
  const polygon = spotPolygon(spot);
  if (!polygon.length) return null;
  for (const area of areas ?? []) {
    if (polygon.every((p) => pointInPolygon(p[0], p[1], area.boundary))) return area.id;
  }
  return null;
}

/** Distance from `point` to the nearest point of the segment `a`-`b`. */
export function segmentDistance(point: LocalPoint, a: LocalPoint, b: LocalPoint): number {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const len2 = dx * dx + dy * dy;
  let t = len2 > 0 ? ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / len2 : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(point[0] - (a[0] + t * dx), point[1] - (a[1] + t * dy));
}

/**
 * How far `point` stands from the spot: 0 inside it, else the distance to its edge (a circle:
 * to its rim). Infinity for a spot without a shape.
 */
export function distanceToSpot(point: LocalPoint, spot: MapSpot | null | undefined): number {
  if (!spot) return Number.POSITIVE_INFINITY;
  if (spot.kind === 'circle') {
    return Math.max(
      0,
      Math.hypot(point[0] - spot.center[0], point[1] - spot.center[1]) - spot.radius_m,
    );
  }
  const polygon = spotPolygon(spot);
  if (!polygon.length) return Number.POSITIVE_INFINITY;
  if (pointInPolygon(point[0], point[1], polygon)) return 0;
  let best = Number.POSITIVE_INFINITY;
  for (let i = 0; i < polygon.length; i += 1) {
    const a = polygon[i] as LocalPoint;
    const b = polygon[(i + 1) % polygon.length] as LocalPoint;
    best = Math.min(best, segmentDistance(point, a, b));
  }
  return best;
}

/**
 * At most `max` corners of a ring, keeping the first corner and an even stride (the audit
 * record of a run allows 64 corners; a missed spot from the coverage layer may have more).
 */
export function thinRing(points: readonly LocalPoint[], max: number): LocalPoint[] {
  if (points.length <= max) return points.map((p) => [p[0], p[1]]);
  const out: LocalPoint[] = [];
  for (let i = 0; i < max; i += 1) {
    const index = Math.floor((i * points.length) / max);
    const p = points[index] as LocalPoint;
    out.push([p[0], p[1]]);
  }
  return out;
}

/** What `MowerMap` draws for a spot: the ring (complete shapes) and the corners placed so far. */
export interface SpotLayer {
  ring: MapCoord[];
  corners: MapCoord[];
  center: MapCoord | null;
  bounds: [MapCoord, MapCoord] | null;
}

export function spotLayer(
  spot: MapSpot | null | undefined,
  projection: Projection,
): SpotLayer | null {
  if (!spot) return null;
  const polygon = spotPolygon(spot);
  const ring = polygon.map((p) => projection.fromLocal(p[0], p[1]));
  const corners =
    spot.kind === 'polygon' ? spot.points.map((p) => projection.fromLocal(p[0], p[1])) : [];
  const center =
    spot.kind === 'circle' ? projection.fromLocal(spot.center[0], spot.center[1]) : null;
  const all = ring.length ? ring : corners;
  let bounds: SpotLayer['bounds'] = null;
  if (all.length) {
    const a = all.map((c) => c[0]);
    const b = all.map((c) => c[1]);
    bounds = [
      [Math.min(...a), Math.min(...b)],
      [Math.max(...a), Math.max(...b)],
    ];
  }
  return { ring, corners, center, bounds };
}
