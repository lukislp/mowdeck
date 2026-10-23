import { describe, expect, it } from 'vitest';

import { JOB_TICKS, formatRunTime, jobView, litTicks } from './job';

const areas = [
  { id: 'front', name: 'Front lawn' },
  { id: 'back', name: null },
];
const namer = (area: { id: string; name?: string | null }) => area.name ?? `Area ${area.id}`;

describe('jobView', () => {
  it('is null without a job or outside a job activity', () => {
    expect(jobView(null, 'mowing', areas, namer)).toBeNull();
    expect(jobView(undefined, 'mowing', areas, namer)).toBeNull();
    for (const activity of ['docked', 'charging', 'idle', 'offline', 'updating', null]) {
      expect(jobView({ progress_percent: 40 }, activity, areas, namer)).toBeNull();
    }
  });

  it('names the area through the map and rounds the progress', () => {
    const view = jobView(
      {
        progress_percent: 41.6,
        area_id: 'front',
        elapsed_s: 600,
        remaining_s: 900,
        mowed_m2: 37.2,
      },
      'mowing',
      areas,
      namer,
    );
    expect(view).toEqual({
      percent: 42,
      area: { kind: 'area', id: 'front', name: 'Front lawn' },
      elapsedS: 600,
      remainingS: 900,
      mowedM2: 37.2,
      paused: false,
    });
  });

  it('names an area the map does not know by its id', () => {
    const view = jobView({ area_id: 'gone' }, 'mowing', areas, namer);
    expect(view?.area).toEqual({ kind: 'area', id: 'gone', name: 'Area gone' });
  });

  it('reads a job without an area as a transfer while driving, else as not reported', () => {
    expect(jobView({ progress_percent: 10 }, 'mowing', areas, namer)?.area.kind).toBe('transit');
    expect(jobView({ progress_percent: 10 }, 'returning', areas, namer)?.area.kind).toBe('transit');
    expect(jobView({ progress_percent: 10 }, 'paused', areas, namer)?.area.kind).toBe('unknown');
  });

  it('marks a paused job and one an error interrupted', () => {
    expect(jobView({}, 'paused', areas, namer)?.paused).toBe(true);
    expect(jobView({}, 'error', areas, namer)?.paused).toBe(true);
    expect(jobView({}, 'returning', areas, namer)?.paused).toBe(false);
  });

  it('clamps out-of-range values and keeps missing ones null', () => {
    const view = jobView(
      { progress_percent: 140, elapsed_s: -5, remaining_s: null, mowed_m2: undefined },
      'mowing',
      areas,
      namer,
    );
    expect(view?.percent).toBe(100);
    expect(view?.elapsedS).toBe(0);
    expect(view?.remainingS).toBeNull();
    expect(view?.mowedM2).toBeNull();
    expect(jobView({ progress_percent: -3 }, 'mowing', areas, namer)?.percent).toBe(0);
    expect(jobView({ progress_percent: Number.NaN }, 'mowing', areas, namer)?.percent).toBeNull();
  });
});

describe('formatRunTime', () => {
  it.each([
    [0, '< 1 min'],
    [59, '< 1 min'],
    [60, '1 min'],
    [47 * 60 + 30, '47 min'],
    [3600, '1 h 00'],
    [2 * 3600 + 5 * 60, '2 h 05'],
    [Number.NaN, '< 1 min'],
  ])('%s s reads %s', (seconds, text) => {
    expect(formatRunTime(seconds)).toBe(text);
  });
});

describe('litTicks', () => {
  it('lights one tick per started 5 %', () => {
    expect(litTicks(null)).toBe(0);
    expect(litTicks(0)).toBe(0);
    expect(litTicks(1)).toBe(1);
    expect(litTicks(5)).toBe(1);
    expect(litTicks(6)).toBe(2);
    expect(litTicks(50)).toBe(10);
    expect(litTicks(100)).toBe(JOB_TICKS);
  });
});
