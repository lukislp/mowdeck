/**
 * Virtual zones on `MowerMap` (ADR 0070): zones the owner draws for a mower whose vendor shares
 * no map. They are stored in WGS84 (`[lat, lon]` corners), drawn as outlined polygons with a
 * name, the zone the mower is in highlighted, and a draft drawn corner by corner while the user
 * taps the map. Pure functions, unit tested without a DOM (`src/tests/virtual-zones.test.ts`);
 * the server validates again (`telemetry.domain.zones`), this only gives early feedback.
 */
import { toWgs84, type GeoReference } from './geo';
import type { MapCoord, Projection } from './projection';
import { polygonAreaM2, type LocalPoint } from './spot';

/** One zone as the map draws it. */
export interface MapVirtualZone {
  id: string;
  name: string;
  /** Corners as `[lat, lon]`, open ring. */
  ring: readonly (readonly number[])[];
}

/** What `MowerMap` draws for one zone. */
export interface VirtualZoneLayer {
  id: string;
  name: string;
  ring: MapCoord[];
  labelAt: MapCoord;
  /** The mower is in this zone. */
  current: boolean;
  /** The zone is being edited or picked in a list. */
  selected: boolean;
}

/** Bounds as `[south-west, north-east]` map coordinates. */
export type CoordBounds = [MapCoord, MapCoord];

function boundsOf(coords: readonly MapCoord[]): CoordBounds | null {
  if (!coords.length) return null;
  const a = coords.map((c) => c[0]);
  const b = coords.map((c) => c[1]);
  return [
    [Math.min(...a), Math.min(...b)],
    [Math.max(...a), Math.max(...b)],
  ];
}

/** The box around two optional boxes. */
export function unionBounds(
  a: CoordBounds | null | undefined,
  b: CoordBounds | null | undefined,
): CoordBounds | null {
  if (!a) return b ?? null;
  if (!b) return a;
  return [
    [Math.min(a[0][0], b[0][0]), Math.min(a[0][1], b[0][1])],
    [Math.max(a[1][0], b[1][0]), Math.max(a[1][1], b[1][1])],
  ];
}

/** Vertex mean of a ring of map coordinates (the label position of a zone). */
function centre(ring: readonly MapCoord[]): MapCoord {
  const n = Math.max(1, ring.length);
  return [ring.reduce((s, c) => s + c[0], 0) / n, ring.reduce((s, c) => s + c[1], 0) / n];
}

/** The zones in map coordinates, with their bounds. */
export function virtualZoneLayers(
  zones: readonly MapVirtualZone[] | null | undefined,
  projection: Projection,
  options: { current?: string | null; selected?: string | null; hidden?: string | null } = {},
): { zones: VirtualZoneLayer[]; bounds: CoordBounds | null } {
  const out: VirtualZoneLayer[] = [];
  for (const zone of zones ?? []) {
    if (zone.id === options.hidden) continue;
    const ring = zone.ring
      .filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]))
      .map((p) => projection.fromWgs84(p[0] ?? 0, p[1] ?? 0));
    if (ring.length < 3) continue;
    out.push({
      id: zone.id,
      name: zone.name,
      ring,
      labelAt: centre(ring),
      current: zone.id === options.current,
      selected: zone.id === options.selected,
    });
  }
  return { zones: out, bounds: boundsOf(out.flatMap((z) => z.ring)) };
}

/** What `MowerMap` draws for a zone being drawn (local metres from the map taps). */
export interface ZoneDraftLayer {
  ring: MapCoord[];
  corners: MapCoord[];
  bounds: CoordBounds | null;
}

export function zoneDraftLayer(
  points: readonly LocalPoint[] | null | undefined,
  projection: Projection,
): ZoneDraftLayer | null {
  if (!points) return null;
  const corners = points.map((p) => projection.fromLocal(p[0], p[1]));
  return { ring: corners.length >= 3 ? corners : [], corners, bounds: boundsOf(corners) };
}

/** Local metres to `[lat, lon]` corners for the API (rounded to about a centimetre). */
export function draftToRing(
  reference: GeoReference,
  points: readonly LocalPoint[],
): [number, number][] {
  const round = (v: number) => Math.round(v * 1e7) / 1e7;
  return points.map((p) => {
    const [lat, lon] = toWgs84(reference, p[0], p[1]);
    return [round(lat), round(lon)];
  });
}

/** Why a draft cannot be saved yet, or null when it can. */
export type DraftProblem = 'corners' | 'crossing' | 'small' | 'large' | 'many';

const cross = (a: LocalPoint, b: LocalPoint, c: LocalPoint) =>
  (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);

/** Whether the closed segments `a-b` and `c-d` cross or touch. */
export function segmentsTouch(a: LocalPoint, b: LocalPoint, c: LocalPoint, d: LocalPoint): boolean {
  const eps = 1e-9;
  const d1 = cross(a, b, c);
  const d2 = cross(a, b, d);
  const d3 = cross(c, d, a);
  const d4 = cross(c, d, b);
  if (
    ((d1 > eps && d2 < -eps) || (d1 < -eps && d2 > eps)) &&
    ((d3 > eps && d4 < -eps) || (d3 < -eps && d4 > eps))
  ) {
    return true;
  }
  const within = (p: LocalPoint, q: LocalPoint, r: LocalPoint) =>
    Math.min(p[0], q[0]) - eps <= r[0] &&
    r[0] <= Math.max(p[0], q[0]) + eps &&
    Math.min(p[1], q[1]) - eps <= r[1] &&
    r[1] <= Math.max(p[1], q[1]) + eps;
  return (
    (Math.abs(d1) <= eps && within(a, b, c)) ||
    (Math.abs(d2) <= eps && within(a, b, d)) ||
    (Math.abs(d3) <= eps && within(c, d, a)) ||
    (Math.abs(d4) <= eps && within(c, d, b))
  );
}

/** Whether two edges of the closed ring that are not neighbours cross or touch. */
export function ringSelfIntersects(points: readonly LocalPoint[]): boolean {
  const n = points.length;
  for (let i = 0; i < n; i += 1) {
    for (let j = i + 1; j < n; j += 1) {
      if (j === i + 1 || (j + 1) % n === i) continue;
      const a = points[i] as LocalPoint;
      const b = points[(i + 1) % n] as LocalPoint;
      const c = points[j] as LocalPoint;
      const d = points[(j + 1) % n] as LocalPoint;
      if (segmentsTouch(a, b, c, d)) return true;
    }
  }
  return false;
}

/** Limits of a zone (from `ZonesOut.limits`; defaults match the server). */
export interface ZoneLimits {
  min_corners: number;
  max_corners: number;
  min_area_m2: number;
  max_area_m2: number;
}

export const DEFAULT_ZONE_LIMITS: ZoneLimits = {
  min_corners: 3,
  max_corners: 64,
  min_area_m2: 1,
  max_area_m2: 100_000,
};

/** The first reason the draft cannot be saved, or null. */
export function draftProblem(
  points: readonly LocalPoint[],
  limits: ZoneLimits = DEFAULT_ZONE_LIMITS,
): DraftProblem | null {
  if (points.length < limits.min_corners) return 'corners';
  if (points.length > limits.max_corners) return 'many';
  if (ringSelfIntersects(points)) return 'crossing';
  const area = polygonAreaM2(points);
  if (area < limits.min_area_m2) return 'small';
  if (area > limits.max_area_m2) return 'large';
  return null;
}
