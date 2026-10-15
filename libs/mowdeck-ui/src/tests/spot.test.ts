import { describe, expect, it } from 'vitest';

import { createProjection } from '../lib/map/projection';
import {
  circlePolygon,
  containingArea,
  distanceToSpot,
  polygonAreaM2,
  segmentDistance,
  spotCenter,
  spotComplete,
  spotLayer,
  spotPolygon,
  thinRing,
  type MapSpot,
} from '../lib/map/spot';

const AREAS = [
  {
    id: 'front',
    boundary: [
      [0, 0],
      [12, 0],
      [12, 8],
      [0, 8],
    ],
  },
  {
    id: 'back',
    boundary: [
      [0, 11],
      [10, 11],
      [10, 17],
      [0, 17],
    ],
  },
];

const CIRCLE: MapSpot = { kind: 'circle', center: [6, 4], radius_m: 1.5 };
const TRIANGLE: MapSpot = {
  kind: 'polygon',
  points: [
    [1, 1],
    [3, 1],
    [2, 3],
  ],
};

describe('circlePolygon', () => {
  it('puts every corner on the rim, counter-clockwise from east', () => {
    const ring = circlePolygon([6, 4], 1.5);
    expect(ring).toHaveLength(32);
    expect(ring[0]).toEqual([7.5, 4]);
    expect(ring[8]).toEqual([6, 5.5]);
    for (const [x, y] of ring) expect(Math.hypot(x - 6, y - 4)).toBeCloseTo(1.5, 1);
    expect(polygonAreaM2(ring)).toBeCloseTo(Math.PI * 1.5 * 1.5, 0);
  });

  it('never draws fewer than three corners', () => {
    expect(circlePolygon([0, 0], 1, 1)).toHaveLength(3);
  });
});

describe('spotPolygon and spotComplete', () => {
  it('turns a circle into a ring and keeps a drawn polygon of three or more corners', () => {
    expect(spotPolygon(CIRCLE)).toHaveLength(32);
    expect(spotPolygon(TRIANGLE)).toEqual(TRIANGLE.points);
    expect(spotComplete(CIRCLE)).toBe(true);
    expect(spotComplete(TRIANGLE)).toBe(true);
  });

  it('is empty without a spot or with fewer than three corners', () => {
    expect(spotPolygon(null)).toEqual([]);
    const two: MapSpot = {
      kind: 'polygon',
      points: [
        [0, 0],
        [1, 0],
      ],
    };
    expect(spotPolygon(two)).toEqual([]);
    expect(spotComplete(two)).toBe(false);
  });
});

describe('spotCenter', () => {
  it('is the circle centre or the vertex mean', () => {
    expect(spotCenter(CIRCLE)).toEqual([6, 4]);
    const [x, y] = spotCenter(TRIANGLE);
    expect(x).toBeCloseTo(2);
    expect(y).toBeCloseTo(5 / 3);
    expect(spotCenter({ kind: 'polygon', points: [] })).toEqual([0, 0]);
  });
});

describe('containingArea', () => {
  it('names the one area that holds every corner', () => {
    expect(containingArea(CIRCLE, AREAS)).toBe('front');
    expect(containingArea({ kind: 'circle', center: [5, 14], radius_m: 1 }, AREAS)).toBe('back');
  });

  it('is null across a boundary, outside the map, without a shape and without areas', () => {
    expect(containingArea({ kind: 'circle', center: [11.5, 4], radius_m: 1.5 }, AREAS)).toBeNull();
    expect(containingArea({ kind: 'circle', center: [5, 9.5], radius_m: 1 }, AREAS)).toBeNull();
    expect(containingArea({ kind: 'polygon', points: [[1, 1]] }, AREAS)).toBeNull();
    expect(containingArea(CIRCLE, null)).toBeNull();
    expect(containingArea(null, AREAS)).toBeNull();
  });
});

describe('distances', () => {
  it('measures the distance to a segment, clamped to its ends', () => {
    expect(segmentDistance([1, 1], [0, 0], [2, 0])).toBe(1);
    expect(segmentDistance([5, 0], [0, 0], [2, 0])).toBe(3);
    expect(segmentDistance([3, 4], [0, 0], [0, 0])).toBe(5);
  });

  it('is zero inside the spot and the distance to the rim or edge outside it', () => {
    expect(distanceToSpot([6, 4], CIRCLE)).toBe(0);
    expect(distanceToSpot([6, 6.5], CIRCLE)).toBeCloseTo(1);
    expect(distanceToSpot([2, 1.5], TRIANGLE)).toBe(0);
    expect(distanceToSpot([2, 0], TRIANGLE)).toBe(1);
    expect(distanceToSpot([0, 0], null)).toBe(Number.POSITIVE_INFINITY);
    expect(distanceToSpot([0, 0], { kind: 'polygon', points: [] })).toBe(Number.POSITIVE_INFINITY);
  });
});

describe('thinRing', () => {
  it('keeps short rings and strides long ones from the first corner', () => {
    expect(thinRing(TRIANGLE.points, 64)).toEqual(TRIANGLE.points);
    const ring = circlePolygon([0, 0], 1, 100);
    const thin = thinRing(ring, 10);
    expect(thin).toHaveLength(10);
    expect(thin[0]).toEqual(ring[0]);
    expect(thin[5]).toEqual(ring[50]);
  });
});

describe('spotLayer', () => {
  const projection = createProjection({ origin_lat: 48, origin_lon: 11 }, 'local');

  it('projects the ring, the centre and the bounds of a circle', () => {
    const layer = spotLayer(CIRCLE, projection)!;
    expect(layer.ring).toHaveLength(32);
    expect(layer.center).toEqual([4, 6]);
    expect(layer.corners).toEqual([]);
    expect(layer.bounds).toEqual([
      [2.5, 4.5],
      [5.5, 7.5],
    ]);
  });

  it('shows the corners of a polygon still being drawn, with bounds around them', () => {
    const layer = spotLayer(
      {
        kind: 'polygon',
        points: [
          [1, 1],
          [3, 2],
        ],
      },
      projection,
    )!;
    expect(layer.ring).toEqual([]);
    expect(layer.corners).toEqual([
      [1, 1],
      [2, 3],
    ]);
    expect(layer.center).toBeNull();
    expect(layer.bounds).toEqual([
      [1, 1],
      [2, 3],
    ]);
    expect(spotLayer({ kind: 'polygon', points: [] }, projection)!.bounds).toBeNull();
    expect(spotLayer(null, projection)).toBeNull();
  });
});
