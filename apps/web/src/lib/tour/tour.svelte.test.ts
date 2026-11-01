import { describe, expect, it, vi } from 'vitest';

import type { TourContext, TourOutcome, TourStepId } from './steps';
import { Tour, type TourDeps } from './tour.svelte';

const WITH_MOWER: TourContext = {
  mowers: 1,
  mowerId: 'm1',
  capabilities: { manual_drive: true },
  canManage: true,
  modules: new Set(['schedules', 'notifications']),
  demo: false,
};

interface Fake {
  deps: TourDeps;
  saved: TourOutcome[];
  navigated: string[];
  resumeAt: { value: TourStepId | null };
  path: { value: string };
}

function fake(
  options: {
    outcome?: TourOutcome | null;
    context?: TourContext;
    missing?: string[];
    setup?: 'new' | 'active' | 'dismissed' | 'completed' | null;
    optedOut?: boolean;
  } = {},
): Fake {
  const saved: TourOutcome[] = [];
  const navigated: string[] = [];
  const resumeAt = { value: null as TourStepId | null };
  const path = { value: '/mowers' };
  const missing = new Set(options.missing ?? []);
  const deps: TourDeps = {
    store: {
      load: vi.fn(async () => (options.outcome === undefined ? 'new' : options.outcome)),
      save: vi.fn(async (outcome: TourOutcome) => {
        saved.push(outcome);
      }),
    },
    context: vi.fn(async () => options.context ?? WITH_MOWER),
    currentPath: () => path.value,
    navigate: vi.fn(async (to: string) => {
      navigated.push(to);
      path.value = to.split('#')[0]!;
    }),
    findTarget: vi.fn(async (target: string) =>
      missing.has(target) ? null : document.createElement('div'),
    ),
    resume: {
      read: () => resumeAt.value,
      write: (step) => (resumeAt.value = step),
    },
    optedOut: () => options.optedOut ?? false,
    setupStatus: vi.fn(async () => (options.setup === undefined ? 'new' : options.setup)),
    targetTimeoutMs: 1,
  };
  return { deps, saved, navigated, resumeAt, path };
}

