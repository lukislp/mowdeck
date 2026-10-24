import { describe, expect, it } from 'vitest';

import { ApiProblem } from '$lib/api/problem';

import {
  emptyForm,
  fromSchedule,
  isValidTimeZone,
  localDate,
  parseNumber,
  serverIssues,
  toBody,
  validateForm,
  type ScheduleFormState,
} from './form';
import { emptyMow } from './mow-options';
import type { Schedule } from './types';

const MOWER = '11111111-1111-4111-8111-111111111111';

function form(patch: Partial<ScheduleFormState> = {}): ScheduleFormState {
  return { ...emptyForm('2026-10-10', { type: 'mower', id: MOWER }), name: 'Morning', ...patch };
}

const stored: Schedule = {
  id: 's1',
  name: 'Front',
  target_type: 'mower',
  target_id: MOWER,
  kind: 'weekly',
  days_of_week: ['mon', 'thu'],
  start_time: '07:30:00',
  duration_minutes: 90,
  anchor_date: null,
  interval_days: null,
  area_ids: ['front', 'back'],
  pattern: {
    name: 'diagonal',
    angle_deg: 45,
    border_laps: null,
    spacing_m: 0.25,
    obstacle_detection: 'sensitive',
    obstacle_laps: null,
    order: null,
  },
  blade_height_mm: 40,
  speed_mps: null,
  rotation_angles: [0, 90],
  rotation_index: 1,
  rain_pause: {
    enabled: true,
    probability_threshold: 70,
    precipitation_mm_threshold: 0.8,
    lookahead_hours: 4,
    cooldown_hours: 1,
    run_when_unknown: false,
  },
  pause_running_jobs_on_rain: true,
  return_to_dock_at_window_end: true,
  timezone: 'Europe/Berlin',
  timezone_override: null,
  enabled: true,
  skip_until: null,
  next_run_at: null,
  last_run_at: null,
  permission_skips: 0,
  disabled_reason: null,
  owner: { kind: 'user', subject: '22222222-2222-4222-8222-222222222222' },
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
};

describe('parseNumber and helpers', () => {
  it('accepts a decimal comma and rejects text', () => {
    expect(parseNumber('0,5')).toBe(0.5);
    expect(parseNumber(' -12.5 ')).toBe(-12.5);
    expect(parseNumber('')).toBeNull();
    expect(parseNumber('1e3')).toBeNull();
    expect(parseNumber('abc')).toBeNull();
  });

  it('knows IANA time zones and formats local dates', () => {
    expect(isValidTimeZone('Europe/Berlin')).toBe(true);
    expect(isValidTimeZone('Mars/Olympus')).toBe(false);
    expect(localDate(new Date(2026, 0, 5))).toBe('2026-01-05');
  });
});

describe('validateForm', () => {
  it('accepts the defaults with a name and a target', () => {
    expect(validateForm(form())).toEqual({});
  });

  it('mirrors the Scheduler rules', () => {
    const issues = validateForm(
      form({
        name: ' ',
        targetId: '',
        days: [],
        startTime: '25:00',
        duration: '0',
        rotation: [0, 500],
        probability: '101',
        precipitation: 'x',
        lookahead: '2.5',
        cooldown: '',
        timezone: 'Nowhere/City',
      }),
    );
    expect(issues).toEqual({
      name: { code: 'required' },
      target: { code: 'required' },
      days: { code: 'days' },
      start_time: { code: 'format' },
      duration: { code: 'range', min: 1, max: 1440 },
      rotation: { code: 'range', min: -360, max: 360 },
      probability: { code: 'range', min: 0, max: 100 },
      precipitation: { code: 'number' },
      lookahead: { code: 'integer' },
      cooldown: { code: 'required' },
      timezone: { code: 'timezone' },
    });
  });

  it('checks every-N-days fields only for that kind', () => {
    expect(
      validateForm(form({ kind: 'every_n_days', anchorDate: '', intervalDays: '400', days: [] })),
    ).toEqual({
      anchor_date: { code: 'required' },
      interval_days: { code: 'range', min: 1, max: 365 },
    });
    expect(validateForm(form({ kind: 'every_n_days', anchorDate: '12.10.2026' }))).toEqual({
      anchor_date: { code: 'format' },
    });
  });

  it('refuses areas on a group, too many angles and long names', () => {
    expect(validateForm(form({ targetType: 'group', areaIds: ['a'] })).areas).toEqual({
      code: 'group_areas',
    });
    expect(validateForm(form({ rotation: Array.from({ length: 25 }, () => 0) })).rotation).toEqual({
      code: 'count',
      max: 24,
    });
    expect(validateForm(form({ name: 'x'.repeat(101) })).name).toEqual({ code: 'count', max: 100 });
  });
});

