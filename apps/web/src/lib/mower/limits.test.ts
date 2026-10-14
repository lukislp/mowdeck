import { describe, expect, it } from 'vitest';

import { bladeRange, clampTo, displayModel, speedRange } from './limits';

describe('bladeRange', () => {
  it('returns the reported range', () => {
    expect(bladeRange({ blade_height_range_mm: [25, 70] })).toEqual([25, 70]);
  });
  it('is null without a valid range', () => {
    expect(bladeRange(null)).toBeNull();
    expect(bladeRange({})).toBeNull();
    expect(bladeRange({ blade_height_range_mm: null })).toBeNull();
    expect(bladeRange({ blade_height_range_mm: [25] })).toBeNull();
    expect(bladeRange({ blade_height_range_mm: [70, 25] })).toBeNull();
  });
});

describe('speedRange', () => {
  it('uses the reported minimum and maximum', () => {
    expect(speedRange({ min_speed_mps: 0.2, max_speed_mps: 0.6 })).toEqual([0.2, 0.6]);
  });
  it('falls back to 0.1 without a minimum', () => {
    expect(speedRange({ max_speed_mps: 0.6 })).toEqual([0.1, 0.6]);
    expect(speedRange({ min_speed_mps: null, max_speed_mps: 1 })).toEqual([0.1, 1]);
  });
  it('is null when the maximum is unknown', () => {
    expect(speedRange({ min_speed_mps: 0.2 })).toBeNull();
    expect(speedRange({ max_speed_mps: null })).toBeNull();
    expect(speedRange(undefined)).toBeNull();
  });
  it('never lets the minimum exceed the maximum', () => {
    expect(speedRange({ min_speed_mps: 0.5, max_speed_mps: 0.3 })).toEqual([0.3, 0.3]);
  });
});

describe('clampTo', () => {
  it('limits the value to the range', () => {
    expect(clampTo(0.8, [0.2, 0.6])).toBe(0.6);
    expect(clampTo(0.1, [0.2, 0.6])).toBe(0.2);
    expect(clampTo(0.4, [0.2, 0.6])).toBe(0.4);
  });
  it('keeps missing values missing', () => {
    expect(clampTo(null, [0, 1])).toBeNull();
    expect(clampTo(undefined, [0, 1])).toBeNull();
  });
});

describe('displayModel', () => {
  it('prefers the capabilities model', () => {
    expect(displayModel({ model: ' LUBA 2 AWD 3000X ' }, { model: 'Luba VP' })).toBe(
      'LUBA 2 AWD 3000X',
    );
  });
  it('falls back to the fleet model', () => {
    expect(displayModel({ model: '  ' }, { model: 'Luba VP' })).toBe('Luba VP');
    expect(displayModel(null, { model: 'Luba VP' })).toBe('Luba VP');
  });
  it('is null when neither is known', () => {
    expect(displayModel(undefined, { model: null })).toBeNull();
    expect(displayModel(null, null)).toBeNull();
  });
});
