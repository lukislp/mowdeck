import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import {
  OUTCOME_KEYS,
  TOUR_STEPS,
  TOUR_STEP_IDS,
  firstFrom,
  keysWith,
  moveFrom,
  outcomeOf,
  progressOf,
  shouldAutoStart,
  tourSteps,
  type AutoStartInput,
  type TourContext,
} from './steps';

const FULL: TourContext = {
  mowers: 2,
  mowerId: 'm 1',
  capabilities: { manual_drive: true },
  canManage: true,
  modules: new Set(['mowers', 'schedules', 'notifications', 'settings']),
  demo: false,
};

const ids = (context: TourContext) => tourSteps(context).map((step) => step.id);

describe('tourSteps', () => {
  it('walks a user with a mower through every core screen, in order', () => {
    expect(ids(FULL)).toEqual([
      'welcome',
      'overview',
      'map',
      'commands',
      'spot',
      'zones',
      'schedules',
      'inbox',
      'settings',
      'restart',
    ]);
  });

  it('leads a user without a mower to adding the first one', () => {
    const steps = ids({ ...FULL, mowers: 0, mowerId: null, capabilities: null });
    expect(steps).toEqual(['welcome', 'restart', 'add-mower']);
    expect(steps.at(-1)).toBe('add-mower');
  });

  it('never takes a mower list it could not read for "no mowers"', () => {
    const steps = ids({ ...FULL, mowers: null, mowerId: null, capabilities: null });
    expect(steps).toEqual(['welcome', 'restart']);
  });

  it('leaves out spot mowing when the mower cannot drive (or its capabilities are unknown)', () => {
    expect(ids({ ...FULL, capabilities: { manual_drive: false } })).not.toContain('spot');
    expect(ids({ ...FULL, capabilities: null })).not.toContain('spot');
  });

  it('leaves out the steps of modules the user does not see', () => {
    const steps = ids({ ...FULL, modules: new Set(['mowers']) });
    expect(steps).not.toContain('schedules');
    expect(steps).not.toContain('inbox');
    expect(steps).toContain('settings');
  });

  it('routes the mower steps to the first mower, encoded', () => {
    const map = tourSteps(FULL).find((step) => step.id === 'map');
    expect(map).toEqual({ id: 'map', route: '/mowers/m%201', target: 'map' });
    expect(tourSteps(FULL)[0]).toEqual({ id: 'welcome', route: null, target: null });
  });

  it('has a definition for every step id, each id once', () => {
    expect(TOUR_STEPS.map((def) => def.id)).toEqual([...TOUR_STEP_IDS]);
  });

  it('always starts with the welcome and always shows where to restart', () => {
    fc.assert(
      fc.property(
        fc.record({
          mowers: fc.nat(5),
          manual: fc.option(fc.boolean()),
          canManage: fc.boolean(),
          schedules: fc.boolean(),
          notifications: fc.boolean(),
          demo: fc.boolean(),
        }),
        (r) => {
          const steps = ids({
            mowers: r.mowers,
            mowerId: r.mowers > 0 ? 'm1' : null,
            capabilities: r.manual === null ? null : { manual_drive: r.manual },
            canManage: r.canManage,
            modules: new Set([
              ...(r.schedules ? ['schedules'] : []),
              ...(r.notifications ? ['notifications'] : []),
            ]),
            demo: r.demo,
          });
          expect(steps[0]).toBe('welcome');
          expect(steps).toContain('restart');
          // Mower screens only with a mower; "add mower" only without one.
          expect(steps.includes('map')).toBe(r.mowers > 0);
          expect(steps.includes('add-mower')).toBe(r.mowers === 0);
        },
      ),
    );
  });
});

