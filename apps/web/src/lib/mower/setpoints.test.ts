import { describe, expect, it } from 'vitest';

import { initialSetpoints, setpointCommands, stepSetpoint } from './setpoints';

const BLADE = [25, 70] as const;
const SPEED = [0.2, 0.6] as const;

describe('initialSetpoints', () => {
  it('takes the reported values within the ranges', () => {
    expect(initialSetpoints(55, 0.4, BLADE, SPEED)).toEqual({ bladeMm: 55, speedMps: 0.4 });
    expect(initialSetpoints(90, 0.05, BLADE, SPEED)).toEqual({ bladeMm: 70, speedMps: 0.2 });
    expect(initialSetpoints(55.4, 0.333, BLADE, SPEED)).toEqual({ bladeMm: 55, speedMps: 0.33 });
  });

  it('stays unknown without a value or a range', () => {
    expect(initialSetpoints(null, undefined, BLADE, SPEED)).toEqual({
      bladeMm: null,
      speedMps: null,
    });
    expect(initialSetpoints(55, 0.4, null, null)).toEqual({ bladeMm: null, speedMps: null });
  });
});

describe('stepSetpoint', () => {
  it('steps within the range and avoids float noise', () => {
    expect(stepSetpoint(55, 1, 5, BLADE)).toBe(60);
    expect(stepSetpoint(70, 1, 5, BLADE)).toBe(70);
    expect(stepSetpoint(25, -1, 5, BLADE)).toBe(25);
    expect(stepSetpoint(0.3, 1, 0.1, SPEED, 2)).toBe(0.4);
    expect(stepSetpoint(0.25, -1, 0.1, SPEED, 2)).toBe(0.2);
    expect(stepSetpoint(null, 1, 5, BLADE)).toBe(25);
  });
});

describe('setpointCommands', () => {
  const initial = { bladeMm: 55, speedMps: 0.4 };

  it('sends nothing when nothing changed', () => {
    expect(setpointCommands({ bladeMm: 55, speedMps: 0.4 }, initial)).toEqual([]);
    expect(setpointCommands({ bladeMm: 55, speedMps: 0.4000001 }, initial)).toEqual([]);
  });

  it('sends only the changed values, blade height first', () => {
    expect(setpointCommands({ bladeMm: 60, speedMps: 0.4 }, initial)).toEqual([
      { kind: 'set_blade_height', mm: 60 },
    ]);
    expect(setpointCommands({ bladeMm: 55, speedMps: 0.5 }, initial)).toEqual([
      { kind: 'set_speed', mps: 0.5 },
    ]);
    expect(setpointCommands({ bladeMm: 45, speedMps: 0.3 }, initial)).toEqual([
      { kind: 'set_blade_height', mm: 45 },
      { kind: 'set_speed', mps: 0.3 },
    ]);
  });

  it('sends a value chosen where none was known, never an unknown one', () => {
    expect(
      setpointCommands({ bladeMm: 25, speedMps: 0.2 }, { bladeMm: null, speedMps: null }),
    ).toEqual([
      { kind: 'set_blade_height', mm: 25 },
      { kind: 'set_speed', mps: 0.2 },
    ]);
    expect(setpointCommands({ bladeMm: null, speedMps: null }, initial)).toEqual([]);
  });
});
