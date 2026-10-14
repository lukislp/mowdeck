import { describe, expect, it } from 'vitest';

import { toWgs84 } from '../lib/map/geo';
import {
  buildLayers,
  centroid,
  dockOutline,
  isPositionInexact,
  localExtent,
  routeSegments,
  rtkClass,
  toggleSelection,
  type MapSnapshot,
  type RoutePoint,
} from '../lib/map/layers';
import { createProjection } from '../lib/map/projection';

/** The simulator's default map (libs/mowdeck-control `default_map()`). */
const MAP: MapSnapshot = {
  reference: { origin_lat: 48.137154, origin_lon: 11.576124, rotation_deg: 12.5 },
  areas: [
    {
      id: 'front',
      name: 'Front lawn',
      boundary: [
        [0, 0],
        [12, 0],
        [12, 8],
        [0, 8],
      ],
    },
    {
      id: 'back',
      name: 'Back lawn',
      boundary: [
        [0, 11],
        [10, 11],
        [10, 17],
        [0, 17],
      ],
    },
  ],
  no_go_zones: [
    {
      id: 'flowerbed',
      name: 'Flower bed',
      boundary: [
        [9, 5.5],
        [12, 5.5],
        [12, 8],
        [9, 8],
      ],
    },
  ],
  obstacles: [],
  paths: [
    {
      id: 'side-path',
      points: [
        [1, 8],
        [1, 11],
      ],
    },
  ],
  dock: { x: -1, y: 1, heading_deg: 0 },
  revision: 'sim-1',
};

const local = createProjection(MAP.reference, 'local');
const geo = createProjection(MAP.reference, 'geo');
const areaName = (a: { id: string; name?: string | null }) => a.name ?? a.id;

function at(x: number, y: number, seconds: number, rtk = 'fixed'): RoutePoint {
  const [lat, lon] = toWgs84(MAP.reference, x, y);
  return {
    lat,
    lon,
    observed_at: new Date(Date.UTC(2026, 9, 9, 10, 0, seconds)).toISOString(),
    rtk_fix: rtk,
  };
}