describe('Tour', () => {
  it('starts at the welcome, records that it was seen, and steps through the pages', async () => {
    const f = fake();
    const tour = new Tour(f.deps);
    await tour.start();
    expect(tour.running).toBe(true);
    expect(tour.step?.id).toBe('welcome');
    expect(tour.first).toBe(true);
    expect(f.saved).toEqual(['seen']);
    expect(f.resumeAt.value).toBe('welcome');

    await tour.next();
    expect(tour.step?.id).toBe('overview');
    expect(tour.target).toBeInstanceOf(HTMLElement);
    expect(f.navigated).toEqual([]); // already on /mowers

    await tour.next();
    expect(tour.step?.id).toBe('map');
    expect(f.navigated).toEqual(['/mowers/m1']);
    await tour.next();
    expect(tour.step?.id).toBe('commands');
    // Same page: no second navigation.
    expect(f.navigated).toEqual(['/mowers/m1']);

    await tour.back();
    expect(tour.step?.id).toBe('map');
    expect(tour.progress).toEqual({ current: 3, total: 10 });
  });

  it('finishes after the last step as done and forgets its position', async () => {
    const f = fake();
    const tour = new Tour(f.deps);
    await tour.start({ at: 'settings' });
    expect(tour.step?.id).toBe('settings');
    await tour.next();
    expect(tour.step?.id).toBe('restart');
    expect(tour.last).toBe(true);
    // Same page (the hash only): the target is scrolled into view instead.
    expect(f.navigated).toEqual(['/settings']);
    await tour.next();
    expect(tour.running).toBe(false);
    expect(tour.outcome).toBe('done');
    expect(f.saved).toEqual(['seen', 'done']);
    expect(f.resumeAt.value).toBeNull();
  });

  it('skips with Esc semantics: ends at once and stores skipped', async () => {
    const f = fake();
    const tour = new Tour(f.deps);
    await tour.start();
    tour.skip();
    expect(tour.running).toBe(false);
    expect(tour.step).toBeNull();
    expect(f.saved).toEqual(['seen', 'skipped']);
  });

  it('never turns a finished tour into a skipped one when taken again', async () => {
    const f = fake({ outcome: 'done' });
    const tour = new Tour(f.deps);
    await tour.start();
    expect(f.saved).toEqual([]); // not "seen" again
    tour.skip();
    expect(tour.outcome).toBe('done');
    expect(f.saved).toEqual([]);
  });

  it('passes over a step whose target is missing, forward and backward', async () => {
    const f = fake({ missing: ['spot', 'zones'] });
    const tour = new Tour(f.deps);
    await tour.start({ at: 'commands' });
    await tour.next();
    expect(tour.step?.id).toBe('schedules');
    expect(tour.missed).toEqual(new Set(['spot', 'zones']));
    expect(tour.progress).toEqual({ current: 5, total: 8 });
    await tour.back();
    expect(tour.step?.id).toBe('commands');
  });

  it('goes forward when the step it starts at is missing', async () => {
    const f = fake({ missing: ['overview'] });
    const tour = new Tour(f.deps);
    await tour.start({ at: 'overview' });
    expect(tour.step?.id).toBe('map');
    await tour.back();
    expect(tour.step?.id).toBe('welcome');
  });

  it('ends as done when every remaining target is missing', async () => {
    const f = fake({ missing: ['tour-restart'] });
    const tour = new Tour(f.deps);
    await tour.start({ at: 'restart' });
    expect(tour.running).toBe(false);
    expect(tour.outcome).toBe('done');
  });

  it('ignores a slow step that the user already moved past', async () => {
    const f = fake();
    let release: (value: HTMLElement | null) => void = () => undefined;
    f.deps.findTarget = vi.fn(
      () =>
        new Promise<HTMLElement | null>((resolve) => {
          release = resolve;
        }),
    );
    const tour = new Tour(f.deps);
    await tour.start();
    const slow = tour.next(); // overview: waits for its target
    tour.skip();
    release(null);
    await slow;
    expect(tour.running).toBe(false);
    expect(tour.missed.size).toBe(0);
  });

  it('starts by itself once for a new user on the mower list', async () => {
    const f = fake({ context: { ...WITH_MOWER, mowers: 0, mowerId: null } });
    const tour = new Tour(f.deps);
    expect(await tour.maybeAutoStart('/mowers', false)).toBe(true);
    expect(tour.running).toBe(true);
    expect(tour.steps.map((s) => s.id)).toEqual(['welcome', 'restart', 'add-mower']);
    tour.skip();
    // Checked once per session.
    expect(await tour.maybeAutoStart('/mowers', false)).toBe(false);
  });

  it('does not start by itself for a set-up account, a seen tour, another page or an opt-out', async () => {
    expect(await new Tour(fake().deps).maybeAutoStart('/mowers', false)).toBe(false); // has a mower
    // The mower list could not be read: unknown, not "new".
    expect(
      await new Tour(
        fake({ context: { ...WITH_MOWER, mowers: null, mowerId: null } }).deps,
      ).maybeAutoStart('/mowers', false),
    ).toBe(false);
    expect(
      await new Tour(fake({ outcome: 'skipped', setup: 'active' }).deps).maybeAutoStart(
        '/mowers',
        false,
      ),
    ).toBe(false);
    expect(await new Tour(fake({ setup: 'active' }).deps).maybeAutoStart('/settings', false)).toBe(
      false,
    );
    expect(
      await new Tour(fake({ setup: 'active', optedOut: true }).deps).maybeAutoStart(
        '/mowers',
        false,
      ),
    ).toBe(false);
    expect(
      await new Tour(fake({ outcome: null, setup: 'active' }).deps).maybeAutoStart(
        '/mowers',
        false,
      ),
    ).toBe(false);
  });

  it('starts in the demo for a new browser without asking the setup status', async () => {
    const f = fake({ setup: 'completed', context: { ...WITH_MOWER, demo: true } });
    const tour = new Tour(f.deps);
    expect(await tour.maybeAutoStart('/mowers', true)).toBe(true);
    expect(f.deps.setupStatus).not.toHaveBeenCalled();
  });

  it('continues a running tour after a reload, once', async () => {
    const f = fake();
    f.resumeAt.value = 'schedules';
    const tour = new Tour(f.deps);
    expect(await tour.resume()).toBe(true);
    expect(tour.step?.id).toBe('schedules');
    expect(f.navigated).toEqual(['/schedules']);
    tour.skip();
    expect(await tour.resume()).toBe(false);
    // The resumed tour was this session's automatic start.
    expect(await tour.maybeAutoStart('/mowers', false)).toBe(false);
  });

  it('looks for a target again when it went away', async () => {
    const f = fake();
    const tour = new Tour(f.deps);
    await tour.start({ at: 'map' });
    const calls = vi.mocked(f.deps.findTarget).mock.calls.length;
    await tour.retarget();
    expect(vi.mocked(f.deps.findTarget).mock.calls.length).toBe(calls + 1);
    expect(tour.step?.id).toBe('map');
  });

  it('forgets everything on sign-out', async () => {
    const tour = new Tour(fake().deps);
    await tour.start();
    tour.reset();
    expect(tour.running).toBe(false);
    expect(tour.steps).toEqual([]);
    expect(tour.outcome).toBeNull();
    expect(tour.context).toBeNull();
  });

  it('keeps going when a navigation is refused', async () => {
    const f = fake();
    f.deps.navigate = vi.fn(async () => {
      throw new Error('blocked');
    });
    const tour = new Tour(f.deps);
    await tour.start({ at: 'schedules' });
    expect(tour.step?.id).toBe('schedules');
    expect(tour.running).toBe(true);
  });
});
