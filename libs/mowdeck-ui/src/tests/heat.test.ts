import { describe, expect, it } from 'vitest';

import type { GeoReference } from '../lib/map/geo';
import {
  DBM_MAX,
  DBM_MIN,
  FIX_GOOD,
  FIX_WEAK,
  areasOfGap,
  gapOutlines,
  heatCells,
  heatRing,
  heatScale,
  heatStop,
  pointInPolygon,
} from '../lib/map/heat';
import { createProjection } from '../lib/map/projection';

const REF: GeoReference = { origin_lat: 48.137154, origin_lon: 11.576124, rotation_deg: 0 };
const local = createProjection(REF, 'local');

describe('heatScale', () => {
  it('coverage runs from ink-muted to the mowing green in five steps', () => {
    const scale = heatScale('ratio');
    expect(scale.stops).toHaveLength(5);
    expect(scale.stops[0]!.color).toBe('var(--md-color-ink-muted)');
    expect(scale.stops[4]!.color).toBe('var(--md-state-mowing)');
    expect(scale.stops[2]!.color).toContain('color-mix');
    expect(heatStop(scale, 0)).toBe(0);
    expect(heatStop(scale, 0.19)).toBe(0);
    expect(heatStop(scale, 0.2)).toBe(1);
    expect(heatStop(scale, 0.83)).toBe(4);
    expect(heatStop(scale, 1)).toBe(4);
  });

  it('RTK uses the fix tokens with the 50 % and 90 % thresholds', () => {
    const scale = heatScale('fix');
    expect(scale.stops.map((s) => s.key)).toEqual(['none', 'float', 'fixed']);
    expect(scale.stops.map((s) => s.color)).toEqual([
      'var(--md-rtk-none)',
      'var(--md-rtk-float)',
      'var(--md-rtk-fixed)',
    ]);
    expect(heatStop(scale, FIX_WEAK - 0.01)).toBe(0);
    expect(heatStop(scale, FIX_WEAK)).toBe(1);
    expect(heatStop(scale, FIX_GOOD)).toBe(2);
  });

  it('signal has five 10 dB steps from -90 to -40, weak to strong', () => {
    const scale = heatScale('dbm');
    expect(scale.stops.map((s) => s.key)).toEqual(['weak', 'poor', 'fair', 'good', 'strong']);
    expect(scale.stops.slice(1).map((s) => s.from)).toEqual([-80, -70, -60, -50]);
    expect(DBM_MIN).toBe(-90);
    expect(DBM_MAX).toBe(-40);
    expect(heatStop(scale, -95)).toBe(0);
    expect(heatStop(scale, -80)).toBe(1);
    expect(heatStop(scale, -65)).toBe(2);
    expect(heatStop(scale, -30)).toBe(4);
    expect(scale.stops[0]!.color).toBe('var(--md-state-error)');
    expect(scale.stops[4]!.color).toBe('var(--md-state-mowing)');
  });

  it('reads an unknown kind as a ratio and a non-finite value as the first step', () => {
    expect(heatScale('nope').kind).toBe('ratio');
    expect(heatStop(heatScale('dbm'), Number.NaN)).toBe(0);
  });
});

describe('heatCells', () => {
  it('turns cell indexes into rectangles of the answered cell size', () => {
    const rects = heatCells({
      cellM: 2,
      legend: { kind: 'ratio' },
      cells: [
        { x: 12, y: -3, value: 0.83, n: 17 },
        { x: 0, y: 0, value: 0.1 },
      ],
    });
    expect(rects).toHaveLength(2);
    expect(rects[0]).toMatchObject({ x0: 24, y0: -6, x1: 26, y1: -4, n: 17, stop: 4 });
    expect(rects[0]!.color).toBe('var(--md-state-mowing)');
    expect(rects[1]).toMatchObject({ x0: 0, y0: 0, x1: 2, y1: 2, n: 0, stop: 0 });
  });

  it('skips broken cells and empty input', () => {
    expect(heatCells(null)).toEqual([]);
    expect(
      heatCells({ cellM: 0, legend: { kind: 'fix' }, cells: [{ x: 0, y: 0, value: 1 }] }),
    ).toEqual([]);
    const rects = heatCells({
      cellM: 1,
      legend: { kind: 'dbm' },
      cells: [
        { x: Number.NaN, y: 0, value: -50 },
        { x: 1, y: 1, value: Number.POSITIVE_INFINITY },
        { x: 2, y: 2, value: -55 },
      ],
    });
    expect(rects.map((r) => r.ix)).toEqual([2]);
    expect(rects[0]!.stop).toBe(3);
  });

  it('projects a rectangle counter-clockwise from the south-west', () => {
    const [rect] = heatCells({
      cellM: 1,
      legend: { kind: 'ratio' },
      cells: [{ x: 1, y: 2, value: 1 }],
    });
    expect(heatRing(rect!, local)).toEqual([
      [2, 1],
      [2, 2],
      [3, 2],
      [3, 1],
    ]);
  });
});

describe('gapOutlines', () => {
  it('keeps the answer index, area, centre and bounds', () => {
    const outlines = gapOutlines(
      [
        { polygon: [[0, 0]], area_m2: 0 },
        {
          polygon: [
            [2, 2],
            [4, 2],
            [4, 3],
            [2, 3],
          ],
          area_m2: 2,
        },
      ],
      local,
    );
    expect(outlines).toHaveLength(1);
    const [gap] = outlines;
    expect(gap!.index).toBe(1);
    expect(gap!.areaM2).toBe(2);
    expect(gap!.centre).toEqual([3, 2.5]);
    expect(gap!.centreAt).toEqual([2.5, 3]);
    expect(gap!.bounds).toEqual([
      [2, 2],
      [3, 4],
    ]);
  });

  it('is empty without gaps', () => {
    expect(gapOutlines(null, local)).toEqual([]);
  });
});

describe('areasOfGap', () => {
  const square = [
    [0, 0],
    [10, 0],
    [10, 10],
    [0, 10],
  ];

  it('finds the areas around the gap centre', () => {
    expect(pointInPolygon(5, 5, square)).toBe(true);
    expect(pointInPolygon(15, 5, square)).toBe(false);
    const gap = {
      polygon: [
        [4, 4],
        [6, 4],
        [6, 6],
      ],
      area_m2: 2,
    };
    expect(
      areasOfGap(gap, [
        { id: 'front', boundary: square },
        { id: 'back', boundary: square.map(([x, y]) => [x! + 20, y!]) },
      ]),
    ).toEqual(['front']);
    expect(areasOfGap({ polygon: [], area_m2: 0 }, [{ id: 'front', boundary: square }])).toEqual(
      [],
    );
  });
});
