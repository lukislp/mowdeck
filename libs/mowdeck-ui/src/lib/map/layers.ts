/**
 * The layer model of `MowerMap`: pure functions that turn a map snapshot, the mower state and
 * the driven route into layer descriptors in map coordinates. `MowerMap.svelte` only draws
 * these descriptors with Leaflet, so everything that decides *what* is drawn is unit tested
 * without a DOM (`src/tests/layers.test.ts`).
 *
 * The input types are structural copies of the contracts (`MowerMap`, `MowerState`,
 * `PositionOut`), so the library does not depend on generated API types.
 */
import { ACTIVITIES, activityTone, type Activity, type ActivityTone } from '../types';

import type { GeoReference } from './geo';
import type { MapCoord, Projection } from './projection';

type Point = readonly number[];

/** The map snapshot (`MowerMap` of the contracts): geometry in local metres. */
export interface MapSnapshot {
  reference: GeoReference;
  areas?: { id: string; name?: string | null; boundary: Point[] }[];
  no_go_zones?: { id: string; name?: string | null; boundary: Point[] }[];
  obstacles?: { id: string; name?: string | null; boundary: Point[] }[];
  paths?: { id: string; name?: string | null; points: Point[] }[];
  dock?: { x: number; y: number; heading_deg?: number } | null;
  revision?: string | null;
}

/** The parts of a `MowerState` the map shows. */
export interface MapMowerState {
  activity: string;
  position?: { lat: number; lon: number; heading?: number | null } | null;
  rtk_fix?: string;
  /** Where the position comes from; `dock` is the known dock pose, not a measurement. */
  position_source?: string;
  observed_at?: string;
}

/**
 * The position is a plain GNSS guess: no RTK fix (`none`) and not the dock pose, which the
 * mower reports without a fix on purpose.
 */
export function isPositionInexact(
  state:
    | {
        rtk_fix?: string;
        position_source?: string;
      }
    | null
    | undefined,
): boolean {
  return state?.rtk_fix === 'none' && state.position_source !== 'dock';
}

/** One point of the driven route (`PositionOut` of Telemetry, or a live state's position). */
export interface RoutePoint {
  lat: number;
  lon: number;
  observed_at: string;
  rtk_fix?: string;
}

export type RtkClass = 'fixed' | 'float' | 'none' | 'unknown';
/** Age of a route segment within the shown route: 0 oldest (faint) to 3 newest (full). */
export type RouteAge = 0 | 1 | 2 | 3;

export interface AreaLayer {
  id: string;
  name: string;
  ring: MapCoord[];
  /** Where the label goes (the area centroid). */
  labelAt: MapCoord;
  /** Position in the start order (0 based), or null when not selected. */
  order: number | null;
}

export interface ZoneLayer {
  id: string;
  name: string | null;
  ring: MapCoord[];
}

export interface PathLayer {
  id: string;
  points: MapCoord[];
}

/** Charging station footprint in metres: along the heading (length) and across it (width). */
export const DOCK_LENGTH_M = 1.2;
export const DOCK_WIDTH_M = 0.9;

/**
 * The four local corners of the dock rectangle, centred on the dock, `lengthM` long along the
 * local heading (`heading_deg`, CCW from +x) and `widthM` across it. Order: rear-right,
 * front-right, front-left, rear-left (counter-clockwise); the open side is the front edge,
 * corners 1 and 2, facing the direction the mower leaves.
 */
export function dockOutline(
  dock: { x: number; y: number; heading_deg?: number },
  lengthM: number = DOCK_LENGTH_M,
  widthM: number = DOCK_WIDTH_M,
): [number, number][] {
  const h = ((dock.heading_deg ?? 0) * Math.PI) / 180;
  const ux = Math.cos(h);
  const uy = Math.sin(h);
  const l = lengthM / 2;
  const w = widthM / 2;
  // (a, b) in the dock frame: a along the heading, b to its left (+90 deg CCW).
  return (
    [
      [-l, -w],
      [l, -w],
      [l, w],
      [-l, w],
    ] as const
  ).map(([a, b]) => [dock.x + a * ux - b * uy, dock.y + a * uy + b * ux]);
}

export interface DockLayer {
  at: MapCoord;
  /** Footprint corners in map coordinates (rear-right, front-right, front-left, rear-left). */
  outline: MapCoord[];
  /** Screen rotation of the dock's heading notch (clockwise degrees from up). */
  rotation: number;
}

