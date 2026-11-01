/**
 * The guided tour (ADR 0073) wired to the app: the context from Fleet, Telemetry, the session
 * and the module registry; the outcome per user in Identity (next to the setup tour's choices,
 * `#lib/onboarding`) or, in the shared demo account, per browser; the position of a running tour
 * per tab, so a reload continues it.
 */
import { goto } from '$app/navigation';

import { fleetApi, telemetryApi, unwrap } from '#lib/api/client.js';
import { session } from '#lib/auth/session.svelte.js';
import { demo } from '#lib/demo/state.svelte.js';
import { onboarding } from '#lib/onboarding/runtime.js';
import { operatorAccess } from '#lib/operator/access.svelte.js';
import { builtIn, registry, visibleModules } from '#lib/shell/registry.svelte.js';

import {
  TOUR_STEP_IDS,
  keysWith,
  outcomeOf,
  type TourCapabilities,
  type TourContext,
  type TourOutcome,
  type TourStepId,
} from './steps';
import { Tour, type TourResume, type TourStore } from './tour.svelte';

/** Device switch: `off` never starts the tour by itself here (wall displays, the e2e suite). */
export const AUTOSTART_KEY = 'mowdeck.tour.autostart';
/** The outcome in the demo, per browser (every visitor shares one demo user). */
export const DEMO_OUTCOME_KEY = 'mowdeck.tour.demo';
/** The step of a running tour, per tab. */
export const RESUME_KEY = 'mowdeck.tour.run';

/** The setup tour's first-time hints the guided tour covers; finishing it dismisses them. */
const COVERED_HINTS = ['mower-map', 'schedules'];

function storage(kind: 'local' | 'session'): Storage | null {
  try {
    return kind === 'local' ? globalThis.localStorage : globalThis.sessionStorage;
  } catch {
    return null;
  }
}

function read(kind: 'local' | 'session', key: string): string | null {
  try {
    return storage(kind)?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function write(kind: 'local' | 'session', key: string, value: string | null): void {
  try {
    if (value === null) storage(kind)?.removeItem(key);
    else storage(kind)?.setItem(key, value);
  } catch {
    // no storage (private mode, blocked site data): the tour still runs, it just forgets
  }
}

async function readContext(): Promise<TourContext> {
  let ids: string[];
  try {
    const list = await unwrap(fleetApi.GET('/api/v1/fleet/mowers'));
    ids = list.items.map((mower) => mower.id);
  } catch {
    ids = [];
  }
  const mowerId = ids[0] ?? null;
  let capabilities: TourCapabilities | null = null;
  if (mowerId) {
    try {
      const found = await unwrap(
        telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/capabilities', {
          params: { path: { mower_id: mowerId } },
        }),
      );
      capabilities = found.capabilities as TourCapabilities;
    } catch {
      capabilities = null;
    }
  }
  const modules = new Set(
    visibleModules(
      builtIn,
      session.me?.permissions ?? [],
      registry.areas,
      operatorAccess.allowed,
    ).map((module) => module.id),
  );
  return {
    mowers: ids.length,
    mowerId,
    capabilities,
    canManage: session.has('fleet.manage'),
    modules,
    demo: demo.active,
  };
}

const demoStore: TourStore = {
  load: async () => {
    const value = read('local', DEMO_OUTCOME_KEY);
    return value === 'seen' || value === 'done' || value === 'skipped' ? value : 'new';
  },
  save: async (outcome) => write('local', DEMO_OUTCOME_KEY, outcome),
};

const userStore: TourStore = {
  load: async () => {
    await onboarding.load();
    return onboarding.failed ? null : outcomeOf(onboarding.progress.hints);
  },
  save: async (outcome, context) => {
    let hints = keysWith(onboarding.progress.hints, outcome);
    if (outcome === 'done') hints = [...new Set([...hints, ...COVERED_HINTS])];
    // A new account that may add mowers and has none starts the setup tour as well (what the
    // mower list does on its first visit); any other account just keeps its status.
    const whenNew = context.canManage && context.mowers === 0 ? 'active' : 'dismissed';
    await onboarding.setHints(hints, whenNew);
  },
};

/** Demo or user store, decided when it is used (the demo config loads with the app). */
const store: TourStore = {
  load: () => (demo.active ? demoStore : userStore).load(),
  save: (outcome, context) => (demo.active ? demoStore : userStore).save(outcome, context),
};

const resume: TourResume = {
  read: () => {
    const raw = read('session', RESUME_KEY);
    if (!raw) return null;
    try {
      const value = JSON.parse(raw) as { user?: string; step?: string };
      const user = session.me?.subject;
      return value.user === user && TOUR_STEP_IDS.includes(value.step as TourStepId)
        ? (value.step as TourStepId)
        : null;
    } catch {
      return null;
    }
  },
  write: (step) =>
    write(
      'session',
      RESUME_KEY,
      step === null ? null : JSON.stringify({ user: session.me?.subject ?? '', step }),
    ),
};

function visible(element: Element): element is HTMLElement {
  return element instanceof HTMLElement && element.getClientRects().length > 0;
}

/** The first visible `[data-tour="<target>"]`, polled until `timeoutMs`. */
export function findTarget(target: string, timeoutMs: number): Promise<HTMLElement | null> {
  const selector = `[data-tour="${CSS.escape(target)}"]`;
  const lookup = () => [...document.querySelectorAll(selector)].find(visible) ?? null;
  const found = lookup();
  if (found) return Promise.resolve(found);
  return new Promise((resolve) => {
    const started = Date.now();
    const timer = setInterval(() => {
      const element = lookup();
      if (element || Date.now() - started >= timeoutMs) {
        clearInterval(timer);
        resolve(element);
      }
    }, 100);
  });
}

export const tour = new Tour({
  store,
  context: readContext,
  currentPath: () => location.pathname,
  navigate: (path) => goto(path),
  findTarget,
  resume,
  optedOut: () => read('local', AUTOSTART_KEY) === 'off',
  setupStatus: async () => {
    await onboarding.load();
    return onboarding.failed ? null : onboarding.status;
  },
});

export type { TourOutcome };
