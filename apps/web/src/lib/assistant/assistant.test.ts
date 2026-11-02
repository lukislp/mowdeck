import { describe, expect, it, vi } from 'vitest';

import {
  actionLabel,
  areaSummary,
  assistantEnabled,
  blockedText,
  commandOf,
  execute,
  expired,
  formatLocal,
  noteText,
  rejectionText,
  scheduleOf,
  type Executor,
  type Proposal,
  type ProposedAction,
} from './assistant';

const MOWER = '11111111-1111-4111-8111-111111111111';
const NOW = Date.parse('2026-11-01T09:30:00Z');

const areas = [
  { id: 'front', name: 'Front lawn' },
  { id: 'back', name: 'Back lawn' },
];

function later(overrides: Partial<ProposedAction> = {}): ProposedAction {
  return {
    kind: 'start_later',
    area_ids: ['back'],
    areas: [areas[1]!],
    start_at: '2026-11-02T07:00:00Z',
    start_local: '2026-11-02T08:00',
    timezone: 'Europe/Berlin',
    duration_minutes: 120,
    ...overrides,
  };
}

function immediate(kind: ProposedAction['kind'], ids: string[] = []): ProposedAction {
  return {
    kind,
    area_ids: ids,
    areas: areas.filter((a) => ids.includes(a.id)),
    start_at: null,
    start_local: null,
    timezone: null,
    duration_minutes: null,
  };
}

function proposal(actions: ProposedAction[], overrides: Partial<Proposal> = {}): Proposal {
  return {
    mower_id: MOWER,
    mower_name: 'Luba',
    interpreter: 'provider',
    model: 'fake/assistant',
    executable: actions.length > 0,
    actions,
    rejected: [],
    notes: [],
    unavailable: {},
    areas,
    created_at: '2026-11-01T09:30:00Z',
    expires_at: '2026-11-01T09:35:00Z',
    ...overrides,
  };
}

function executor(results: boolean[] = []): Executor & {
  calls: unknown[];
} {
  const calls: unknown[] = [];
  const next = () => results.shift() ?? true;
  return {
    calls,
    command: vi.fn(async (_id, command) => {
      calls.push(command);
      return next();
    }),
    schedule: vi.fn(async (body) => {
      calls.push(body);
      return next();
    }),
  };
}

describe('assistantEnabled', () => {
  it('needs the announced UI area and fails closed without areas', () => {
    expect(assistantEnabled([{ route: '/schedules' }, { route: '/assistant' }])).toBe(true);
    expect(assistantEnabled([{ route: '/schedules' }])).toBe(false);
    expect(assistantEnabled(null)).toBe(false);
  });
});

describe('commandOf and scheduleOf', () => {
  it('maps every immediate kind to exactly one Fleet command', () => {
    expect(commandOf(immediate('start_now', ['back']))).toEqual({
      kind: 'start_mow',
      area_ids: ['back'],
    });
    expect(commandOf(immediate('pause'))).toEqual({ kind: 'pause_mow' });
    expect(commandOf(immediate('resume'))).toEqual({ kind: 'resume_mow' });
    expect(commandOf(immediate('dock'))).toEqual({ kind: 'return_to_dock' });
    expect(commandOf(later())).toBeNull();
  });

  it('builds a one-off schedule in the user time zone', () => {
    expect(scheduleOf(later(), MOWER, 'Chat · Back lawn')).toEqual({
      name: 'Chat · Back lawn',
      target_type: 'mower',
      target_id: MOWER,
      kind: 'once',
      days_of_week: [],
      anchor_date: '2026-11-02',
      start_time: '08:00',
      duration_minutes: 120,
      timezone: 'Europe/Berlin',
      area_ids: ['back'],
      enabled: true,
      pause_running_jobs_on_rain: true,
      return_to_dock_at_window_end: false,
    });
    expect(scheduleOf(later(), MOWER, 'x'.repeat(150)).name).toHaveLength(100);
  });
});

describe('execute', () => {
  it('runs the actions in order through the regular endpoints', async () => {
    const run = executor();
    const order = proposal([immediate('dock'), later()]);
    const result = await execute(order, run, NOW);
    expect(result).toEqual({ done: 2, failedAt: null });
    expect(run.calls).toEqual([
      { kind: 'return_to_dock' },
      expect.objectContaining({ kind: 'once', anchor_date: '2026-11-02', area_ids: ['back'] }),
    ]);
  });

  it('stops at the first refusal', async () => {
    const run = executor([false]);
    const result = await execute(proposal([immediate('dock'), later()]), run, NOW);
    expect(result).toEqual({ done: 0, failedAt: 0 });
    expect(run.schedule).not.toHaveBeenCalled();
  });

  it('never runs an expired or not executable proposal', async () => {
    const run = executor();
    const order = proposal([immediate('dock')]);
    expect(expired(order, Date.parse('2026-11-01T09:35:00Z'))).toBe(true);
    expect(await execute(order, run, Date.parse('2026-11-01T09:36:00Z'))).toEqual({
      done: 0,
      failedAt: 0,
    });
    const refused = proposal([immediate('dock')], {
      executable: false,
      rejected: [{ code: 'unknown_kind', kind: null }],
    });
    expect(await execute(refused, run, NOW)).toEqual({ done: 0, failedAt: 0 });
    expect(run.calls).toEqual([]);
  });
});

describe('texts', () => {
  it('summarises areas, times, notes and rejections', () => {
    const order = proposal([immediate('start_now', ['front', 'back'])]);
    expect(areaSummary(order.actions[0]!, order)).toBe('All areas');
    expect(areaSummary(later(), order)).toBe('Back lawn');
    expect(areaSummary(immediate('dock'), order)).toBe('');
    expect(formatLocal('2026-11-02T08:00', 'de')).toContain('08:00');
    expect(formatLocal('nonsense', 'en')).toBe('nonsense');
    expect(noteText('timed_resume')).toContain('set time');
    expect(noteText('manual_drive')).toContain('drive controls');
    expect(rejectionText('time_past')).toContain('passed');
    expect(rejectionText('unknown_area')).toContain('area');
    expect(rejectionText('malformed')).toContain('not clear enough');
  });

  it('labels every action kind', () => {
    const kinds = ['start_now', 'start_later', 'pause', 'resume', 'dock'] as const;
    const labels = kinds.map((kind) => actionLabel(kind));
    expect(new Set(labels).size).toBe(kinds.length);
    expect(labels.every((label) => label.length > 0)).toBe(true);
  });

  it('explains a mower that offers nothing', () => {
    const all = { start_now: 'permission', start_later: 'permission', pause: 'permission' };
    const viewer = proposal([], {
      unavailable: { ...all, resume: 'permission', dock: 'permission' },
    } as Partial<Proposal>);
    expect(blockedText(viewer)).toContain('not control');
    expect(blockedText(proposal([]))).toBeNull();
  });
});
