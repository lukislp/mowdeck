/**
 * What the Matter side knows of a mower, independent of the Matter device type: a normalized
 * state snapshot and the intents a controller can express. The device profiles (`profiles/`)
 * translate both to and from their clusters, so a future Matter mower device type replaces one
 * profile and nothing else (ADR 0063).
 */

/** `MowerActivity` of MowDeck's contracts (`offline` while the state is stale). */
export type Activity =
  | 'idle'
  | 'mowing'
  | 'paused'
  | 'returning'
  | 'docked'
  | 'charging'
  | 'error'
  | 'offline'
  | 'updating'
  | 'unknown';

const ACTIVITIES: ReadonlySet<string> = new Set<Activity>([
  'idle',
  'mowing',
  'paused',
  'returning',
  'docked',
  'charging',
  'error',
  'offline',
  'updating',
  'unknown',
]);

/** The state a profile shows. */
export interface MowerSnapshot {
  activity: Activity;
  /** 0..100, or null before the first state. */
  batteryPercent: number | null;
  charging: boolean;
}

/** What a controller asks for. There is deliberately no emergency stop: it stays in MowDeck. */
export type Intent = 'start' | 'stop' | 'pause' | 'resume' | 'dock';

/** How an intent is carried out. `start` resolves areas and the mowing defaults later. */
export type Plan =
  | { kind: 'start' }
  | { kind: 'command'; command: Record<string, unknown> }
  | { kind: 'noop' }
  | { kind: 'refuse'; reason: RefuseReason };

export type RefuseReason = 'offline' | 'updating' | 'error' | 'invalid_in_state';

/** The snapshot before any telemetry arrived. */
export const UNKNOWN_SNAPSHOT: MowerSnapshot = {
  activity: 'unknown',
  batteryPercent: null,
  charging: false,
};

/** A snapshot from Telemetry's compact state (`GET /telemetry/mowers/{id}/widget`). */
export function snapshotFromWidget(body: unknown): MowerSnapshot {
  if (typeof body !== 'object' || body === null) return UNKNOWN_SNAPSHOT;
  const value = body as Record<string, unknown>;
  const raw = typeof value.activity === 'string' ? value.activity : 'unknown';
  const activity: Activity =
    value.stale === true ? 'offline' : ACTIVITIES.has(raw) ? (raw as Activity) : 'unknown';
  const battery = value.battery_percent;
  const batteryPercent =
    typeof battery === 'number' && Number.isFinite(battery)
      ? Math.min(100, Math.max(0, Math.round(battery)))
      : null;
  return { activity, batteryPercent, charging: value.charging === true };
}

/** True while a job runs or waits (mowing, paused, on the way home within a job). */
export function isWorking(activity: Activity): boolean {
  return activity === 'mowing' || activity === 'paused';
}

/**
 * The MowDeck command for `intent` in the state `snapshot`, the same commands the Home Assistant
 * integration and the iOS widgets send:
 *
 * - start: resume a paused job, otherwise start every area with the mower's mowing defaults
 *   (resolved by the caller); a mowing mower is left alone.
 * - pause: `pause_mow` while mowing.
 * - resume: `resume_mow` while paused.
 * - dock: `return_to_dock` (a job is paused and can be resumed); nothing when already home.
 * - stop: end the job and return to the dock (`return_to_dock` with `end_job`).
 *
 * An offline, updating or failed mower refuses everything: the controller shows the error and
 * MowDeck's own app stays the place to recover (and to stop in an emergency).
 */
export function planIntent(intent: Intent, snapshot: MowerSnapshot): Plan {
  const { activity } = snapshot;
  if (activity === 'offline' || activity === 'unknown') return refuse('offline');
  if (activity === 'updating') return refuse('updating');
  switch (intent) {
    case 'start':
      if (activity === 'error') return refuse('error');
      if (activity === 'paused') return command({ kind: 'resume_mow' });
      if (activity === 'mowing') return { kind: 'noop' };
      return { kind: 'start' };
    case 'pause':
      if (activity === 'paused') return { kind: 'noop' };
      return activity === 'mowing'
        ? command({ kind: 'pause_mow' })
        : refuse(activity === 'error' ? 'error' : 'invalid_in_state');
    case 'resume':
      if (activity === 'mowing') return { kind: 'noop' };
      return activity === 'paused'
        ? command({ kind: 'resume_mow' })
        : refuse(activity === 'error' ? 'error' : 'invalid_in_state');
    case 'dock':
      if (activity === 'docked' || activity === 'charging' || activity === 'returning') {
        return { kind: 'noop' };
      }
      return command({ kind: 'return_to_dock' });
    case 'stop':
      if (activity === 'docked' || activity === 'charging' || activity === 'idle') {
        return { kind: 'noop' };
      }
      return command({ kind: 'return_to_dock', end_job: true });
  }
}

/** The `start_mow` command for `areaIds` with the saved mowing pattern, when there is one. */
export function startCommand(
  areaIds: readonly string[],
  pattern: Record<string, unknown> | null,
): Record<string, unknown> {
  const body: Record<string, unknown> = { kind: 'start_mow', area_ids: [...areaIds] };
  if (pattern) body.pattern = pattern;
  return body;
}

function command(body: Record<string, unknown>): Plan {
  return { kind: 'command', command: body };
}

function refuse(reason: RefuseReason): Plan {
  return { kind: 'refuse', reason };
}
