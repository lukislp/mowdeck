/**
 * The guided tour (ADR 0073) as plain data: its steps, which of them apply to this user, mower
 * and installation, how the position moves over steps whose target is missing, whether the tour
 * starts by itself, and how its outcome is kept. No DOM, no fetch and no runes here;
 * `tour.svelte.ts` drives it and `TourOverlay.svelte` draws it.
 */

/** Every step of the tour, in order. A step that does not apply is left out. */
export const TOUR_STEP_IDS = [
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
  'add-mower',
] as const;
export type TourStepId = (typeof TOUR_STEP_IDS)[number];

/** The capabilities a step looks at (`GET /telemetry/mowers/{id}/capabilities`, ADR 0012). */
export interface TourCapabilities {
  manual_drive?: boolean;
}

/** What the tour knows about the user, the first mower and the installation. */
export interface TourContext {
  /** Mowers the user sees; null when the list could not be read (never taken for "none"). */
  mowers: number | null;
  /** The mower the mower page steps show (the first of the list). */
  mowerId: string | null;
  /** Its capabilities; null when unknown (steps that need one are left out). */
  capabilities: TourCapabilities | null;
  /** `fleet.manage`: may add mowers. */
  canManage: boolean;
  /** The visible modules of the shell (`schedules`, `notifications`, ...). */
  modules: ReadonlySet<string>;
  /** The public demo (ADR 0068). */
  demo: boolean;
  /** The demo gives every visitor an own sandbox (ADR 0076): the notes do not claim sharing. */
  sandbox?: boolean;
}

export interface TourStepDef {
  id: TourStepId;
  /** The page the step is shown on; null: wherever the user is. */
  route(context: TourContext): string | null;
  /** `data-tour` value of the element the step points at; null: a centred card. */
  target: string | null;
  applies(context: TourContext): boolean;
}

/** A step as the running tour holds it. */
export interface TourStep {
  id: TourStepId;
  route: string | null;
  target: string | null;
}

const hasMower = (c: TourContext): boolean => (c.mowers ?? 0) > 0 && c.mowerId !== null;
const mowerPage = (c: TourContext): string | null =>
  c.mowerId ? `/mowers/${encodeURIComponent(c.mowerId)}` : null;

export const TOUR_STEPS: readonly TourStepDef[] = [
  { id: 'welcome', route: () => null, target: null, applies: () => true },
  { id: 'overview', route: () => '/mowers', target: 'overview', applies: hasMower },
  { id: 'map', route: mowerPage, target: 'map', applies: hasMower },
  { id: 'commands', route: mowerPage, target: 'commands', applies: hasMower },
  {
    id: 'spot',
    route: mowerPage,
    target: 'spot',
    // Spot mowing drives the mower over its drive link (ADR 0039): only with manual driving.
    applies: (c) => hasMower(c) && c.capabilities?.manual_drive === true,
  },
  // Vendor areas or virtual zones (ADR 0070): the page shows whichever the mower has; a mower
  // with neither has no target, and the step is passed over at run time.
  { id: 'zones', route: mowerPage, target: 'zones', applies: hasMower },
  {
    id: 'schedules',
    route: () => '/schedules',
    target: 'schedules',
    applies: (c) => hasMower(c) && c.modules.has('schedules'),
  },
  {
    id: 'inbox',
    route: () => '/notifications',
    target: 'inbox',
    applies: (c) => hasMower(c) && c.modules.has('notifications'),
  },
  { id: 'settings', route: () => '/settings', target: 'settings', applies: hasMower },
  { id: 'restart', route: () => '/settings#setup', target: 'tour-restart', applies: () => true },
  // Without a mower the tour ends where the first one is added (or, for a viewer, where it
  // will appear).
  { id: 'add-mower', route: () => '/mowers', target: 'add-mower', applies: (c) => c.mowers === 0 },
];