describe('buildLayers', () => {
  it('draws the two areas with labels at their centroids, the zone, the path and the dock', () => {
    const layers = buildLayers({ map: MAP, projection: local, areaName });
    expect(layers.areas.map((a) => a.id)).toEqual(['front', 'back']);
    expect(layers.areas[0]?.name).toBe('Front lawn');
    expect(layers.areas[0]?.labelAt).toEqual([4, 6]);
    expect(layers.areas[1]?.labelAt).toEqual([14, 5]);
    expect(layers.areas[0]?.ring).toHaveLength(4);
    expect(layers.noGo.map((z) => z.id)).toEqual(['flowerbed']);
    expect(layers.paths[0]?.points).toEqual([
      [8, 1],
      [11, 1],
    ]);
    expect(layers.dock?.at).toEqual([1, -1]);
    expect(layers.dock?.rotation).toBe(90);
    expect(layers.dock?.outline).toHaveLength(4);
    expect(layers.mower).toBeNull();
    expect(layers.route).toEqual([]);
  });

  it('marks selected areas with their start order', () => {
    const layers = buildLayers({
      map: MAP,
      projection: local,
      areaName,
      selected: ['back', 'front'],
    });
    expect(layers.areas.map((a) => a.order)).toEqual([1, 0]);
    const none = buildLayers({ map: MAP, projection: local, areaName, selected: ['back'] });
    expect(none.areas.map((a) => a.order)).toEqual([null, 0]);
  });

  it('bounds cover all geometry including the dock', () => {
    const layers = buildLayers({ map: MAP, projection: local, areaName });
    expect(layers.bounds).toEqual([
      [0, -1.6],
      [17, 12],
    ]);
  });

  it('dockOutline: right angles, 1.2 x 0.9 m, front edge towards the heading', () => {
    type P = [number, number];
    type Quad = [P, P, P, P];
    const d = (a: P, b: P) => Math.hypot(a[0] - b[0], a[1] - b[1]);
    const c0 = dockOutline({ x: 0, y: 0, heading_deg: 0 }, 1.2, 0.9) as Quad;
    expect(c0[0]).toEqual([-0.6, -0.45]);
    expect(c0[1]).toEqual([0.6, -0.45]);
    expect(c0[2]).toEqual([0.6, 0.45]);
    const c90 = dockOutline({ x: 2, y: 3, heading_deg: 90 }, 1.2, 0.9) as Quad;
    // front edge (corners 1, 2) lies at y = 3.6, across x = 2 +- 0.45
    expect(c90[1][1]).toBeCloseTo(3.6);
    expect(c90[2][1]).toBeCloseTo(3.6);
    expect(c90[1][0]).toBeCloseTo(2.45);
    expect(c90[2][0]).toBeCloseTo(1.55);
    for (const [p0, p1, p2] of [c0, c90]) {
      expect(d(p0, p1)).toBeCloseTo(1.2);
      expect(d(p1, p2)).toBeCloseTo(0.9);
      const dot = (p1[0] - p0[0]) * (p2[0] - p1[0]) + (p1[1] - p0[1]) * (p2[1] - p1[1]);
      expect(dot).toBeCloseTo(0);
    }
  });

  it('places the mower from its WGS84 position with the state tone and heading', () => {
    const [lat, lon] = toWgs84(MAP.reference, 5, 3);
    const layers = buildLayers({
      map: MAP,
      projection: local,
      areaName,
      state: { activity: 'mowing', position: { lat, lon, heading: 90 } },
    });
    expect(layers.mower?.at[0]).toBeCloseTo(3, 6);
    expect(layers.mower?.at[1]).toBeCloseTo(5, 6);
    expect(layers.mower?.rotation).toBeCloseTo(102.5);
    expect(layers.mower?.tone).toBe('mowing');
    expect(layers.mower?.hollow).toBe(false);
  });

  it('draws a mower without an RTK fix hollow, except at the dock', () => {
    const position = { lat: 48.137154, lon: 11.576124 };
    const draw = (state: object) =>
      buildLayers({
        map: MAP,
        projection: local,
        areaName,
        state: { activity: 'mowing', position, ...state },
      }).mower;
    expect(draw({ rtk_fix: 'none', position_source: 'gnss' })).toMatchObject({
      hollow: true,
      inexact: true,
    });
    expect(draw({ rtk_fix: 'fixed', position_source: 'rtk' })).toMatchObject({
      hollow: false,
      inexact: false,
    });
    expect(draw({ rtk_fix: 'none', position_source: 'dock' })).toMatchObject({
      hollow: false,
      inexact: false,
    });
    expect(draw({})).toMatchObject({ hollow: false, inexact: false });
    expect(isPositionInexact({ rtk_fix: 'none' })).toBe(true);
    expect(isPositionInexact(null)).toBe(false);
  });

  it('draws a stale or offline mower hollow, an unknown heading as a dot', () => {
    const state = { activity: 'idle', position: { lat: 48.137154, lon: 11.576124 } };
    const stale = buildLayers({ map: MAP, projection: local, areaName, state, stale: true });
    expect(stale.mower?.hollow).toBe(true);
    expect(stale.mower?.rotation).toBeNull();
    expect(stale.mower?.tone).toBe('docked');
    const offline = buildLayers({
      map: MAP,
      projection: local,
      areaName,
      state: { ...state, activity: 'offline' },
    });
    expect(offline.mower?.hollow).toBe(true);
    const odd = buildLayers({
      map: MAP,
      projection: local,
      areaName,
      state: { ...state, activity: 'teleporting' },
    });
    expect(odd.mower?.tone).toBe('unknown');
  });

  it('has no mower without a position and bounds around the mower without a map', () => {
    expect(
      buildLayers({ map: MAP, projection: local, areaName, state: { activity: 'docked' } }).mower,
    ).toBeNull();
    const layers = buildLayers({
      map: null,
      projection: geo,
      areaName,
      state: { activity: 'mowing', position: { lat: 48.1, lon: 11.5, heading: null } },
    });
    expect(layers.bounds).toEqual([
      [48.1, 11.5],
      [48.1, 11.5],
    ]);
    expect(layers.areas).toEqual([]);
  });

  it('converts local geometry to WGS84 in the geo projection', () => {
    const layers = buildLayers({ map: MAP, projection: geo, areaName });
    const [lat, lon] = toWgs84(MAP.reference, 12, 0);
    expect(layers.areas[0]?.ring[1]).toEqual([lat, lon]);
    expect(layers.dock?.rotation).toBeCloseTo(77.5);
  });
});

