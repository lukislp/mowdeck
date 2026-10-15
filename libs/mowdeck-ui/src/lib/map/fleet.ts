/**
 * The layer model of `FleetMap`: every visible mower on one map. Pure functions, unit tested in
 * `src/tests/fleet.test.ts`; the component only draws the descriptors.
 *
 * Mowers of different sites have different local frames, so the fleet map always works in
 * WGS84 (`geo` projection, Leaflet `EPSG:3857`). Without tiles it draws a metric grid around a
 * fleet reference (the centre of the bounding box of all positions and map origins) whose step
 * follows the visible span (1, 2, 5 times a power of ten), so a garden and a fleet spread over
 * a region both get a readable grid. Each mower's map areas use that mower's own reference.
 */
import { ACTIVITIES, activityTone, type Activity, type ActivityTone } from '../types';

import type { GeoReference } from './geo';
import { buildLayers, type GridLine, type MapMowerState, type MapSnapshot } from './layers';
import { createProjection, type MapCoord, type Projection } from './projection';

/** The state colour family of a fleet marker. */
export type FleetTone = ActivityTone;

/** Legend order: what needs attention first. */
export const FLEET_TONES: readonly FleetTone[] = [
  'error',
  'mowing',
  'returning',
  'paused',
  'charging',
  'docked',
  'offline',
  'unknown',
];

/** One mower of the fleet map. */
export interface FleetMower {
  id: string;
  name: string;
  /** The latest state, or null before the first telemetry. */
  state: MapMowerState | null;
  /** The state is older than the stale threshold (drawn hollow). */
  stale?: boolean;
  /** The mower's map snapshot, once loaded (areas are drawn lazily). */
  map?: MapSnapshot | null;
}

export interface FleetMarker {
  id: string;
  name: string;
  at: MapCoord;
  tone: FleetTone;
  /** Screen rotation of the chevron, or null without a heading (drawn as a dot). */
  rotation: number | null;
  /** Stale or offline: drawn hollow and dashed. */
  hollow: boolean;
}

export interface FleetLegendEntry {
  tone: FleetTone;
  count: number;
}

export interface FleetArea {
  /** `<mower id>:<area id>`. */
  key: string;
  mowerId: string;
  ring: MapCoord[];
}

export interface FleetDock {
  mowerId: string;
  at: MapCoord;
  rotation: number;
}

function isActivity(value: string): value is Activity {
  return (ACTIVITIES as readonly string[]).includes(value);
}

function validPosition(state: MapMowerState | null): { lat: number; lon: number } | null {
  const p = state?.position;
  return p && Number.isFinite(p.lat) && Number.isFinite(p.lon) ? p : null;
}

/** The tone of a mower: its activity's family, `unknown` without a state. */
export function fleetTone(state: MapMowerState | null): FleetTone {
  if (!state) return 'unknown';
  return activityTone(isActivity(state.activity) ? state.activity : 'unknown');
}

/** Counts per tone in legend order (tones without mowers are left out). */
export function fleetLegend(mowers: readonly FleetMower[]): FleetLegendEntry[] {
  const counts = new Map<FleetTone, number>();
  for (const mower of mowers) {
    const tone = fleetTone(mower.state);
    counts.set(tone, (counts.get(tone) ?? 0) + 1);
  }
  return FLEET_TONES.filter((tone) => counts.has(tone)).map((tone) => ({
    tone,
    count: counts.get(tone) ?? 0,
  }));
}

/** Mowers without a known position (listed beside the map, not drawn on it). */
export function unplaced(mowers: readonly FleetMower[]): FleetMower[] {
  return mowers.filter((mower) => validPosition(mower.state) === null);
}

/** The fleet reference: the centre of the bounding box of all positions and map origins. */
export function fleetReference(mowers: readonly FleetMower[]): GeoReference | null {
  const points: [number, number][] = [];
  for (const mower of mowers) {
    const p = validPosition(mower.state);
    if (p) points.push([p.lat, p.lon]);
    if (mower.map) points.push([mower.map.reference.origin_lat, mower.map.reference.origin_lon]);
  }
  if (!points.length) return null;
  const lats = points.map((p) => p[0]);
  const lons = points.map((p) => p[1]);
  return {
    origin_lat: (Math.min(...lats) + Math.max(...lats)) / 2,
    origin_lon: (Math.min(...lons) + Math.max(...lons)) / 2,
  };
}

