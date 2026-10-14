import { describe, expect, it } from 'vitest';
import { estimateMinutes, formatDuration } from './duration';

describe('estimateMinutes', () => {
  it('uses points x 1 m / speed with a path', () => {
    expect(estimateMinutes({ pointsTotal: 3600, speedMps: 0.5 })).toBe(120);
  });
  it('uses area / (width x speed) x 1.3 without a path', () => {
    // 1000 m2 / (0.4 * 0.5) = 5000 s * 1.3 = 108.33 min
    expect(estimateMinutes({ areaM2: 1000, cuttingWidthM: 0.4, speedMps: 0.5 })).toBeCloseTo(
      108.33,
      1,
    );
  });
  it('caps to 30 min and 6 h', () => {
    expect(estimateMinutes({ areaM2: 10 })).toBe(30);
    expect(estimateMinutes({ pointsTotal: 1_000_000, speedMps: 0.5 })).toBe(360);
  });
  it('falls back on speed and width, null without input', () => {
    expect(estimateMinutes({ areaM2: 500 })).not.toBeNull();
    expect(estimateMinutes({})).toBeNull();
    expect(estimateMinutes({ areaM2: 0, pointsTotal: null })).toBeNull();
  });
});

describe('formatDuration', () => {
  it('rounds to 5 minutes', () => {
    expect(formatDuration(80)).toBe('1 h 20 min');
    expect(formatDuration(45)).toBe('45 min');
    expect(formatDuration(120)).toBe('2 h');
    expect(formatDuration(82)).toBe('1 h 20 min');
  });
});
