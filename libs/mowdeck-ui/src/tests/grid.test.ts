import { describe, expect, it } from 'vitest';

import { gridMultiples, gridSpacing } from '../lib/map/grid';

describe('gridSpacing', () => {
  it('draws 1 m and 5 m lines when zoomed in', () => {
    expect(gridSpacing(32)).toEqual({ minor: 1, major: 5 });
    expect(gridSpacing(9)).toEqual({ minor: 1, major: 5 });
  });

  it('thins out when zoomed out', () => {
    expect(gridSpacing(4)).toEqual({ minor: 5, major: 10 });
    expect(gridSpacing(1)).toEqual({ minor: 10, major: 50 });
    expect(gridSpacing(0.25)).toEqual({ minor: 50, major: 100 });
    expect(gridSpacing(0.125)).toEqual({ minor: 100, major: 500 });
  });

  it('never packs minor lines closer than 9 px', () => {
    for (let z = -3; z <= 8; z += 0.25) {
      const scale = 2 ** z;
      const { minor, major } = gridSpacing(scale);
      expect(minor * scale).toBeGreaterThanOrEqual(9);
      expect(major % minor).toBe(0);
    }
  });

  it('tolerates invalid scales', () => {
    expect(gridSpacing(0).minor).toBeGreaterThan(0);
    expect(gridSpacing(Number.NaN).minor).toBeGreaterThan(0);
  });
});

describe('gridMultiples', () => {
  it('lists multiples of the step in range, negative included', () => {
    expect(gridMultiples(-2.5, 3, 1)).toEqual([-2, -1, 0, 1, 2, 3]);
    expect(gridMultiples(0.5, 0.9, 1)).toEqual([]);
  });
});