describe('routeSegments', () => {
  it('needs two points for a line and ignores invalid points', () => {
    expect(routeSegments([at(0, 0, 0)], local).segments).toEqual([]);
    const bad = { lat: Number.NaN, lon: 1, observed_at: '2026-10-09T10:00:00Z' };
    expect(routeSegments([at(0, 0, 0), bad], local).points).toBe(1);
  });

  it('fades older parts in four age steps and keeps the line continuous', () => {
    const route = Array.from({ length: 9 }, (_, i) => at(i, 0, i * 10));
    const { segments, points } = routeSegments(route, local);
    expect(points).toBe(9);
    expect(segments.map((s) => s.age)).toEqual([0, 1, 2, 3]);
    for (let i = 1; i < segments.length; i += 1) {
      const previous = segments[i - 1]?.points ?? [];
      expect(segments[i]?.points[0]).toEqual(previous[previous.length - 1]);
    }
    expect(segments.every((s) => s.rtk === null)).toBe(true);
  });

  it('sorts by time and splits at gaps', () => {
    const route = [at(2, 0, 20), at(0, 0, 0), at(1, 0, 10), at(5, 5, 1000), at(6, 5, 1010)];
    const { segments } = routeSegments(route, local, { gapMs: 60_000 });
    const first = segments[0]?.points ?? [];
    expect(first[0]?.[1]).toBeCloseTo(0, 6);
    // The gap between 20 s and 1000 s starts a new line: no segment joins (2, 0) and (5, 5).
    for (const segment of segments) {
      const xs = segment.points.map((p) => Math.round(p[1]));
      expect(xs.includes(2) && xs.includes(5)).toBe(false);
    }
  });

  it('splits the route where the fix drops to none and marks those segments inexact', () => {
    const route = [
      at(0, 0, 0, 'fixed'),
      at(1, 0, 1, 'fixed'),
      at(2, 0, 2, 'none'),
      at(3, 0, 3, 'none'),
      at(4, 0, 4, 'fixed'),
    ];
    const { segments } = routeSegments(route, local); // no tint: the dash does not need it
    // The segment ending at x = 2 and x = 3 is dashed, the fixed ones are not (age buckets may
    // split a run further).
    const dashed = segments.filter((s) => s.inexact);
    expect(dashed.length).toBeGreaterThan(0);
    for (const segment of dashed) {
      const xs = segment.points.map((p) => Math.round(p[1]));
      expect(Math.max(...xs)).toBeGreaterThanOrEqual(2);
      expect(Math.max(...xs)).toBeLessThanOrEqual(3);
    }
    expect(segments.some((s) => !s.inexact)).toBe(true);
    expect(segments.every((s) => s.rtk === null)).toBe(true);
  });

  it('tints by RTK quality when asked', () => {
    const route = [
      at(0, 0, 0, 'fixed'),
      at(1, 0, 1, 'fixed'),
      at(2, 0, 2, 'float'),
      at(3, 0, 3, 'none'),
    ];
    const { segments } = routeSegments(route, local, { rtkTint: true });
    expect(segments.map((s) => s.rtk)).toContain('float');
    expect(segments.map((s) => s.rtk)).toContain('none');
    expect(rtkClass('weird')).toBe('unknown');
  });
});

describe('helpers', () => {
  it('centroid of a rectangle and of a degenerate ring', () => {
    expect(
      centroid([
        [0, 0],
        [4, 0],
        [4, 2],
        [0, 2],
      ]),
    ).toEqual([2, 1]);
    expect(
      centroid([
        [0, 0],
        [2, 0],
        [4, 0],
      ]),
    ).toEqual([2, 0]);
  });

  it('extent of an empty map is null', () => {
    expect(localExtent({ reference: MAP.reference })).toBeNull();
  });

  it('toggles a selection in order', () => {
    expect(toggleSelection([], 'front')).toEqual(['front']);
    expect(toggleSelection(['front'], 'back')).toEqual(['front', 'back']);
    expect(toggleSelection(['front', 'back'], 'front')).toEqual(['back']);
  });
});
