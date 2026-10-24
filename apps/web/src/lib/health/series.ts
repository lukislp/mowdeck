/**
 * Pure helpers of the health page (Telemetry's `/health`, `/health/daily`, `/battery`): the last
 * 30 UTC days with gaps filled, bar geometry, the battery trace as an SVG path that breaks over
 * gaps, the blade change threshold level and the error counts in order. No DOM, unit tested.
 */
import type { components } from '$lib/generated/telemetry';

export type HealthTotals = components['schemas']['MowerHealthOut'];
export type HealthDay = components['schemas']['HealthDayOut'];
export type BatteryPoint = components['schemas']['BatteryPointOut'];

export const DAILY_DAYS = 30;
/** Hours since the blade change from which the change is "due soon" (fraction of the threshold). */
export const SOON_FRACTION = 0.8;

export interface DayValue {
  day: string;
  bladeHours: number;
  distanceM: number;
  mowingHours: number;
}

/** `YYYY-MM-DD` of a UTC day. */
export function utcDay(date: Date): string {
  return date.toISOString().slice(0, 10);
}

/** First and last UTC day of the `count` days ending with the day of `end`. */
export function dayRange(end: Date, count = DAILY_DAYS): { from: string; to: string } {
  const first = new Date(Date.UTC(end.getUTCFullYear(), end.getUTCMonth(), end.getUTCDate()));
  first.setUTCDate(first.getUTCDate() - (count - 1));
  return { from: utcDay(first), to: utcDay(end) };
}

/** One entry per UTC day of the range, oldest first; days without data are zero. */
export function fillDays(days: readonly HealthDay[], end: Date, count = DAILY_DAYS): DayValue[] {
  const byDay = new Map(days.map((d) => [d.day, d]));
  const { from } = dayRange(end, count);
  const cursor = new Date(`${from}T00:00:00Z`);
  const result: DayValue[] = [];
  for (let i = 0; i < count; i++) {
    const key = utcDay(cursor);
    const found = byDay.get(key);
    result.push({
      day: key,
      bladeHours: found?.blade_hours ?? 0,
      distanceM: found?.distance_m ?? 0,
      mowingHours: found?.mowing_hours ?? 0,
    });
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }
  return result;
}

export interface Bar {
  x: number;
  y: number;
  width: number;
  height: number;
  value: number;
}

/**
 * Bars for `values` in a `width` x `height` box, a 1 unit gap between bars; a value above zero
 * is at least 1 unit tall so a short mowing day stays visible.
 */
export function barGeometry(values: readonly number[], width: number, height: number): Bar[] {
  if (values.length === 0) return [];
  const peak = Math.max(0, ...values);
  const slot = width / values.length;
  const gap = slot > 3 ? 1 : 0;
  return values.map((value, i) => {
    const h = peak > 0 && value > 0 ? Math.max(1, (value / peak) * height) : 0;
    return { x: i * slot, y: height - h, width: slot - gap, height: h, value };
  });
}

/**
 * The battery series as an SVG path in a `width` x `height` box over `[from, to]` (0 % at the
 * bottom, 100 % at the top). A gap longer than `maxGapMs` starts a new segment.
 */
export function batteryPath(
  points: readonly BatteryPoint[],
  from: number,
  to: number,
  width: number,
  height: number,
  maxGapMs = 3 * 60 * 60 * 1000,
): string {
  const span = Math.max(1, to - from);
  let previous: number | null = null;
  const parts: string[] = [];
  for (const point of [...points].sort((a, b) => Date.parse(a.at) - Date.parse(b.at))) {
    const at = Date.parse(point.at);
    if (!Number.isFinite(at)) continue;
    const x = ((Math.min(Math.max(at, from), to) - from) / span) * width;
    const percent = Math.min(Math.max(point.battery_percent, 0), 100);
    const y = height - (percent / 100) * height;
    const move = previous === null || at - previous > maxGapMs;
    parts.push(`${move ? 'M' : 'L'}${x.toFixed(1)} ${y.toFixed(1)}`);
    previous = at;
  }
  return parts.join(' ');
}

/** Lowest and highest battery level of the series (null without points). */
export function batteryExtremes(
  points: readonly BatteryPoint[],
): { min: number; max: number; last: number } | null {
  if (points.length === 0) return null;
  const sorted = [...points].sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
  return {
    min: Math.min(...points.map((p) => p.battery_min ?? p.battery_percent)),
    max: Math.max(...points.map((p) => p.battery_max ?? p.battery_percent)),
    last: sorted[sorted.length - 1]!.battery_percent,
  };
}

/**
 * A duration in hours as a readout: minutes below one hour (one decimal below ten minutes), hours
 * from then on (two decimals below ten hours), so a young mower does not read "0.00 h".
 */
export function durationParts(hours: number): { value: number; digits: number; unit: 'min' | 'h' } {
  const safe = Number.isFinite(hours) && hours > 0 ? hours : 0;
  if (safe < 1) {
    const minutes = safe * 60;
    return { value: minutes, digits: minutes < 10 ? 1 : 0, unit: 'min' };
  }
  return { value: safe, digits: safe < 10 ? 2 : 1, unit: 'h' };
}

export type ThresholdLevel = 'ok' | 'soon' | 'due';

/** How far the blade hours since the change are towards the change interval. */
export function thresholdLevel(hours: number, threshold: number): ThresholdLevel {
  if (threshold <= 0) return 'ok';
  if (hours >= threshold) return 'due';
  return hours >= threshold * SOON_FRACTION ? 'soon' : 'ok';
}

export type BladeSource = HealthTotals['blade_source'];

