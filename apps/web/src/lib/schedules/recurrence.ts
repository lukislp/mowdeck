/**
 * Schedules in words: "Mo, Mi, Fr 07:00, 90 min", "alle 3 Tage ab 12.10.2026 07:00, 2 h", and
 * the countdown to the next firing. Pure (the locale is a parameter), so both languages are
 * unit tested (`recurrence.test.ts`).
 */
import { m } from '$lib/paraglide/messages';

import { WEEKDAYS, isWeekday, type Locale, type Schedule, type Weekday } from './types';

export function weekdayShort(day: Weekday, locale: Locale): string {
  const options = { locale };
  switch (day) {
    case 'mon':
      return m.sched_day_mon({}, options);
    case 'tue':
      return m.sched_day_tue({}, options);
    case 'wed':
      return m.sched_day_wed({}, options);
    case 'thu':
      return m.sched_day_thu({}, options);
    case 'fri':
      return m.sched_day_fri({}, options);
    case 'sat':
      return m.sched_day_sat({}, options);
    case 'sun':
      return m.sched_day_sun({}, options);
  }
}

/**
 * The days of a weekly schedule: every day is "täglich", three or more days in a row become a
 * range ("Mo–Fr"), others are listed ("Mo, Mi, Fr").
 */
export function daysSummary(days: readonly string[], locale: Locale): string {
  const chosen = new Set(days.filter(isWeekday));
  if (chosen.size === 7) return m.sched_daily({}, { locale });
  const parts: string[] = [];
  let run: Weekday[] = [];
  const flush = () => {
    if (run.length >= 3) {
      parts.push(`${weekdayShort(run[0]!, locale)}–${weekdayShort(run[run.length - 1]!, locale)}`);
    } else {
      parts.push(...run.map((d) => weekdayShort(d, locale)));
    }
    run = [];
  };
  for (const day of WEEKDAYS) {
    if (chosen.has(day)) run.push(day);
    else flush();
  }
  flush();
  return parts.join(', ');
}

/** `HH:MM` of an API time (`07:00` or `07:00:00`). */
export function clockTime(value: string): string {
  const match = /^(\d{1,2}):(\d{2})/.exec(value.trim());
  return match ? `${match[1]!.padStart(2, '0')}:${match[2]}` : value;
}

/** A window length: "90 min" below two hours, then "2 h" or "2 h 30 min". */
export function formatDuration(minutes: number): string {
  if (!Number.isFinite(minutes) || minutes < 120) return `${Math.round(minutes)} min`;
  const h = Math.floor(minutes / 60);
  const rest = Math.round(minutes - h * 60);
  return rest ? `${h} h ${rest} min` : `${h} h`;
}

/** A calendar date of the API (`YYYY-MM-DD`) for the locale, without a time zone shift. */
export function formatDay(value: string, locale: Locale): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return value;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium' }).format(date);
}

type Recurrence = Pick<
  Schedule,
  'kind' | 'days_of_week' | 'start_time' | 'duration_minutes' | 'anchor_date' | 'interval_days'
>;

/** The recurrence, time and window of a schedule in one line. */
export function recurrenceSummary(schedule: Recurrence, locale: Locale): string {
  const time = clockTime(schedule.start_time);
  const duration = formatDuration(schedule.duration_minutes);
  if (schedule.kind === 'every_n_days') {
    const count = schedule.interval_days ?? 1;
    const date = schedule.anchor_date ? formatDay(schedule.anchor_date, locale) : '';
    const rule =
      count === 1
        ? m.sched_every_day_from({ date }, { locale })
        : m.sched_every_n_days_from({ count, date }, { locale });
    return `${rule} ${time}, ${duration}`;
  }
  return `${daysSummary(schedule.days_of_week, locale)} ${time}, ${duration}`;
}

/** Time until `at` in words: "in 12 min", "in 3 h 05 min", "in 4 Tagen", or "jetzt fällig". */
export function formatCountdown(at: string | number, now: number, locale: Locale): string {
  const target = typeof at === 'number' ? at : Date.parse(at);
  const seconds = Math.round((target - now) / 1000);
  if (!Number.isFinite(seconds)) return '';
  if (seconds <= 0) return m.sched_countdown_due({}, { locale });
  if (seconds < 60) return m.sched_countdown_soon({}, { locale });
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return m.sched_countdown_minutes({ minutes }, { locale });
  const hours = Math.floor(minutes / 60);
  if (hours < 48) {
    const rest = String(minutes - hours * 60).padStart(2, '0');
    return m.sched_countdown_hours({ hours, minutes: rest }, { locale });
  }
  return m.sched_countdown_days({ count: Math.floor(hours / 24) }, { locale });
}

/** Date and time of a firing in the schedule's time zone ("Mo., 13.10., 07:00"). */
export function formatFiring(at: string, timeZone: string | null, locale: Locale): string {
  const options: Intl.DateTimeFormatOptions = {
    weekday: 'short',
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  };
  try {
    return new Intl.DateTimeFormat(locale, { ...options, timeZone: timeZone ?? undefined }).format(
      new Date(at),
    );
  } catch {
    return new Intl.DateTimeFormat(locale, options).format(new Date(at));
  }
}
