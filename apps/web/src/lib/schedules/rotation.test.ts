import { describe, expect, it } from 'vitest';

import {
  angleText,
  lastAngle,
  nextManualAngle,
  normalised,
  rotationModeOf,
  stepSequence,
  stepped,
  validStep,
} from './rotation';

const at = (minutes: number) => new Date(Date.UTC(2026, 9, 9, 8, minutes)).toISOString();

describe('angle rotation', () => {
  it('normalises to half a turn like the Scheduler', () => {
    expect([0, 179.9, 180, 225, -30, 179.96].map(normalised)).toEqual([0, 179.9, 0, 45, 150, 0]);
  });

  it('steps from the last angle or starts at the base', () => {
    expect(stepped(null, 45, 30)).toBe(30);
    expect(stepped(165, 45, 0)).toBe(30);
    expect(stepSequence(null, 45, 30, 5)).toEqual([30, 75, 120, 165, 30]);
    expect(stepSequence(90, 90, 0, 3)).toEqual([0, 90, 0]);
  });

  it('accepts steps that change the angle', () => {
    expect([1, 45, 179].every(validStep)).toBe(true);
    expect([0, 0.5, 180, NaN, null, undefined].some(validStep)).toBe(false);
  });

  it('reads the mode of a schedule', () => {
    expect(rotationModeOf({ rotation_step_deg: 45, rotation_angles: [] })).toBe('step');
    expect(rotationModeOf({ rotation_step_deg: null, rotation_angles: [0, 90] })).toBe('list');
    expect(rotationModeOf({})).toBe('off');
  });

  it('continues from the area mowed last', () => {
    const areas = [
      { area_id: 'front', angle_deg: 10, used_at: at(0) },
      { area_id: 'back', angle_deg: 100, used_at: at(5) },
    ];
    expect(lastAngle(areas, ['front', 'back'])).toBe(100);
    expect(lastAngle(areas, ['front'])).toBe(10);
    expect(lastAngle(areas, ['pond'])).toBeNull();
  });

  it('works out the angle of a manual start', () => {
    const state = {
      areas: [
        {
          area_id: 'front',
          angle_deg: 150,
          used_at: at(0),
          source: 'manual' as const,
          schedule_id: null,
        },
      ],
    };
    expect(nextManualAngle(state, ['front'], 45, 0)).toBe(15);
    expect(nextManualAngle(state, ['back'], 45, 20)).toBe(20);
    expect(nextManualAngle(null, ['front'], 45, 20)).toBe(20);
    expect(nextManualAngle(state, ['front'], null, 0)).toBeNull();
    expect(nextManualAngle(state, [], 45, 0)).toBeNull();
  });

  it('prints whole degrees and tenths', () => {
    const format = (n: number, digits = 0) => n.toFixed(digits);
    expect(angleText(45, format)).toBe('45°');
    expect(angleText(22.5, format)).toBe('22.5°');
  });
});
