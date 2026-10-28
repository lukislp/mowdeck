/**
 * The setup tour (ADR 0062) as plain data: its steps, which of them apply to this user and
 * device, their state from the real facts (a mower exists, a schedule exists, ...) and the
 * user's own choices (skipped steps), and whether the tour, a single hint or nothing is offered.
 * No fetch and no runes here; `onboarding.svelte.ts` feeds it.
 */

/** Every step key the web app knows. Identity stores them as opaque keys. */
export const STEP_KEYS = [
  'cloud-account',
  'mower',
  'map',
  'notifications',
  'passkey',
  'schedule',
  'sign-in-links',
  'app-extras',
] as const;
export type StepKey = (typeof STEP_KEYS)[number];

/** The contextual "first time here" hints. */
export const HINT_KEYS = ['mower-map', 'schedules'] as const;
export type HintKey = (typeof HINT_KEYS)[number];

/** Status of the tour as Identity keeps it (`new`: nothing stored yet). */
export type TourStatus = 'new' | 'active' | 'dismissed' | 'completed';

/** The user's choices. The key lists stay as the server has them, keys of other versions of
 * the web app included, so saving never drops them. */
export interface Progress {
  status: TourStatus;
  skipped: string[];
  hints: string[];
}

export const UNTOUCHED: Progress = { status: 'new', skipped: [], hints: [] };

/**
 * What the app knows about this user and device. A fact that could not be read is `null`
 * (unknown): the step then counts as open, never as done, and never blocks anything.
 */
export interface Facts {
  /** Number of mowers the user sees. */
  mowers: number | null;
  /** The first mower (for the map step's link). */
  firstMowerId: string | null;
  /** A linked vendor cloud account (Mammotion) exists. */
  cloudAccounts: number | null;
  /** Telemetry has a map of at least one mower. */
  map: boolean | null;
  /** A push channel is set up: a browser subscription, an iOS device or an ntfy target. */
  notifications: boolean | null;
  passkeys: number | null;
  schedules: number | null;
  /** Linked GitHub or Apple sign-in. */
  signInLinks: number | null;
  /** Widgets (and with them the Live Activity and the Watch) are on in this iOS app. */
  appExtras: boolean | null;
}

export const NO_FACTS: Facts = {
  mowers: null,
  firstMowerId: null,
  cloudAccounts: null,
  map: null,
  notifications: null,
  passkeys: null,
  schedules: null,
  signInLinks: null,
  appExtras: null,
};

/** What this user may do and what this installation and device offer. */
export interface Context {
  /** `fleet.manage`: may link cloud accounts and add mowers. */
  canManage: boolean;
  /** The installation takes mowers from a linked vendor cloud account (Mammotion). */
  cloud: boolean;
  /** The Scheduler is deployed and visible. */
  schedules: boolean;
  /** The notification settings are visible. */
  notifications: boolean;
  /** Passkeys are configured on the installation (`GET /login-methods`). */
  passkeys: boolean;
  /** GitHub or Apple sign-in is configured on the installation. */
  signInLinks: boolean;
  /** Inside the iOS app with widget support (native-only step). */
  appExtras: boolean;
}

export interface StepDef {
  key: StepKey;
  /** Optional steps never hold up completing the tour. */
  optional: boolean;
  applies(context: Context): boolean;
  done(facts: Facts): boolean;
}

const positive = (value: number | null): boolean => (value ?? 0) > 0;

export const STEPS: readonly StepDef[] = [
  {
    key: 'cloud-account',
    optional: false,
    applies: (c) => c.canManage && c.cloud,
    // A mower of another kind (ESP32 bridge, simulated) needs no cloud account.
    done: (f) => positive(f.cloudAccounts) || positive(f.mowers),
  },
  {
    key: 'mower',
    optional: false,
    applies: (c) => c.canManage,
    done: (f) => positive(f.mowers),
  },
  { key: 'map', optional: false, applies: () => true, done: (f) => f.map === true },
  {
    key: 'notifications',
    optional: false,
    applies: (c) => c.notifications,
    done: (f) => f.notifications === true,
  },
  { key: 'passkey', optional: true, applies: (c) => c.passkeys, done: (f) => positive(f.passkeys) },
  {
    key: 'schedule',
    optional: false,
    applies: (c) => c.schedules,
    done: (f) => positive(f.schedules),
  },
  {
    key: 'sign-in-links',
    optional: true,
    applies: (c) => c.signInLinks,
    done: (f) => positive(f.signInLinks),
  },
  {
    key: 'app-extras',
    optional: true,
    applies: (c) => c.appExtras,
    done: (f) => f.appExtras === true,
  },
];

export type StepState = 'done' | 'skipped' | 'open';

export interface StepView {
  key: StepKey;
  optional: boolean;
  state: StepState;
}

/** The steps that apply here, in order, each with its state. Done wins over skipped. */
export function stepViews(facts: Facts, context: Context, skipped: readonly string[]): StepView[] {
  return STEPS.filter((step) => step.applies(context)).map((step) => ({
    key: step.key,
    optional: step.optional,
    state: step.done(facts) ? 'done' : skipped.includes(step.key) ? 'skipped' : 'open',
  }));
}

/** The first open step, or null when nothing is open. */
export function nextOpen(steps: readonly StepView[]): StepView | null {
  return steps.find((step) => step.state === 'open') ?? null;
}

/** Done and skipped count as settled. */
export function settledCount(steps: readonly StepView[]): number {
  return steps.filter((step) => step.state !== 'open').length;
}

/** Every required step is done or skipped (optional ones may stay open). */
export function isComplete(steps: readonly StepView[]): boolean {
  return steps.every((step) => step.optional || step.state !== 'open');
}

/**
 * What the app offers on the mower list:
 * - `tour`: the setup card (the tour runs, or a new account without a mower that may add one),
 * - `hint`: one slim, dismissible line (an account that was set up before the tour existed),
 * - `none`: the tour was dismissed or completed, or there is nothing this user can set up.
 * Unknown facts offer nothing rather than guess.
 */
export function offer(
  status: TourStatus,
  facts: Pick<Facts, 'mowers'>,
  context: Pick<Context, 'canManage'>,
): 'tour' | 'hint' | 'none' {
  if (status === 'active') return 'tour';
  if (status !== 'new' || facts.mowers === null) return 'none';
  if (facts.mowers > 0) return 'hint';
  return context.canManage ? 'tour' : 'none';
}

/** "First time here" hints show to users who started the tour (not to accounts set up before
 * it, nor after "dismiss"), until each is dismissed. */
export function hintVisible(progress: Progress, key: HintKey): boolean {
  return (
    (progress.status === 'active' || progress.status === 'completed') &&
    !progress.hints.includes(key)
  );
}
