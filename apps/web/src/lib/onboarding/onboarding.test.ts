import { describe, expect, it, vi } from 'vitest';

import { stepHref, stepText } from './labels';
import { NO_CONTEXT, Onboarding, type OnboardingDeps } from './onboarding.svelte';
import {
  NO_FACTS,
  STEP_KEYS,
  hintVisible,
  isComplete,
  nextOpen,
  offer,
  settledCount,
  stepViews,
  type Context,
  type Facts,
  type Progress,
} from './steps';

const ALL: Context = {
  canManage: true,
  cloud: true,
  schedules: true,
  notifications: true,
  passkeys: true,
  signInLinks: true,
  appExtras: false,
};

const EMPTY: Facts = {
  ...NO_FACTS,
  mowers: 0,
  cloudAccounts: 0,
  map: false,
  notifications: false,
  passkeys: 0,
  schedules: 0,
  signInLinks: 0,
};

describe('steps', () => {
  it('lists the steps that apply, in order, all open for a new account', () => {
    const views = stepViews(EMPTY, ALL, []);
    expect(views.map((v) => v.key)).toEqual([
      'cloud-account',
      'mower',
      'map',
      'notifications',
      'passkey',
      'schedule',
      'sign-in-links',
    ]);
    expect(views.every((v) => v.state === 'open')).toBe(true);
    expect(nextOpen(views)?.key).toBe('cloud-account');
  });

  it('shows the native-only step only inside the iOS app', () => {
    expect(stepViews(EMPTY, { ...ALL, appExtras: true }, []).at(-1)?.key).toBe('app-extras');
  });

  it('asks for a cloud account only where the installation takes mowers from one', () => {
    expect(stepViews(EMPTY, { ...ALL, cloud: false }, [])[0]?.key).toBe('mower');
  });

  it('leaves out what the user may not do or the installation does not offer', () => {
    const keys = stepViews(EMPTY, { ...NO_CONTEXT }, []).map((v) => v.key);
    expect(keys).toEqual(['map']);
  });

  it('derives done from the facts; done wins over skipped', () => {
    const facts: Facts = { ...EMPTY, mowers: 1, map: true, schedules: 2 };
    const views = stepViews(facts, ALL, ['schedule', 'passkey']);
    const state = Object.fromEntries(views.map((v) => [v.key, v.state]));
    // A mower of another kind needs no cloud account.
    expect(state['cloud-account']).toBe('done');
    expect(state.mower).toBe('done');
    expect(state.map).toBe('done');
    expect(state.schedule).toBe('done');
    expect(state.passkey).toBe('skipped');
    expect(state.notifications).toBe('open');
    expect(settledCount(views)).toBe(5);
  });

  it('never counts an unknown fact as done', () => {
    expect(stepViews(NO_FACTS, ALL, []).every((v) => v.state === 'open')).toBe(true);
  });

  it('is complete when every required step is done or skipped; optional ones may stay open', () => {
    const facts: Facts = { ...EMPTY, mowers: 1, map: true, notifications: true };
    expect(isComplete(stepViews(facts, ALL, []))).toBe(false);
    expect(isComplete(stepViews(facts, ALL, ['schedule']))).toBe(true);
  });

  it('offers the tour to new accounts without a mower, a hint to set-up accounts', () => {
    expect(offer('new', { mowers: 0 }, { canManage: true })).toBe('tour');
    expect(offer('new', { mowers: 0 }, { canManage: false })).toBe('none');
    expect(offer('new', { mowers: 2 }, { canManage: true })).toBe('hint');
    expect(offer('new', { mowers: null }, { canManage: true })).toBe('none');
    expect(offer('active', { mowers: 2 }, { canManage: true })).toBe('tour');
    expect(offer('dismissed', { mowers: 0 }, { canManage: true })).toBe('none');
    expect(offer('completed', { mowers: 0 }, { canManage: true })).toBe('none');
  });

  it('shows first-time hints only to users who started the tour, until dismissed', () => {
    const base: Progress = { status: 'active', skipped: [], hints: [] };
    expect(hintVisible(base, 'mower-map')).toBe(true);
    expect(hintVisible({ ...base, status: 'completed' }, 'mower-map')).toBe(true);
    expect(hintVisible({ ...base, hints: ['mower-map'] }, 'mower-map')).toBe(false);
    expect(hintVisible({ ...base, status: 'new' }, 'mower-map')).toBe(false);
    expect(hintVisible({ ...base, status: 'dismissed' }, 'schedules')).toBe(false);
  });
});

function deps(initial: Progress, facts: Facts = EMPTY, context: Context = ALL) {
  let stored = initial;
  const save = vi.fn(async (progress: Progress) => {
    stored = progress;
    return progress;
  });
  const d = {
    api: { load: vi.fn(async () => stored), save },
    facts: vi.fn(async () => facts),
    context: vi.fn(async () => context),
    onSaveFailed: vi.fn(),
  } satisfies OnboardingDeps;
  return d;
}