export interface MowerLayer {
  at: MapCoord;
  /** Screen rotation of the chevron, or null when the heading is unknown (drawn as a dot). */
  rotation: number | null;
  tone: ActivityTone;
  /** Stale, offline or without an RTK fix: drawn hollow and dashed. */
  hollow: boolean;
  /** No RTK fix and not the dock pose (`isPositionInexact`). */
  inexact: boolean;
}

export interface RouteSegment {
  points: MapCoord[];
  age: RouteAge;
  /** RTK class of the segment when the RTK tint is on, otherwise null. */
  rtk: RtkClass | null;
  /** Driven without an RTK fix (`none`): drawn dashed, with or without the tint. */
  inexact: boolean;
}

export interface GridLine {
  from: MapCoord;
  to: MapCoord;
  major: boolean;
}

export interface MapLayers {
  areas: AreaLayer[];
  noGo: ZoneLayer[];
  obstacles: ZoneLayer[];
  paths: PathLayer[];
  dock: DockLayer | null;
  mower: MowerLayer | null;
  route: RouteSegment[];
  /** Number of route points drawn (after dropping invalid ones). */
  routePoints: number;
  /** South-west and north-east corner of the map geometry, for fitting; null when empty. */
  bounds: [MapCoord, MapCoord] | null;
}

export interface LayerInput {
  map: MapSnapshot | null;
  state?: MapMowerState | null;
  route?: readonly RoutePoint[];
  selected?: readonly string[];
  projection: Projection;
  areaName: (area: { id: string; name?: string | null }) => string;
  /** The mower has no live link (offline, ADR 0040). */
  stale?: boolean;
  /** Colour route segments by RTK fix quality. */
  rtkTint?: boolean;
  /** A gap in the route longer than this starts a new line (default 5 minutes). */
  routeGapMs?: number;
}

const xy = (p: Point): [number, number] => [p[0] ?? 0, p[1] ?? 0];

/** Area-weighted centroid of a ring in local metres (vertex mean for degenerate rings). */
export function centroid(ring: readonly Point[]): [number, number] {
  let a = 0;
  let cx = 0;
  let cy = 0;
  for (let i = 0; i < ring.length; i += 1) {
    const [x1, y1] = xy(ring[i] ?? [0, 0]);
    const [x2, y2] = xy(ring[(i + 1) % ring.length] ?? [0, 0]);
    const cross = x1 * y2 - x2 * y1;
    a += cross;
    cx += (x1 + x2) * cross;
    cy += (y1 + y2) * cross;
  }
  if (Math.abs(a) < 1e-12) {
    const n = Math.max(1, ring.length);
    return [ring.reduce((s, p) => s + xy(p)[0], 0) / n, ring.reduce((s, p) => s + xy(p)[1], 0) / n];
  }
  return [cx / (3 * a), cy / (3 * a)];
}

/** Map an RTK fix string of the contracts to a class (anything else is `unknown`). */
export function rtkClass(fix: string | undefined): RtkClass {
  return fix === 'fixed' || fix === 'float' || fix === 'none' ? fix : 'unknown';
}

function isActivity(value: string): value is Activity {
  return (ACTIVITIES as readonly string[]).includes(value);
}

/** Local extent `[minX, minY, maxX, maxY]` of all map geometry, or null without geometry. */
export function localExtent(map: MapSnapshot): [number, number, number, number] | null {
  const points: [number, number][] = [
    ...(map.areas ?? []).flatMap((a) => a.boundary.map(xy)),
    ...(map.no_go_zones ?? []).flatMap((z) => z.boundary.map(xy)),
    ...(map.obstacles ?? []).flatMap((z) => z.boundary.map(xy)),
    ...(map.paths ?? []).flatMap((p) => p.points.map(xy)),
    ...(map.dock ? dockOutline(map.dock) : []),
  ];
  if (!points.length) return null;
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}

/**
 * Split the route into drawable segments: a new line after a time gap, and a new segment
 * whenever the age bucket (or, with the tint, the RTK class) changes. Adjacent segments share
 * their joint point, so the line stays continuous.
 */
