/**
 * The schedule form: state, the client checks that mirror the Scheduler's rules
 * (`scheduler.domain.rules.validate_schedule` and the request models), the request bodies, and
 * the mapping of a 422 answer to the fields. Pure and unit tested (`form.test.ts`).
 */
import type { ApiProblem } from '$lib/api/problem';

import { emptyMow, mowBody, mowDraftOf, type MowDraft } from './mow-options';
import { clockTime } from './recurrence';
import { isWeekday, WEEKDAYS, type Schedule, type ScheduleIn, type Weekday } from './types';

export interface ScheduleFormState {
  name: string;
  targetType: 'mower' | 'group';
  targetId: string;
  kind: 'weekly' | 'every_n_days';
  days: Weekday[];
  /** `HH:MM` wall-clock time in the schedule's time zone. */
  startTime: string;
  /** `YYYY-MM-DD`, the first day an every-N-days schedule fires. */
  anchorDate: string;
  intervalDays: string;
  /** Window length in minutes. */
  duration: string;
  /** Areas in mowing order (a mower target; empty means every area). */
  areaIds: string[];
  /** The start settings (`$lib/schedules/mow-options`): pattern keys and setpoints. */
  mow: MowDraft;
  /** Angles used round robin, one per run (replace the angle). */
  rotation: number[];
  rainEnabled: boolean;
  probability: string;
  precipitation: string;
  lookahead: string;
  cooldown: string;
  runWhenUnknown: boolean;
  pauseRunning: boolean;
  returnAtWindowEnd: boolean;
  /** IANA name overriding the site's time zone; empty follows the site. */
  timezone: string;
  enabled: boolean;
}

export type FormField =
  | 'name'
  | 'target'
  | 'days'
  | 'start_time'
  | 'anchor_date'
  | 'interval_days'
  | 'duration'
  | 'areas'
  | 'mow'
  | 'rotation'
  | 'probability'
  | 'precipitation'
  | 'lookahead'
  | 'cooldown'
  | 'timezone';

export type FormIssue =
  | { code: 'required' }
  | { code: 'days' }
  | { code: 'format' }
  | { code: 'integer' }
  | { code: 'number' }
  | { code: 'range'; min: number; max: number }
  | { code: 'count'; max: number }
  | { code: 'timezone' }
  | { code: 'group_areas' }
  /** A rule only the server knows; `message` is its English text. */
  | { code: 'server'; message: string };

export type FormIssues = Partial<Record<FormField, FormIssue>>;

export const LIMITS = {
  name: 100,
  intervalDays: [1, 365],
  duration: [1, 24 * 60],
  rotation: 24,
  areas: 50,
  probability: [0, 100],
  precipitation: [0, 100],
  hours: [0, 24],
} as const;

