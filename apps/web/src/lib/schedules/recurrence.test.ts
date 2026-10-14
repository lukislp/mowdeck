import { describe, expect, it } from 'vitest';

import {
  clockTime,
  daysSummary,
  formatCountdown,
  formatDay,
  formatDuration,
  formatFiring,
  recurrenceSummary,
} from './recurrence';

const weekly = {
  kind: 'weekly',
  days_of_week: ['mon', 'wed', 'fri'],
  start_time: '07:00:00',
  duration_minutes: 90,
  anchor_date: null,
  interval_days: null,
};

describe('recurrenceSummary', () => {
  it('lists weekdays with time and window in German and English', () => {
    expect(recurrenceSummary(weekly, 'de')).toBe('Mo, Mi, Fr 07:00, 90 min');
    expect(recurrenceSummary(weekly, 'en')).toBe('Mon, Wed, Fri 07:00, 90 min');
  });

  it('describes every-N-days schedules with their anchor date', () => {
    const every = {
      ...weekly,
      kind: 'every_n_days',
      days_of_week: [],
      anchor_date: '2026-10-12',
      interval_days: 3,
      duration_minutes: 120,
    };
    expect(recurrenceSummary(every, 'de')).toBe('Alle 3 Tage ab 12.10.2026 07:00, 2 h');
    expect(recurrenceSummary(every, 'en')).toBe('Every 3 days from Oct 12, 2026 07:00, 2 h');
    expect(recurrenceSummary({ ...every, interval_days: 1 }, 'de')).toBe(
      'Täglich ab 12.10.2026 07:00, 2 h',
    );
  });
});

describe('daysSummary', () => {
  it('collapses three or more days in a row into a range', () => {
    expect(daysSummary(['mon', 'tue', 'wed', 'thu', 'fri'], 'de')).toBe('Mo–Fr');
    expect(daysSummary(['mon', 'tue', 'wed', 'fri'], 'en')).toBe('Mon–Wed, Fri');
    expect(daysSummary(['sat', 'sun'], 'de')).toBe('Sa, So');
  });

  it('says daily for all seven days and ignores unknown names and the order', () => {
    expect(daysSummary(['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'], 'de')).toBe('Täglich');
    expect(daysSummary(['fri', 'xyz', 'mon'], 'en')).toBe('Mon, Fri');
  });
});

describe('formatting helpers', () => {
  it('formats durations below two hours in minutes, else in hours', () => {
    expect(formatDuration(45)).toBe('45 min');
    expect(formatDuration(119)).toBe('119 min');
    expect(formatDuration(120)).toBe('2 h');
    expect(formatDuration(150)).toBe('2 h 30 min');
  });

  it('cuts API times to HH:MM', () => {
    expect(clockTime('07:00:00')).toBe('07:00');
    expect(clockTime('7:05')).toBe('07:05');
    expect(clockTime('soon')).toBe('soon');
  });

  it('formats calendar dates without a time zone shift', () => {
    expect(formatDay('2026-01-01', 'de')).toBe('01.01.2026');
    expect(formatDay('not a date', 'de')).toBe('not a date');
  });

  it('formats a firing in the given time zone', () => {
    const at = '2026-10-12T05:00:00Z';
    expect(formatFiring(at, 'Europe/Berlin', 'de')).toBe('Mo., 12.10., 07:00');
    expect(formatFiring(at, 'UTC', 'en')).toContain('05:00');
    expect(formatFiring(at, 'Not/AZone', 'de')).toMatch(/12\.10\./);
  });
});

describe('formatCountdown', () => {
  const now = Date.parse('2026-10-10T08:00:00Z');

  it('counts down in minutes, hours and days', () => {
    expect(formatCountdown(now + 30_000, now, 'de')).toBe('in weniger als 1 min');
    expect(formatCountdown(now + 12 * 60_000, now, 'de')).toBe('in 12 min');
    expect(formatCountdown(now + (3 * 60 + 5) * 60_000, now, 'en')).toBe('in 3 h 05 min');
    expect(formatCountdown(now + 4 * 86_400_000 + 1000, now, 'de')).toBe('in 4 Tagen');
    expect(formatCountdown(new Date(now + 4 * 86_400_000).toISOString(), now, 'en')).toBe(
      'in 4 days',
    );
  });

  it('says due when the time has come, and nothing for an invalid time', () => {
    expect(formatCountdown(now - 1000, now, 'de')).toBe('jetzt fällig');
    expect(formatCountdown('nonsense', now, 'de')).toBe('');
  });
});
