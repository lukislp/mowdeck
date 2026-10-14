/**
 * Report helpers: periods, per mower totals, account totals and faults by code, from
 * Telemetry's `GET /reports/summary` (day totals per mower in UTC days, one call). Telemetry
 * does not know sites, so the site filter uses Fleet's `site_id` of each mower.
 *
 * Availability is the online time divided by the hours of the period that have passed (today
 * counts up to now), capped at 100 %; a mower without online time in the period reads 0 %.
 */
import type { CsvDialect, Cell } from './csv';
import { csvNumber } from './csv';

export type Period = 'today' | '7d' | '30d' | 'custom';
export const PERIODS: readonly Period[] = ['today', '7d', '30d', 'custom'];

/** An inclusive range of UTC days (`YYYY-MM-DD`). */
export interface DayRange {
  from: string;
  to: string;
}

export interface ReportDay {
  day: string;
  blade_hours: number;
  mowing_hours: number;
  distance_m: number;
  jobs: number;
  online_hours: number;
  errors: Record<string, number>;
}

export interface ReportMower {
  id: string;
  name: string;
  site_id: string;
}

export interface ReportRow {
  mowerId: string;
  name: string;
  siteId: string;
  mowingHours: number;
  bladeHours: number;
  distanceM: number;
  jobs: number;
  faults: number;
  errors: Record<string, number>;
  onlineHours: number;
  /** Share of the period online, 0 to 1. */
  availability: number;
  /** Days with any data. */
  days: number;
}

export interface ReportTotals {
  mowers: number;
  mowingHours: number;
  bladeHours: number;
  distanceM: number;
  jobs: number;
  faults: number;
  /** Mean availability of the mowers (each mower weighs the same). */
  availability: number;
}

const DAY_MS = 86_400_000;

/** `YYYY-MM-DD` of a UTC day. */
export function isoDay(at: Date): string {
  return at.toISOString().slice(0, 10);
}

function dayStart(day: string): number {
  return Date.parse(`${day}T00:00:00Z`);
}

/** The UTC days of a period ending today (custom: the given range, ordered). */
export function periodRange(period: Period, now: Date, custom?: DayRange): DayRange {
  const today = isoDay(now);
  const back = (days: number) => isoDay(new Date(dayStart(today) - days * DAY_MS));
  switch (period) {
    case 'today':
      return { from: today, to: today };
    case '7d':
      return { from: back(6), to: today };
    case '30d':
      return { from: back(29), to: today };
    case 'custom': {
      const from = custom?.from || today;
      const to = custom?.to || from;
      return from <= to ? { from, to } : { from: to, to: from };
    }
  }
}

/** Inclusive number of days of a range. */
export function rangeDays(range: DayRange): number {
  return Math.round((dayStart(range.to) - dayStart(range.from)) / DAY_MS) + 1;
}

/** A custom range is valid when both days parse, it spans at most 366 days and ends by today. */
export function customRangeIssue(
  range: DayRange,
  now: Date,
): 'missing' | 'order' | 'too_long' | 'future' | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(range.from) || !/^\d{4}-\d{2}-\d{2}$/.test(range.to)) {
    return 'missing';
  }
  if (range.from > range.to) return 'order';
  if (rangeDays(range) > 366) return 'too_long';
  if (range.from > isoDay(now)) return 'future';
  return null;
}

/** Hours of the range that have passed at `now` (the elapsed part of today). */
export function elapsedHours(range: DayRange, now: Date): number {
  const start = dayStart(range.from);
  const end = Math.min(dayStart(range.to) + DAY_MS, now.getTime());
  return Math.max(0, (end - start) / 3_600_000);
}

function sumErrors(target: Record<string, number>, add: Record<string, number>): void {
  for (const [code, count] of Object.entries(add)) target[code] = (target[code] ?? 0) + count;
}

