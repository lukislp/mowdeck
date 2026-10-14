import { describe, expect, it } from 'vitest';

import { toWgs84, type GeoReference } from '../lib/map/geo';
import type { RoutePoint } from '../lib/map/layers';
import { createProjection } from '../lib/map/projection';
import { binRtk, cellClass, cellRing, rtkSummary } from '../lib/map/rtk';

const REF: GeoReference = { origin_lat: 48.137154, origin_lon: 11.576124, rotation_deg: 12.5 };
const T = '2026-10-10T08:00:00Z';

/** A route point at local metres `(x, y)` with the given fix. */
function at(x: number, y: number, rtk_fix?: string): RoutePoint {
  const [lat, lon] = toWgs84(REF, x, y);
  return { lat, lon, observed_at: T, rtk_fix };
}

const counts = (fixed: number, float: number, none: number, unknown = 0) => ({
  fixed,
  float,
  none,
  unknown,
});

describe('cellClass', () => {
  it('worst: any none wins, then float, then fixed', () => {
    expect(cellClass(counts(9, 0, 1), 'worst')).toBe('none');
    expect(cellClass(counts(9, 1, 0), 'worst')).toBe('float');
    expect(cellClass(counts(9, 0, 0), 'worst')).toBe('fixed');
  });

  it('majority: the most frequent class, ties go to the worse one', () => {
    expect(cellClass(counts(9, 0, 1), 'majority')).toBe('fixed');
    expect(cellClass(counts(2, 3, 1), 'majority')).toBe('float');
    expect(cellClass(counts(2, 2, 0), 'majority')).toBe('float');
    expect(cellClass(counts(1, 1, 1), 'majority')).toBe('none');
  });

  it('unknown only decides an otherwise empty cell', () => {
    expect(cellClass(counts(0, 0, 0, 5), 'worst')).toBe('unknown');
    expect(cellClass(counts(0, 0, 0, 5), 'majority')).toBe('unknown');
    expect(cellClass(counts(1, 0, 0, 5), 'majority')).toBe('fixed');
  });
});

describe('binRtk', () => {
  it('bins points into 1 m cells in the local frame (rotation applied)', () => {
    const cells = binRtk(
      [at(0.2, 0.2, 'fixed'), at(0.8, 0.7, 'fixed'), at(1.5, 0.5, 'float'), at(1.6, 0.4, 'none')],
      REF,
    );
    expect(cells.map((c) => [c.ix, c.iy, c.total, c.rtk])).toEqual([
      [0, 0, 2, 'fixed'],
      [1, 0, 2, 'none'],
    ]);
    expect(cells[1]?.counts).toEqual(counts(0, 1, 1));
  });

  it('uses the chosen cell size and aggregate', () => {
    const points = [at(0.2, 0.2, 'fixed'), at(1.5, 0.5, 'fixed'), at(1.6, 1.4, 'float')];
    const two = binRtk(points, REF, { size: 2, aggregate: 'majority' });
    expect(two).toHaveLength(1);
    expect(two[0]).toMatchObject({ ix: 0, iy: 0, size: 2, total: 3, rtk: 'fixed' });
    const half = binRtk(points, REF, { size: 0.5 });
    expect(half.map((c) => [c.ix, c.iy])).toEqual([
      [0, 0],
      [3, 1],
      [3, 2],
    ]);
  });

  it('handles negative coordinates, drops invalid points and sorts by row, then column', () => {
    const cells = binRtk(
      [at(-0.5, 2.5, 'fixed'), at(3.2, -1.1, 'none'), { lat: NaN, lon: 1, observed_at: T }],
      REF,
    );
    expect(cells.map((c) => [c.ix, c.iy, c.rtk])).toEqual([
      [3, -2, 'none'],
      [-1, 2, 'fixed'],
    ]);
  });

  it('falls back to 1 m for a non-positive size; unknown fixes count as unknown', () => {
    const cells = binRtk([at(0.5, 0.5), at(0.6, 0.5, 'bogus')], REF, { size: 0 });
    expect(cells).toHaveLength(1);
    expect(cells[0]).toMatchObject({ size: 1, rtk: 'unknown' });
    expect(cells[0]?.counts.unknown).toBe(2);
  });
});

describe('cellRing', () => {
  it('returns the four corners in map coordinates', () => {
    const local = createProjection(REF, 'local');
    const [cell] = binRtk([at(2.5, 3.5, 'fixed')], REF, { size: 2 });
    expect(cell).toBeDefined();
    const ring = cellRing(cell!, local);
    const expected = [
      [2, 2],
      [2, 4],
      [4, 4],
      [4, 2],
    ];
    ring.forEach((corner, i) => {
      expect(corner[0]).toBeCloseTo(expected[i]![0]!, 9);
      expect(corner[1]).toBeCloseTo(expected[i]![1]!, 9);
    });
    const geo = cellRing(cell!, createProjection(REF, 'geo'));
    expect(geo[0]?.[0]).toBeCloseTo(toWgs84(REF, 2, 2)[0], 12);
  });
});

describe('rtkSummary', () => {
  it('counts the shares over valid points', () => {
    const summary = rtkSummary([
      at(0, 0, 'fixed'),
      at(1, 0, 'fixed'),
      at(2, 0, 'float'),
      at(3, 0, 'none'),
      { lat: Infinity, lon: 0, observed_at: T, rtk_fix: 'fixed' },
    ]);
    expect(summary.total).toBe(4);
    expect(summary.counts).toEqual(counts(2, 1, 1));
    expect(summary.shares.fixed).toBe(0.5);
    expect(summary.shares.none).toBe(0.25);
  });

  it('is all zero without points', () => {
    expect(rtkSummary([]).shares).toEqual(counts(0, 0, 0));
  });
});