describe('toBody and fromSchedule', () => {
  it('builds a weekly body in week order', () => {
    const mow = emptyMow();
    mow.pattern.angle = 15;
    mow.pattern.options.order = 'grid_first';
    mow.setpoints.speedMps = 0.6;
    const body = toBody(form({ days: ['fri', 'mon'], areaIds: ['back', 'front'], mow }));
    expect(body).toMatchObject({
      name: 'Morning',
      target_type: 'mower',
      target_id: MOWER,
      kind: 'weekly',
      days_of_week: ['mon', 'fri'],
      anchor_date: null,
      interval_days: null,
      duration_minutes: 120,
      area_ids: ['back', 'front'],
      pattern: { angle_deg: 15, order: 'grid_first' },
      blade_height_mm: null,
      speed_mps: 0.6,
      timezone: null,
    });
    expect(body.rain_pause).toEqual({
      enabled: true,
      probability_threshold: 60,
      precipitation_mm_threshold: 0.5,
      lookahead_hours: 3,
      cooldown_hours: 2,
      run_when_unknown: true,
    });
  });

  it('builds an every-N-days body and drops areas for groups', () => {
    const body = toBody(
      form({
        kind: 'every_n_days',
        intervalDays: '3',
        targetType: 'group',
        areaIds: ['x'],
        precipitation: '1,5',
        timezone: ' Europe/Vienna ',
      }),
    );
    expect(body).toMatchObject({
      days_of_week: [],
      anchor_date: '2026-10-10',
      interval_days: 3,
      area_ids: [],
      pattern: null,
      timezone: 'Europe/Vienna',
    });
    expect(body.rain_pause?.precipitation_mm_threshold).toBe(1.5);
  });

  it('round-trips a stored schedule with its start settings', () => {
    const state = fromSchedule(stored);
    expect(state.mow.pattern).toMatchObject({ name: 'diagonal', angle: 45, laps: null });
    expect(state).toMatchObject({
      startTime: '07:30',
      days: ['mon', 'thu'],
      rotation: [0, 90],
      probability: '70',
      runWhenUnknown: false,
      returnAtWindowEnd: true,
      timezone: '',
    });
    expect(toBody(state)).toMatchObject({
      pattern: {
        name: 'diagonal',
        angle_deg: 45,
        spacing_m: 0.25,
        obstacle_detection: 'sensitive',
      },
      blade_height_mm: 40,
      speed_mps: null,
    });
    expect(toBody({ ...state, mow: emptyMow() })).toMatchObject({
      pattern: null,
      blade_height_mm: null,
      speed_mps: null,
    });
  });
});

describe('serverIssues', () => {
  it('maps request field errors by pointer', () => {
    const problem = new ApiProblem({
      status: 422,
      errors: [
        { pointer: '#/rain_pause/probability_threshold', message: 'too big' },
        { pointer: '#/rotation_angles/3', message: 'not a number' },
        { pointer: '#/unknown', message: 'ignored' },
      ],
    });
    expect(serverIssues(problem)).toEqual({
      probability: { code: 'server', message: 'too big' },
      rotation: { code: 'server', message: 'not a number' },
    });
  });

  it('maps the domain rules by their message', () => {
    const issue = (detail: string) =>
      serverIssues(new ApiProblem({ status: 422, detail, errors: [] }));
    expect(issue('a weekly schedule needs at least one day of the week')).toHaveProperty('days');
    expect(issue("unknown time zone 'X/Y'")).toHaveProperty('timezone');
    expect(issue('a group schedule mows all areas of each mower (no area_ids)')).toHaveProperty(
      'areas',
    );
    expect(
      issue("start settings do not fit Yuka: pattern 'diagonal' is not offered"),
    ).toHaveProperty('mow');
    expect(issue('something else')).toEqual({});
  });

  it('ignores other statuses', () => {
    expect(serverIssues(new ApiProblem({ status: 403, detail: 'name must' }))).toEqual({});
    expect(serverIssues(null)).toEqual({});
  });
});