/** The steps that apply here, in order. */
export function tourSteps(
  context: TourContext,
  defs: readonly TourStepDef[] = TOUR_STEPS,
): TourStep[] {
  return defs
    .filter((def) => def.applies(context))
    .map((def) => ({ id: def.id, route: def.route(context), target: def.target }));
}

/**
 * The next step to show from `from` in `direction`, passing over the steps whose target was
 * missing. Forward past the last step: `end`. Backward before the first shown step: stay.
 */
export function moveFrom(
  steps: readonly { id: string }[],
  missed: ReadonlySet<string>,
  from: number,
  direction: 1 | -1,
): number | 'end' {
  for (let i = from + direction; i >= 0 && i < steps.length; i += direction) {
    if (!missed.has(steps[i]!.id)) return i;
  }
  return direction === 1 ? 'end' : from;
}

/** The first step to show, from `start` forward. */
export function firstFrom(
  steps: readonly { id: string }[],
  missed: ReadonlySet<string>,
  start: number,
): number | 'end' {
  if (start >= steps.length) return 'end';
  return missed.has(steps[start]!.id) ? moveFrom(steps, missed, start, 1) : start;
}

/** Position among the steps still shown: "3 of 7". */
export function progressOf(
  steps: readonly { id: string }[],
  missed: ReadonlySet<string>,
  index: number,
): { current: number; total: number } {
  const shown = steps.filter((step) => !missed.has(step.id));
  const current = shown.findIndex((step) => step.id === steps[index]?.id);
  return { current: current + 1, total: shown.length };
}

/** The user's history with the tour. `seen`: it opened once and was not finished. */
export type TourOutcome = 'new' | 'seen' | 'done' | 'skipped';

/** Keys of the outcome in Identity's per-user hint list (ADR 0062 `dismissed_hints`). */
export const OUTCOME_KEYS = {
  seen: 'tour-seen',
  done: 'tour-done',
  skipped: 'tour-skipped',
} as const;

/** The outcome from the stored keys; the most final one wins. */
export function outcomeOf(keys: readonly string[]): TourOutcome {
  if (keys.includes(OUTCOME_KEYS.done)) return 'done';
  if (keys.includes(OUTCOME_KEYS.skipped)) return 'skipped';
  if (keys.includes(OUTCOME_KEYS.seen)) return 'seen';
  return 'new';
}

/** The keys to store for `outcome` (the others of the tour removed, unknown keys kept). */
export function keysWith(keys: readonly string[], outcome: Exclude<TourOutcome, 'new'>): string[] {
  const ours = new Set<string>(Object.values(OUTCOME_KEYS));
  return [...keys.filter((key) => !ours.has(key)), OUTCOME_KEYS[outcome]];
}

/** Status of the setup tour (ADR 0062) as Identity keeps it. */
export type SetupStatus = 'new' | 'active' | 'dismissed' | 'completed';

export interface AutoStartInput {
  /** The page the user is on: the tour only starts by itself on the mower list (the landing
   * page after the sign-in), never over a deep link. */
  path: string;
  /** The stored outcome; null when it could not be read (never start then). */
  outcome: TourOutcome | null;
  demo: boolean;
  /** This device opted out (`mowdeck.tour.autostart` = `off`: wall displays, the e2e suite). */
  optedOut: boolean;
  /** The setup tour's status (ignored in the demo). */
  setup: SetupStatus | null;
  /** Mowers the user sees; null when unknown. */
  mowers: number | null;
}

/**
 * Whether the tour starts by itself: once, on the mower list, for a new user. A new user is one
 * the setup tour treats as new (ADR 0062: its status `active`, or `new` without a mower); an
 * account set up before the tours existed is not bombarded (Settings â†’ Setup starts it). In the
 * demo every visitor is new: the outcome is kept per browser, not for the shared demo user.
 */
export function shouldAutoStart(input: AutoStartInput): boolean {
  if (input.optedOut || input.outcome !== 'new') return false;
  if (input.path !== '/mowers') return false;
  if (input.demo) return true;
  if (input.setup === 'active') return true;
  return input.setup === 'new' && input.mowers === 0;
}