/** One row per mower (also mowers without data: zeros), ordered by name. */
export function reportRows(
  mowers: readonly ReportMower[],
  daysByMower: ReadonlyMap<string, readonly ReportDay[]>,
  periodHours: number,
): ReportRow[] {
  return mowers
    .map((mower) => {
      const days = daysByMower.get(mower.id) ?? [];
      const errors: Record<string, number> = {};
      let mowingHours = 0;
      let bladeHours = 0;
      let distanceM = 0;
      let jobs = 0;
      let onlineHours = 0;
      for (const day of days) {
        mowingHours += day.mowing_hours;
        bladeHours += day.blade_hours;
        distanceM += day.distance_m;
        jobs += day.jobs;
        onlineHours += day.online_hours;
        sumErrors(errors, day.errors);
      }
      const faults = Object.values(errors).reduce((sum, n) => sum + n, 0);
      return {
        mowerId: mower.id,
        name: mower.name,
        siteId: mower.site_id,
        mowingHours,
        bladeHours,
        distanceM,
        jobs,
        faults,
        errors,
        onlineHours,
        availability: periodHours > 0 ? Math.min(1, onlineHours / periodHours) : 0,
        days: days.length,
      };
    })
    .sort((a, b) => a.name.localeCompare(b.name));
}

export function reportTotals(rows: readonly ReportRow[]): ReportTotals {
  const sum = (pick: (row: ReportRow) => number) => rows.reduce((s, row) => s + pick(row), 0);
  return {
    mowers: rows.length,
    mowingHours: sum((r) => r.mowingHours),
    bladeHours: sum((r) => r.bladeHours),
    distanceM: sum((r) => r.distanceM),
    jobs: sum((r) => r.jobs),
    faults: sum((r) => r.faults),
    availability: rows.length ? sum((r) => r.availability) / rows.length : 0,
  };
}

/** Faults by code over all rows, most frequent first (ties by code). */
export function faultsByCode(rows: readonly ReportRow[]): { code: string; count: number }[] {
  const all: Record<string, number> = {};
  for (const row of rows) sumErrors(all, row.errors);
  return Object.entries(all)
    .map(([code, count]) => ({ code, count }))
    .sort((a, b) => b.count - a.count || a.code.localeCompare(b.code));
}

/** The CSV and clipboard columns: stable English keys, whatever the UI language. */
export const REPORT_COLUMNS = [
  'mower_id',
  'mower',
  'site',
  'from',
  'to',
  'mowing_hours',
  'blade_hours',
  'distance_m',
  'jobs',
  'faults',
  'availability_percent',
  'faults_by_code',
] as const;

/** Header, one line per mower and a totals line (mower id `total`). */
export function reportTable(
  rows: readonly ReportRow[],
  totals: ReportTotals,
  range: DayRange,
  siteName: (siteId: string) => string,
  totalLabel: string,
  dialect: CsvDialect,
): Cell[][] {
  const codes = (errors: Record<string, number>) =>
    Object.entries(errors)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([code, count]) => `${code}=${count}`)
      .join(' ');
  const number = (value: number) => csvNumber(value, dialect, 2);
  const lines: Cell[][] = [[...REPORT_COLUMNS]];
  for (const row of rows) {
    lines.push([
      row.mowerId,
      row.name,
      siteName(row.siteId),
      range.from,
      range.to,
      number(row.mowingHours),
      number(row.bladeHours),
      number(row.distanceM),
      row.jobs,
      row.faults,
      number(row.availability * 100),
      codes(row.errors),
    ]);
  }
  const all: Record<string, number> = {};
  for (const row of rows) sumErrors(all, row.errors);
  lines.push([
    'total',
    totalLabel,
    '',
    range.from,
    range.to,
    number(totals.mowingHours),
    number(totals.bladeHours),
    number(totals.distanceM),
    totals.jobs,
    totals.faults,
    number(totals.availability * 100),
    codes(all),
  ]);
  return lines;
}

/** Every day of the range, oldest first. */
export function rangeDayList(range: DayRange): string[] {
  const days: string[] = [];
  for (let at = dayStart(range.from); at <= dayStart(range.to); at += DAY_MS) {
    days.push(isoDay(new Date(at)));
  }
  return days;
}

/** One value per day of the range: `pick` summed over the given mowers (absent days are 0). */
export function dailySeries(
  range: DayRange,
  daysByMower: ReadonlyMap<string, readonly ReportDay[]>,
  mowerIds: readonly string[],
  pick: (day: ReportDay) => number,
): { days: string[]; values: number[] } {
  const days = rangeDayList(range);
  const index = new Map(days.map((d, i) => [d, i]));
  const values = days.map(() => 0);
  for (const id of mowerIds) {
    for (const day of daysByMower.get(id) ?? []) {
      const i = index.get(day.day);
      if (i !== undefined) values[i] = (values[i] ?? 0) + pick(day);
    }
  }
  return { days, values };
}