/** One marker per mower with a valid position. */
export function fleetMarkers(mowers: readonly FleetMower[], projection: Projection): FleetMarker[] {
  const markers: FleetMarker[] = [];
  for (const mower of mowers) {
    const p = validPosition(mower.state);
    if (!p || !mower.state) continue;
    const heading = mower.state.position?.heading;
    const tone = fleetTone(mower.state);
    markers.push({
      id: mower.id,
      name: mower.name,
      at: projection.fromWgs84(p.lat, p.lon),
      tone,
      rotation:
        heading == null || !Number.isFinite(heading) ? null : projection.headingToScreen(heading),
      hollow: !!mower.stale || tone === 'offline',
    });
  }
  return markers;
}

/** The areas and docks of every mower whose map is loaded, in WGS84. */
export function fleetGeometry(
  mowers: readonly FleetMower[],
  areaName: (area: { id: string; name?: string | null }) => string = (a) => a.name ?? a.id,
): { areas: FleetArea[]; docks: FleetDock[] } {
  const areas: FleetArea[] = [];
  const docks: FleetDock[] = [];
  for (const mower of mowers) {
    if (!mower.map) continue;
    const projection = createProjection(mower.map.reference, 'geo');
    const layers = buildLayers({ map: mower.map, projection, areaName });
    for (const area of layers.areas) {
      areas.push({ key: `${mower.id}:${area.id}`, mowerId: mower.id, ring: area.ring });
    }
    if (layers.dock)
      docks.push({ mowerId: mower.id, at: layers.dock.at, rotation: layers.dock.rotation });
  }
  return { areas, docks };
}

/** South-west and north-east corner around markers and areas, or null when empty. */
export function fleetBounds(
  markers: readonly FleetMarker[],
  areas: readonly FleetArea[] = [],
): [MapCoord, MapCoord] | null {
  const coords: MapCoord[] = [...markers.map((m) => m.at), ...areas.flatMap((a) => a.ring)];
  if (!coords.length) return null;
  const lats = coords.map((c) => c[0]);
  const lngs = coords.map((c) => c[1]);
  return [
    [Math.min(...lats), Math.min(...lngs)],
    [Math.max(...lats), Math.max(...lngs)],
  ];
}

/** The grid step in metres for a span: 1, 2 or 5 times a power of ten, about `lines` lines. */
export function gridStep(spanMetres: number, lines = 10): number {
  if (!Number.isFinite(spanMetres) || spanMetres <= 0) return 1;
  const raw = spanMetres / lines;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((f) => f * pow).find((v) => v >= raw) ?? 10 * pow;
  return Math.max(1, step);
}

/**
 * The metric grid over a local extent `[minX, minY, maxX, maxY]` (metres around `projection`'s
 * reference), with a step for the extent's longer side; every fifth line is major.
 */
export function fleetGrid(
  extent: [number, number, number, number],
  projection: Projection,
  lines = 10,
): GridLine[] {
  const [x0, y0, x1, y1] = extent;
  const step = gridStep(Math.max(x1 - x0, y1 - y0), lines);
  const major = step * 5;
  const minX = Math.floor(x0 / step) * step;
  const minY = Math.floor(y0 / step) * step;
  const maxX = Math.ceil(x1 / step) * step;
  const maxY = Math.ceil(y1 / step) * step;
  const isMajor = (v: number) => Math.abs(v / major - Math.round(v / major)) < 1e-9;
  const grid: GridLine[] = [];
  for (let x = minX; x <= maxX + step / 2; x += step) {
    grid.push({
      from: projection.fromLocal(x, minY),
      to: projection.fromLocal(x, maxY),
      major: isMajor(x),
    });
  }
  for (let y = minY; y <= maxY + step / 2; y += step) {
    grid.push({
      from: projection.fromLocal(minX, y),
      to: projection.fromLocal(maxX, y),
      major: isMajor(y),
    });
  }
  return grid;
}