describe('moving over the steps', () => {
  const steps = ['a', 'b', 'c', 'd'].map((id) => ({ id }));

  it('moves forward and back, ending after the last step', () => {
    const none = new Set<string>();
    expect(moveFrom(steps, none, 0, 1)).toBe(1);
    expect(moveFrom(steps, none, 3, 1)).toBe('end');
    expect(moveFrom(steps, none, 2, -1)).toBe(1);
    expect(moveFrom(steps, none, 0, -1)).toBe(0);
  });

  it('passes over missed steps in both directions', () => {
    const missed = new Set(['b', 'c']);
    expect(moveFrom(steps, missed, 0, 1)).toBe(3);
    expect(moveFrom(steps, missed, 3, -1)).toBe(0);
    expect(moveFrom(steps, new Set(['d']), 2, 1)).toBe('end');
    expect(moveFrom(steps, new Set(['a']), 1, -1)).toBe(1);
  });

  it('starts at the first step that is not missed', () => {
    expect(firstFrom(steps, new Set(), 0)).toBe(0);
    expect(firstFrom(steps, new Set(['a', 'b']), 0)).toBe(2);
    expect(firstFrom(steps, new Set(['a', 'b', 'c', 'd']), 0)).toBe('end');
    expect(firstFrom(steps, new Set(), 9)).toBe('end');
  });

  it('counts the progress among the steps still shown', () => {
    expect(progressOf(steps, new Set(), 2)).toEqual({ current: 3, total: 4 });
    expect(progressOf(steps, new Set(['b']), 2)).toEqual({ current: 2, total: 3 });
  });
});

describe('outcome', () => {
  it('reads the most final key', () => {
    expect(outcomeOf([])).toBe('new');
    expect(outcomeOf(['mower-map', OUTCOME_KEYS.seen])).toBe('seen');
    expect(outcomeOf([OUTCOME_KEYS.seen, OUTCOME_KEYS.skipped])).toBe('skipped');
    expect(outcomeOf([OUTCOME_KEYS.skipped, OUTCOME_KEYS.done])).toBe('done');
  });

  it('replaces only its own keys and keeps the others', () => {
    expect(keysWith(['mower-map', 'tour-seen', 'future-key'], 'done')).toEqual([
      'mower-map',
      'future-key',
      'tour-done',
    ]);
  });

  it('keeps every key within the shape Identity accepts', () => {
    for (const key of Object.values(OUTCOME_KEYS)) {
      expect(key).toMatch(/^[a-z][a-z0-9-]{0,31}$/);
    }
  });
});

describe('shouldAutoStart', () => {
  const NEW_USER: AutoStartInput = {
    path: '/mowers',
    outcome: 'new',
    demo: false,
    optedOut: false,
    setup: 'new',
    mowers: 0,
  };

  it('starts for a new account on the mower list', () => {
    expect(shouldAutoStart(NEW_USER)).toBe(true);
    expect(shouldAutoStart({ ...NEW_USER, setup: 'active', mowers: 3 })).toBe(true);
  });

  it('starts once: never after it was seen, done or skipped, or when unknown', () => {
    for (const outcome of ['seen', 'done', 'skipped', null] as const) {
      expect(shouldAutoStart({ ...NEW_USER, outcome })).toBe(false);
    }
  });

  it('does not bombard an account set up before the tour existed', () => {
    expect(shouldAutoStart({ ...NEW_USER, mowers: 2 })).toBe(false);
    expect(shouldAutoStart({ ...NEW_USER, setup: 'dismissed' })).toBe(false);
    expect(shouldAutoStart({ ...NEW_USER, setup: 'completed' })).toBe(false);
    expect(shouldAutoStart({ ...NEW_USER, setup: null })).toBe(false);
    expect(shouldAutoStart({ ...NEW_USER, mowers: null })).toBe(false);
  });

  it('never over a deep link, nor on a device that opted out', () => {
    expect(shouldAutoStart({ ...NEW_USER, path: '/mowers/abc' })).toBe(false);
    expect(shouldAutoStart({ ...NEW_USER, optedOut: true })).toBe(false);
  });

  it('starts for every new browser in the demo, whatever the shared user did', () => {
    expect(shouldAutoStart({ ...NEW_USER, demo: true, setup: 'completed', mowers: 3 })).toBe(true);
    expect(shouldAutoStart({ ...NEW_USER, demo: true, outcome: 'skipped' })).toBe(false);
  });
});