/** A number typed with a decimal comma or point; null when it is not one. */
export function parseNumber(text: string): number | null {
  const value = text.trim().replace(',', '.');
  if (!/^[-+]?(\d+(\.\d*)?|\.\d+)$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** An IANA time zone the browser knows. */
export function isValidTimeZone(name: string): boolean {
  try {
    new Intl.DateTimeFormat('en', { timeZone: name });
    return true;
  } catch {
    return false;
  }
}

/** Today in the browser as `YYYY-MM-DD`. */
export function localDate(now: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** A new schedule: weekdays at 09:00 for two hours, the rain pause with its defaults. */
export function emptyForm(
  today: string,
  target?: { type: 'mower' | 'group'; id: string },
): ScheduleFormState {
  return {
    name: '',
    targetType: target?.type ?? 'mower',
    targetId: target?.id ?? '',
    kind: 'weekly',
    days: ['mon', 'wed', 'fri'],
    startTime: '09:00',
    anchorDate: today,
    intervalDays: '3',
    duration: '120',
    areaIds: [],
    mow: emptyMow(),
    rotation: [],
    rainEnabled: true,
    probability: '60',
    precipitation: '0.5',
    lookahead: '3',
    cooldown: '2',
    runWhenUnknown: true,
    pauseRunning: true,
    returnAtWindowEnd: false,
    timezone: '',
    enabled: true,
  };
}

/** The form of a stored schedule. */
export function fromSchedule(schedule: Schedule): ScheduleFormState {
  return {
    name: schedule.name,
    targetType: schedule.target_type === 'group' ? 'group' : 'mower',
    targetId: schedule.target_id,
    kind: schedule.kind === 'every_n_days' ? 'every_n_days' : 'weekly',
    days: schedule.days_of_week.filter(isWeekday),
    startTime: clockTime(schedule.start_time),
    anchorDate: schedule.anchor_date ?? localDate(new Date()),
    intervalDays: String(schedule.interval_days ?? 3),
    duration: String(schedule.duration_minutes),
    areaIds: [...schedule.area_ids],
    mow: mowDraftOf(schedule),
    rotation: [...schedule.rotation_angles],
    rainEnabled: schedule.rain_pause.enabled,
    probability: String(schedule.rain_pause.probability_threshold),
    precipitation: String(schedule.rain_pause.precipitation_mm_threshold),
    lookahead: String(schedule.rain_pause.lookahead_hours),
    cooldown: String(schedule.rain_pause.cooldown_hours),
    runWhenUnknown: schedule.rain_pause.run_when_unknown,
    pauseRunning: schedule.pause_running_jobs_on_rain,
    returnAtWindowEnd: schedule.return_to_dock_at_window_end,
    timezone: schedule.timezone_override ?? '',
    enabled: schedule.enabled,
  };
}

function checkInteger(text: string, [min, max]: readonly [number, number]): FormIssue | null {
  if (!text.trim()) return { code: 'required' };
  const value = parseNumber(text);
  if (value === null || !Number.isInteger(value)) return { code: 'integer' };
  return value < min || value > max ? { code: 'range', min, max } : null;
}

function checkNumber(text: string, [min, max]: readonly [number, number]): FormIssue | null {
  if (!text.trim()) return { code: 'required' };
  const value = parseNumber(text);
  if (value === null) return { code: 'number' };
  return value < min || value > max ? { code: 'range', min, max } : null;
}

/** The client checks; an empty result means the form may be sent. */
export function validateForm(state: ScheduleFormState): FormIssues {
  const issues: FormIssues = {};
  const set = (field: FormField, issue: FormIssue | null) => {
    if (issue) issues[field] = issue;
  };
  const name = state.name.trim();
  set(
    'name',
    !name ? { code: 'required' } : name.length > LIMITS.name ? { code: 'count', max: 100 } : null,
  );
  set('target', state.targetId ? null : { code: 'required' });
  set('start_time', /^([01]\d|2[0-3]):[0-5]\d$/.test(state.startTime) ? null : { code: 'format' });
  if (state.kind === 'weekly') {
    set('days', state.days.length ? null : { code: 'days' });
  } else {
    set(
      'anchor_date',
      !state.anchorDate
        ? { code: 'required' }
        : /^\d{4}-\d{2}-\d{2}$/.test(state.anchorDate)
          ? null
          : { code: 'format' },
    );
    set('interval_days', checkInteger(state.intervalDays, LIMITS.intervalDays));
  }
  set('duration', checkInteger(state.duration, LIMITS.duration));
  if (state.targetType === 'group' && state.areaIds.length) set('areas', { code: 'group_areas' });
  else if (state.areaIds.length > LIMITS.areas) set('areas', { code: 'count', max: LIMITS.areas });
  if (state.rotation.length > LIMITS.rotation) {
    set('rotation', { code: 'count', max: LIMITS.rotation });
  } else if (state.rotation.some((a) => !Number.isFinite(a) || a < -360 || a > 360)) {
    set('rotation', { code: 'range', min: -360, max: 360 });
  }
  set('probability', checkInteger(state.probability, LIMITS.probability));
  set('precipitation', checkNumber(state.precipitation, LIMITS.precipitation));
  set('lookahead', checkInteger(state.lookahead, LIMITS.hours));
  set('cooldown', checkInteger(state.cooldown, LIMITS.hours));
  if (state.timezone.trim() && !isValidTimeZone(state.timezone.trim())) {
    set('timezone', { code: 'timezone' });
  }
  return issues;
}

/** The request body (`POST /schedules`; `PATCH` takes the same fields). Validate first. */
export function toBody(state: ScheduleFormState): ScheduleIn {
  const int = (text: string, fallback: number) => {
    const value = parseNumber(text);
    return value === null ? fallback : Math.round(value);
  };
  const weekly = state.kind === 'weekly';
  return {
    name: state.name.trim(),
    target_type: state.targetType,
    target_id: state.targetId,
    kind: state.kind,
    days_of_week: weekly ? WEEKDAYS.filter((d) => state.days.includes(d)) : [],
    start_time: state.startTime,
    anchor_date: weekly ? null : state.anchorDate,
    interval_days: weekly ? null : int(state.intervalDays, 1),
    duration_minutes: int(state.duration, 120),
    area_ids: state.targetType === 'group' ? [] : [...state.areaIds],
    ...mowBody(state.mow),
    rotation_angles: [...state.rotation],
    rain_pause: {
      enabled: state.rainEnabled,
      probability_threshold: int(state.probability, 60),
      precipitation_mm_threshold: parseNumber(state.precipitation) ?? 0.5,
      lookahead_hours: int(state.lookahead, 3),
      cooldown_hours: int(state.cooldown, 2),
      run_when_unknown: state.runWhenUnknown,
    },
    pause_running_jobs_on_rain: state.pauseRunning,
    return_to_dock_at_window_end: state.returnAtWindowEnd,
    timezone: state.timezone.trim() || null,
    enabled: state.enabled,
  };
}

/** Request field names (and nested keys) of the API to form fields. */
const FIELD_OF_KEY: Record<string, FormField> = {
  name: 'name',
  target_id: 'target',
  target_type: 'target',
  days_of_week: 'days',
  start_time: 'start_time',
  anchor_date: 'anchor_date',
  interval_days: 'interval_days',
  duration_minutes: 'duration',
  area_ids: 'areas',
  pattern: 'mow',
  blade_height_mm: 'mow',
  speed_mps: 'mow',
  rotation_angles: 'rotation',
  probability_threshold: 'probability',
  precipitation_mm_threshold: 'precipitation',
  lookahead_hours: 'lookahead',
  cooldown_hours: 'cooldown',
  timezone: 'timezone',
};

/** The Scheduler's domain errors carry no pointer: their English messages name the rule. */
const FIELD_OF_MESSAGE: [RegExp, FormField][] = [
  [/^name must/, 'name'],
  [/at least one day of the week/, 'days'],
  [/needs anchor_date/, 'anchor_date'],
  [/^interval_days/, 'interval_days'],
  [/^duration_minutes/, 'duration'],
  [/area_ids/, 'areas'],
  [/^rotation_angles/, 'rotation'],
  [/^pattern/, 'mow'],
  [/^start settings/, 'mow'],
  [/^(blade_height_mm|speed_mps)/, 'mow'],
  [/time zone/, 'timezone'],
  [/probability_threshold/, 'probability'],
  [/precipitation_mm_threshold/, 'precipitation'],
  [/rain_pause hours/, 'lookahead'],
];

/** The fields a 422 answer names: request field errors by pointer, domain rules by message. */
export function serverIssues(problem: ApiProblem | null): FormIssues {
  const issues: FormIssues = {};
  if (!problem || problem.status !== 422) return issues;
  for (const error of problem.errors) {
    const parts = (error.pointer ?? '').replace(/^#/, '').split('/').filter(Boolean);
    const field = parts.map((part) => FIELD_OF_KEY[part]).find((f) => f !== undefined);
    if (field && !issues[field]) issues[field] = { code: 'server', message: error.message };
  }
  if (!problem.errors.length && problem.detail) {
    const match = FIELD_OF_MESSAGE.find(([pattern]) => pattern.test(problem.detail ?? ''));
    if (match) issues[match[1]] = { code: 'server', message: problem.detail };
  }
  return issues;
}
