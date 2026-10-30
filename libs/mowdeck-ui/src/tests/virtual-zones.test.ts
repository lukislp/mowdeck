import { describe, expect, it } from 'vitest';

import { fromWgs84, toWgs84 } from '../lib/map/geo';
import { createProjection } from '../lib/map/projection';
import type { LocalPoint } from '../lib/map/spot';
import {
  draftProblem,
  draftToRing,
  ringSelfIntersects,
  segmentsTouch,
  unionBounds,
  virtualZoneLayers,
  zoneDraftLayer,
} from '../lib/map/virtual-zones';

const REF = { origin_lat: 52.52045, origin_lon: 13.40495, rotation_deg: 0 };
const square = (x0: number, y0: number, x1: number, y1: number): LocalPoint[] => [
  [x0, y0],
  [x1, y0],
  [x1, y1],
  [x0, y1],
];
const geoRing = (points: LocalPoint[]) => points.map((p) => toWgs84(REF, p[0], p[1]));

describe('virtualZoneLayers', () => {
  const local = createProjection(REF, 'local');

  it('projects WGS84 rings, marks the current and selected zone and fits them', () => {
    const { zones, bounds } = virtualZoneLayers(
      [
        { id: 'a', name: 'Front', ring: geoRing(square(0, 0, 10, 10)) },
        { id: 'b', name: 'Back', ring: geoRing(square(20, 0, 30, 10)) },
      ],
      local,
      { current: 'b', selected: 'a' },
    );
    expect(zones.map((z) => [z.id, z.current, z.selected])).toEqual([
      ['a', false, true],
      ['b', true, false],
    ]);
    // local mode: [y, x]
    expect(zones[0]!.labelAt[0]).toBeCloseTo(5, 6);
    expect(zones[0]!.labelAt[1]).toBeCloseTo(5, 6);
    expect(bounds![0][0]).toBeCloseTo(0, 6);
    expect(bounds![1][1]).toBeCloseTo(30, 6);
  });

  it('skips the hidden zone (being edited) and broken rings', () => {
    const { zones, bounds } = virtualZoneLayers(
      [
        { id: 'a', name: 'Front', ring: geoRing(square(0, 0, 10, 10)) },
        { id: 'b', name: 'Broken', ring: [[52.5, 13.4]] },
      ],
      local,
      { hidden: 'a' },
    );
    expect(zones).toEqual([]);
    expect(bounds).toBeNull();
    expect(virtualZoneLayers(null, local).zones).toEqual([]);
  });

  it('draws in WGS84 unchanged in geographic mode', () => {
    const geo = createProjection(REF, 'geo');
    const ring = geoRing(square(0, 0, 4, 4));
    const { zones } = virtualZoneLayers([{ id: 'a', name: 'A', ring }], geo);
    expect(zones[0]!.ring[1]).toEqual(ring[1]);
  });
});

describe('drafts', () => {
  it('shows corners first and a ring from three on', () => {
    const local = createProjection(REF, 'local');
    expect(zoneDraftLayer(null, local)).toBeNull();
    const two = zoneDraftLayer(
      [
        [0, 0],
        [4, 0],
      ],
      local,
    )!;
    expect(two.ring).toEqual([]);
    expect(two.corners).toHaveLength(2);
    expect(zoneDraftLayer(square(0, 0, 4, 4), local)!.ring).toHaveLength(4);
  });

  it('converts a draft to [lat, lon] corners that round-trip to the centimetre', () => {
    const ring = draftToRing(REF, square(0, 0, 12.5, 8));
    expect(ring).toHaveLength(4);
    const back = fromWgs84(REF, ring[2]![0], ring[2]![1]);
    expect(back[0]).toBeCloseTo(12.5, 1);
    expect(back[1]).toBeCloseTo(8, 1);
  });

  it('names the first problem of a draft', () => {
    expect(draftProblem(square(0, 0, 4, 4).slice(0, 2))).toBe('corners');
    expect(
      draftProblem([
        [0, 0],
        [10, 10],
        [10, 0],
        [0, 10],
      ]),
    ).toBe('crossing');
    expect(draftProblem(square(0, 0, 0.5, 0.5))).toBe('small');
    expect(draftProblem(square(0, 0, 400, 400))).toBe('large');
    const many = Array.from({ length: 65 }, (_, i): LocalPoint => {
      const a = (i / 65) * 2 * Math.PI;
      return [10 * Math.cos(a), 10 * Math.sin(a)];
    });
    expect(draftProblem(many)).toBe('many');
    expect(draftProblem(square(0, 0, 4, 4))).toBeNull();
  });

  it('detects crossing and touching edges', () => {
    expect(segmentsTouch([0, 0], [10, 10], [0, 10], [10, 0])).toBe(true);
    expect(segmentsTouch([0, 0], [10, 0], [5, 0], [5, 10])).toBe(true);
    expect(segmentsTouch([0, 0], [10, 0], [0, 1], [10, 1])).toBe(false);
    expect(ringSelfIntersects(square(0, 0, 4, 4))).toBe(false);
  });
});

describe('unionBounds', () => {
  it('grows a box by another and passes either alone', () => {
    expect(
      unionBounds(
        [
          [0, 0],
          [1, 1],
        ],
        [
          [-1, 0.5],
          [0.5, 3],
        ],
      ),
    ).toEqual([
      [-1, 0],
      [1, 3],
    ]);
    expect(unionBounds(null, null)).toBeNull();
    expect(
      unionBounds(undefined, [
        [0, 0],
        [1, 1],
      ]),
    ).toEqual([
      [0, 0],
      [1, 1],
    ]);
  });
});