describe('Onboarding', () => {
  it('offers nothing before the choices and the facts are known', async () => {
    const tour = new Onboarding(deps({ status: 'new', skipped: [], hints: [] }));
    expect(tour.offer).toBe('none');
    await tour.load();
    expect(tour.offer).toBe('none');
    await tour.refresh();
    expect(tour.offer).toBe('tour');
  });

  it('loads the choices once', async () => {
    const d = deps({ status: 'active', skipped: ['passkey'], hints: [] });
    const tour = new Onboarding(d);
    await Promise.all([tour.load(), tour.load()]);
    expect(d.api.load).toHaveBeenCalledTimes(1);
    expect(tour.status).toBe('active');
    expect(tour.offer).toBe('tour');
  });

  it('starts, skips, resumes, dismisses and finishes, saving the whole state', async () => {
    const d = deps({ status: 'new', skipped: ['old-step'], hints: [] });
    const tour = new Onboarding(d);
    await tour.load();
    await tour.refresh();
    await tour.start();
    expect(tour.status).toBe('active');
    await tour.skip('cloud-account');
    expect(tour.next?.key).toBe('mower');
    await tour.skip('cloud-account');
    expect(tour.progress.skipped).toEqual(['old-step', 'cloud-account']);
    await tour.unskip('cloud-account');
    expect(tour.progress.skipped).toEqual(['old-step']);
    await tour.dismiss();
    expect(tour.status).toBe('dismissed');
    expect(tour.offer).toBe('none');
    await tour.finish();
    expect(d.api.save).toHaveBeenLastCalledWith({
      status: 'completed',
      skipped: ['old-step'],
      hints: [],
    });
  });

  it('dismisses a hint once and keeps it dismissed', async () => {
    const d = deps({ status: 'active', skipped: [], hints: [] });
    const tour = new Onboarding(d);
    await tour.load();
    expect(tour.hintVisible('schedules')).toBe(true);
    await tour.dismissHint('schedules');
    await tour.dismissHint('schedules');
    expect(tour.hintVisible('schedules')).toBe(false);
    expect(d.api.save).toHaveBeenCalledTimes(1);
  });

  it('keeps a change for the page when saving fails, and reports it', async () => {
    const d = deps({ status: 'active', skipped: [], hints: [] });
    d.api.save.mockRejectedValueOnce(new Error('offline'));
    const tour = new Onboarding(d);
    await tour.load();
    await tour.skip('map');
    expect(tour.progress.skipped).toEqual(['map']);
    expect(d.onSaveFailed).toHaveBeenCalledTimes(1);
  });

  it('offers nothing when the choices cannot be loaded, and tries again later', async () => {
    const d = deps({ status: 'new', skipped: [], hints: [] });
    d.api.load.mockRejectedValueOnce(new Error('down'));
    const tour = new Onboarding(d);
    await tour.load();
    await tour.refresh();
    expect(tour.failed).toBe(true);
    expect(tour.offer).toBe('none');
    expect(tour.hintVisible('mower-map')).toBe(false);
    await tour.load();
    expect(tour.failed).toBe(false);
    expect(tour.offer).toBe('tour');
  });

  it('stores a dismissal before the tour ever started as dismissed', async () => {
    const d = deps({ status: 'new', skipped: [], hints: [] }, { ...EMPTY, mowers: 3 });
    const tour = new Onboarding(d);
    await tour.load();
    await tour.refresh();
    expect(tour.offer).toBe('hint');
    await tour.dismiss();
    expect(d.api.save).toHaveBeenCalledWith({ status: 'dismissed', skipped: [], hints: [] });
    expect(tour.offer).toBe('none');
  });

  it('is complete only once the facts are known', async () => {
    const done: Facts = {
      ...EMPTY,
      mowers: 1,
      map: true,
      notifications: true,
      schedules: 1,
      passkeys: 1,
      signInLinks: 1,
    };
    const tour = new Onboarding(deps({ status: 'active', skipped: [], hints: [] }, done));
    await tour.load();
    expect(tour.complete).toBe(false);
    await tour.refresh();
    expect(tour.complete).toBe(true);
    expect(tour.next).toBeNull();
  });

  it('forgets everything on sign-out', async () => {
    const tour = new Onboarding(deps({ status: 'active', skipped: ['map'], hints: [] }));
    await tour.load();
    await tour.refresh();
    tour.reset();
    expect(tour.loaded).toBe(false);
    expect(tour.factsLoaded).toBe(false);
    expect(tour.progress.status).toBe('new');
    expect(tour.offer).toBe('none');
  });
});

describe('step links', () => {
  it('lead into the existing flows', () => {
    expect(stepHref('mower', NO_FACTS)).toBe('/mowers/new');
    expect(stepHref('map', NO_FACTS)).toBe('/mowers');
    expect(stepHref('map', { firstMowerId: 'a b' })).toBe('/mowers/a%20b');
    expect(stepHref('schedule', NO_FACTS)).toBe('/schedules/new');
    expect(stepHref('cloud-account', NO_FACTS)).toBe('/settings#linked-accounts');
    expect(stepHref('notifications', NO_FACTS)).toBe('/settings#notifications');
    expect(stepHref('passkey', NO_FACTS)).toBe('/settings#profile');
  });

  it('have a title, a text and an action for every step', () => {
    for (const key of STEP_KEYS) {
      const text = stepText(key);
      expect(text.title && text.text && text.action).toBeTruthy();
    }
  });
});