export function routeSegments(
  route: readonly RoutePoint[],
  projection: Projection,
  options: { rtkTint?: boolean; gapMs?: number } = {},
): { segments: RouteSegment[]; points: number } {
  const gapMs = options.gapMs ?? 5 * 60_000;
  const points = route
    .map((p) => ({ p, t: Date.parse(p.observed_at) }))
    .filter(({ p, t }) => Number.isFinite(p.lat) && Number.isFinite(p.lon) && Number.isFinite(t))
    .sort((a, b) => a.t - b.t);
  const segments: RouteSegment[] = [];
  if (points.length < 2) return { segments, points: points.length };
  const first = points[0]?.t ?? 0;
  const span = Math.max(1, (points[points.length - 1]?.t ?? 0) - first);
  const ageOf = (t: number) => Math.min(3, Math.floor(((t - first) / span) * 4)) as RouteAge;

  let current: RouteSegment | null = null;
  let previous: { coord: MapCoord; t: number } | null = null;
  for (const { p, t } of points) {
    const coord = projection.fromWgs84(p.lat, p.lon);
    const age = ageOf(t);
    const rtk = options.rtkTint ? rtkClass(p.rtk_fix) : null;
    const inexact = p.rtk_fix === 'none';
    if (previous && t - previous.t > gapMs) {
      current = null; // a gap: the next point starts a new line
    } else if (previous) {
      if (!current || current.age !== age || current.rtk !== rtk || current.inexact !== inexact) {
        current = { points: [previous.coord], age, rtk, inexact };
        segments.push(current);
      }
      current.points.push(coord);
    }
    previous = { coord, t };
  }
  return { segments, points: points.length };
}

/** Build every layer of the map. */
export function buildLayers(input: LayerInput): MapLayers {
  const { map, state, projection } = input;
  const selected = input.selected ?? [];
  const ring = (points: readonly Point[]) => points.map((p) => projection.fromLocal(...xy(p)));

  const areas: AreaLayer[] = (map?.areas ?? []).map((area) => {
    const order = selected.indexOf(area.id);
    return {
      id: area.id,
      name: input.areaName(area),
      ring: ring(area.boundary),
      labelAt: projection.fromLocal(...centroid(area.boundary)),
      order: order >= 0 ? order : null,
    };
  });
  const zone = (z: { id: string; name?: string | null; boundary: readonly Point[] }) => ({
    id: z.id,
    name: z.name ?? null,
    ring: ring(z.boundary),
  });
  const dock: DockLayer | null = map?.dock
    ? {
        at: projection.fromLocal(map.dock.x, map.dock.y),
        outline: dockOutline(map.dock).map((c) => projection.fromLocal(c[0], c[1])),
        rotation: projection.localAngleToScreen(map.dock.heading_deg ?? 0),
      }
    : null;

  let mower: MowerLayer | null = null;
  const position = state?.position;
  if (state && position && Number.isFinite(position.lat) && Number.isFinite(position.lon)) {
    const activity: Activity = isActivity(state.activity) ? state.activity : 'unknown';
    const inexact = isPositionInexact(state);
    mower = {
      at: projection.fromWgs84(position.lat, position.lon),
      rotation:
        position.heading == null || !Number.isFinite(position.heading)
          ? null
          : projection.headingToScreen(position.heading),
      tone: activityTone(activity),
      hollow: !!input.stale || activity === 'offline' || inexact,
      inexact,
    };
  }

  const { segments, points } = routeSegments(input.route ?? [], projection, {
    rtkTint: input.rtkTint,
    gapMs: input.routeGapMs,
  });

  const extent = map ? localExtent(map) : null;
  let bounds: [MapCoord, MapCoord] | null = null;
  if (extent) {
    const corners = [
      projection.fromLocal(extent[0], extent[1]),
      projection.fromLocal(extent[2], extent[1]),
      projection.fromLocal(extent[2], extent[3]),
      projection.fromLocal(extent[0], extent[3]),
    ];
    const lats = corners.map((c) => c[0]);
    const lngs = corners.map((c) => c[1]);
    bounds = [
      [Math.min(...lats), Math.min(...lngs)],
      [Math.max(...lats), Math.max(...lngs)],
    ];
  } else if (mower) {
    bounds = [mower.at, mower.at];
  }

  return {
    areas,
    noGo: (map?.no_go_zones ?? []).map(zone),
    obstacles: (map?.obstacles ?? []).map(zone),
    paths: (map?.paths ?? []).map((p) => ({ id: p.id, points: ring(p.points) })),
    dock,
    mower,
    route: segments,
    routePoints: points,
    bounds,
  };
}

/** Toggle `id` in an ordered selection: remove it, or append it at the end. */
export function toggleSelection(selected: readonly string[], id: string): string[] {
  return selected.includes(id) ? selected.filter((s) => s !== id) : [...selected, id];
}
