import { describe, expect, it } from 'vitest';

import {
  barGeometry,
  batteryExtremes,
  bladeMeter,
  batteryPath,
  cycleCount,
  batteryQuery,
  dayRange,
  durationParts,
  errorRows,
  fillDays,
  lifetimeTotals,
  thresholdLevel,
  thresholdShare,
  type BatteryPoint,
  type HealthDay,
} from './series';

function day(dayKey: string, blade: number, distance: number): HealthDay {
  return {
    day: dayKey,
    blade_hours: blade,
    distance_m: distance,
    mowing_hours: blade,
    battery_cycles: 0,
    charge_sessions: 0,
    errors: {},
    jobs: 0,
    online_hours: 0,
  };
}

function point(at: string, percent: number, min = percent, max = percent): BatteryPoint {
  return { at, battery_percent: percent, battery_min: min, battery_max: max, charging: false };
}

describe('health series', () => {
  it('covers the last 30 UTC days and fills gaps with zero', () => {
    const end = new Date('2026-10-10T23:30:00Z');
    expect(dayRange(end)).toEqual({ from: '2026-09-11', to: '2026-10-10' });
    const days = fillDays([day('2026-10-10', 1.5, 1200), day('2026-09-11', 0.25, 80)], end);
    expect(days).toHaveLength(30);
    expect(days[0]).toEqual({
      day: '2026-09-11',
      bladeHours: 0.25,
      distanceM: 80,
      mowingHours: 0.25,
    });
    expect(days[29]).toMatchObject({ day: '2026-10-10', bladeHours: 1.5 });
    expect(days[15]).toMatchObject({ bladeHours: 0, distanceM: 0 });
  });

  it('scales bars to the peak and keeps short days visible', () => {
    const bars = barGeometry([0, 1, 4, 0.01], 40, 20);
    expect(bars.map((b) => b.height)).toEqual([0, 5, 20, 1]);
    expect(bars[2]).toMatchObject({ x: 20, y: 0, width: 9 });
    expect(barGeometry([], 10, 10)).toEqual([]);
    expect(barGeometry([0, 0], 10, 10).every((b) => b.height === 0)).toBe(true);
  });

  it('draws the battery trace and breaks it over long gaps', () => {
    const from = Date.parse('2026-10-10T00:00:00Z');
    const to = from + 4 * 3600_000;
    const path = batteryPath(
      [
        point('2026-10-10T01:00:00Z', 50),
        point('2026-10-10T00:00:00Z', 100),
        point('2026-10-10T04:00:00Z', 0),
        point('garbage', 10),
      ],
      from,
      to,
      400,
      100,
      2 * 3600_000,
    );
    expect(path).toBe('M0.0 0.0 L100.0 50.0 M400.0 100.0');
    expect(batteryPath([], from, to, 10, 10)).toBe('');
  });

  it('finds the extremes and the latest level', () => {
    expect(
      batteryExtremes([
        point('2026-10-10T02:00:00Z', 40, 35, 45),
        point('2026-10-10T01:00:00Z', 90),
      ]),
    ).toEqual({ min: 35, max: 90, last: 40 });
    expect(batteryExtremes([])).toBeNull();
  });

  it('reads short durations in minutes and long ones in hours', () => {
    expect(durationParts(0)).toEqual({ value: 0, digits: 1, unit: 'min' });
    expect(durationParts(0.05)).toEqual({ value: 3, digits: 1, unit: 'min' });
    expect(durationParts(0.5)).toEqual({ value: 30, digits: 0, unit: 'min' });
    expect(durationParts(2.5)).toEqual({ value: 2.5, digits: 2, unit: 'h' });
    expect(durationParts(42)).toEqual({ value: 42, digits: 1, unit: 'h' });
    expect(durationParts(Number.NaN)).toEqual({ value: 0, digits: 1, unit: 'min' });
  });

  it('grades the blade hours against the change interval', () => {
    expect(thresholdLevel(10, 50)).toBe('ok');
    expect(thresholdLevel(40, 50)).toBe('soon');
    expect(thresholdLevel(50, 50)).toBe('due');
    expect(thresholdLevel(5, 0)).toBe('ok');
    expect(thresholdShare(25, 50)).toBe(0.5);
    expect(thresholdShare(80, 50)).toBe(1);
    expect(thresholdShare(1, 0)).toBe(0);
  });

  it('takes the blade meter from the device counter when the mower reports one', () => {
    const own = {
      blade_source: 'telemetry' as const,
      blade_hours_since_change: 3.5,
      blade_hours_threshold: 50,
      device_blade_hours: null,
      device_blade_warn_hours: null,
    };
    expect(bladeMeter(own)).toEqual({
      hours: 3.5,
      threshold: 50,
      source: 'telemetry',
      thresholdSource: 'default',
    });
    const device = {
      ...own,
      blade_source: 'device' as const,
      device_blade_hours: 12,
      device_blade_warn_hours: 100,
    };
    expect(bladeMeter(device)).toEqual({
      hours: 12,
      threshold: 100,
      source: 'device',
      thresholdSource: 'device',
    });
    // no device threshold: the configured one
    expect(bladeMeter({ ...device, device_blade_warn_hours: null })).toEqual({
      hours: 12,
      threshold: 50,
      source: 'device',
      thresholdSource: 'default',
    });
    // an older Telemetry without the effective threshold: one set in MowDeck still wins
    expect(
      bladeMeter({ ...device, blade_hours_threshold: 80, blade_hours_threshold_custom: true }),
    ).toMatchObject({ threshold: 80, thresholdSource: 'custom' });
    // inconsistent answer (device source without a value): MowDeck's own counter
    expect(bladeMeter({ ...device, device_blade_hours: null }).source).toBe('telemetry');
  });

  it('uses the one effective threshold Telemetry names', () => {
    // The live case: the mower's 82.59 h of its own 100 h, MowDeck's default 50 h unused.
    const live = {
      blade_source: 'device' as const,
      blade_hours_since_change: 0.1,
      blade_hours_threshold: 50,
      blade_hours_threshold_custom: false,
      blade_hours_threshold_effective: 100,
      blade_hours_threshold_source: 'device' as const,
      device_blade_hours: 82.59,
      device_blade_warn_hours: 100,
    };
    expect(bladeMeter(live)).toEqual({
      hours: 82.59,
      threshold: 100,
      source: 'device',
      thresholdSource: 'device',
    });
    const custom = {
      ...live,
      blade_hours_threshold: 80,
      blade_hours_threshold_custom: true,
      blade_hours_threshold_effective: 80,
      blade_hours_threshold_source: 'custom' as const,
    };
    const meter = bladeMeter(custom);
    expect(meter).toMatchObject({ hours: 82.59, threshold: 80, thresholdSource: 'custom' });
    expect(thresholdLevel(meter.hours, meter.threshold)).toBe('due');
  });

  it('shows the lifetime totals of the mower when it reports them', () => {
    const own = { mowing_hours: 0.14, distance_m: 151 };
    expect(lifetimeTotals(own)).toEqual({
      mowingHours: 0.14,
      mowingSource: 'telemetry',
      distanceM: 151,
      distanceSource: 'telemetry',
      trackedHours: 0.14,
      trackedDistanceM: 151,
    });
    expect(
      lifetimeTotals({ ...own, device_mowing_hours: 1012.5, device_distance_m: 432100 }),
    ).toEqual({
      mowingHours: 1012.5,
      mowingSource: 'device',
      distanceM: 432100,
      distanceSource: 'device',
      trackedHours: 0.14,
      trackedDistanceM: 151,
    });
    // each counter on its own
    expect(
      lifetimeTotals({ ...own, device_mowing_hours: null, device_distance_m: 9 }),
    ).toMatchObject({ mowingSource: 'telemetry', distanceSource: 'device', distanceM: 9 });
  });

  it('takes the battery cycles from the device counter when the mower reports one', () => {
    const own = {
      battery_cycles: 2,
      battery_cycles_tracked: 2,
      battery_cycles_source: 'telemetry' as const,
      device_battery_cycles: null,
    };
    expect(cycleCount(own)).toEqual({ cycles: 2, source: 'telemetry', tracked: 2 });
    const device = {
      ...own,
      battery_cycles: 152,
      battery_cycles_source: 'device' as const,
      device_battery_cycles: 152,
    };
    expect(cycleCount(device)).toEqual({ cycles: 152, source: 'device', tracked: 2 });
    // inconsistent answer (device source without a value): MowDeck's own count
    expect(cycleCount({ ...device, device_battery_cycles: null })).toEqual({
      cycles: 2,
      source: 'telemetry',
      tracked: 2,
    });
    // an older Telemetry without the source fields
    expect(cycleCount({ battery_cycles: 3 })).toEqual({
      cycles: 3,
      source: 'telemetry',
      tracked: 3,
    });
  });

  it('orders error counts and asks for 15 minute battery bins', () => {
    expect(errorRows({ e2: 1, unknown: 3, e1: 1, zero: 0 })).toEqual([
      { code: 'unknown', count: 3 },
      { code: 'e1', count: 1 },
      { code: 'e2', count: 1 },
    ]);
    expect(batteryQuery(new Date('2026-10-10T12:00:00Z'))).toEqual({
      from: '2026-10-09T12:00:00.000Z',
      to: '2026-10-10T12:00:00.000Z',
      step: '15m',
    });
  });
});