/** What the "blades since change" meter shows. */
export interface BladeMeter {
  /** Hours since the blade change. */
  hours: number;
  /** The change interval in hours. */
  threshold: number;
  /** `device`: the mower's own counter (reset in the vendor app); `telemetry`: MowDeck's. */
  source: BladeSource;
  /**
   * Where the interval comes from: `custom` set in MowDeck, `device` the mower's own warning,
   * `default` MowDeck's default.
   */
  thresholdSource: ThresholdSource;
}

export type ThresholdSource = HealthTotals['blade_hours_threshold_source'];

type BladeFields = Pick<
  HealthTotals,
  | 'blade_source'
  | 'blade_hours_since_change'
  | 'blade_hours_threshold'
  | 'device_blade_hours'
  | 'device_blade_warn_hours'
> &
  Partial<
    Pick<
      HealthTotals,
      | 'blade_hours_threshold_custom'
      | 'blade_hours_threshold_effective'
      | 'blade_hours_threshold_source'
    >
  >;

/** The interval of an answer without the effective threshold (an older Telemetry). */
function fallbackThreshold(totals: BladeFields, device: boolean): [number, ThresholdSource] {
  if (totals.blade_hours_threshold_custom) return [totals.blade_hours_threshold, 'custom'];
  const warn = totals.device_blade_warn_hours;
  if (device && warn != null && warn > 0) return [warn, 'device'];
  return [totals.blade_hours_threshold, 'default'];
}

/**
 * The blade meter: the mower's own counter when it reports one (that is what the vendor app
 * shows), MowDeck's own counter otherwise; against the one effective interval Telemetry names
 * (set in MowDeck, else the mower's own warning, else MowDeck's default), the same one the
 * reminder uses.
 */
export function bladeMeter(totals: BladeFields): BladeMeter {
  const device = totals.device_blade_hours;
  const isDevice = totals.blade_source === 'device' && device != null;
  const effective = totals.blade_hours_threshold_effective;
  const source = totals.blade_hours_threshold_source;
  const [threshold, thresholdSource] =
    effective != null && source != null ? [effective, source] : fallbackThreshold(totals, isDevice);
  if (isDevice) {
    return { hours: device, threshold, source: 'device', thresholdSource };
  }
  return {
    hours: totals.blade_hours_since_change,
    threshold,
    source: 'telemetry',
    thresholdSource,
  };
}

/** Mowing hours and distance to show: the mower's lifetime values when it reports them. */
export interface LifetimeTotals {
  mowingHours: number;
  /** `device`: the mower's own lifetime working time; `telemetry`: MowDeck's own count. */
  mowingSource: BladeSource;
  distanceM: number;
  distanceSource: BladeSource;
  /** MowDeck's own count since it started tracking. */
  trackedHours: number;
  trackedDistanceM: number;
}

type LifetimeFields = Pick<HealthTotals, 'mowing_hours' | 'distance_m'> &
  Partial<Pick<HealthTotals, 'device_mowing_hours' | 'device_distance_m'>>;

/**
 * Mowing hours and distance: the mower's own lifetime counters when it reports them (MowDeck's
 * own only start with its first state), MowDeck's otherwise; each one on its own.
 */
export function lifetimeTotals(totals: LifetimeFields): LifetimeTotals {
  const hours = totals.device_mowing_hours;
  const distance = totals.device_distance_m;
  return {
    mowingHours: hours ?? totals.mowing_hours,
    mowingSource: hours != null ? 'device' : 'telemetry',
    distanceM: distance ?? totals.distance_m,
    distanceSource: distance != null ? 'device' : 'telemetry',
    trackedHours: totals.mowing_hours,
    trackedDistanceM: totals.distance_m,
  };
}

/** What the "battery cycles" readout shows. */
export interface CycleCount {
  /** The cycles to show. */
  cycles: number;
  /** `device`: the mower's own lifetime counter; `telemetry`: MowDeck's count. */
  source: HealthTotals['battery_cycles_source'];
  /** MowDeck's own count since it started tracking. */
  tracked: number;
}

type CycleFields = Pick<HealthTotals, 'battery_cycles'> &
  Partial<
    Pick<HealthTotals, 'battery_cycles_source' | 'battery_cycles_tracked' | 'device_battery_cycles'>
  >;

/**
 * Battery cycles: the mower's own lifetime counter when it reports one (MowDeck's count only
 * starts with its first state), MowDeck's count otherwise. An answer without the source fields
 * (an older Telemetry) is MowDeck's count.
 */
export function cycleCount(totals: CycleFields): CycleCount {
  const tracked = totals.battery_cycles_tracked ?? totals.battery_cycles;
  const device = totals.device_battery_cycles;
  if (totals.battery_cycles_source === 'device' && device != null) {
    return { cycles: device, source: 'device', tracked };
  }
  return { cycles: tracked, source: 'telemetry', tracked };
}

/** Share of the interval used, 0..1. */
export function thresholdShare(hours: number, threshold: number): number {
  if (threshold <= 0) return 0;
  return Math.min(1, Math.max(0, hours / threshold));
}

/** Error counts, most frequent first, then by code. */
export function errorRows(errors: Record<string, number>): { code: string; count: number }[] {
  return Object.entries(errors)
    .filter(([, count]) => count > 0)
    .map(([code, count]) => ({ code, count }))
    .sort((a, b) => b.count - a.count || a.code.localeCompare(b.code));
}

/** The query for the battery series of the last 24 hours (15 minute bins). */
export function batteryQuery(now: Date): { from: string; to: string; step: string } {
  return {
    from: new Date(now.getTime() - 24 * 60 * 60 * 1000).toISOString(),
    to: now.toISOString(),
    step: '15m',
  };
}
