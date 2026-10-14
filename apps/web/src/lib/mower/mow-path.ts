/**
 * The mower's planned mow path on the detail page: which path the map shows (a preview from the
 * start sheet, the running job's path from Telemetry, or the last job's path while the job
 * still exists on the mower), the remembered "Mähpfad" toggle, the preview's key and status, and how often the
 * running job's path is polled. Pure apart from the injected storage, so it is unit tested
 * (`mow-path.test.ts`); the page does the requests.
 */
import type { MapMowPath } from '@mowdeck/ui';

/** The mow path (`MowPath` of the contracts) as Fleet's preview and Telemetry's path return it. */
export interface MowPath {
  path_hash: string;
  polylines?: number[][][];
  kinds?: string[] | null;
  points_total: number;
  progress?: { index: number; direction?: number } | null;
  area_ids?: string[];
  planned_at: string;
}

/** How often the running job's path is read again (progress moves with every report). */
export const PATH_POLL_MS = 10_000;

/** Activities of a running job: its path is polled and shown with progress. */
const JOB_ACTIVITIES: ReadonlySet<string> = new Set(['mowing', 'paused', 'returning']);

export function isJobActivity(activity: string | null | undefined): boolean {
  return !!activity && JOB_ACTIVITIES.has(activity);
}

/** Activities of a mower at rest: no job is running or waiting to be resumed. */
const RESTING_ACTIVITIES: ReadonlySet<string> = new Set(['idle', 'docked', 'charging']);

export function isRestingActivity(activity: string | null | undefined): boolean {
  return !!activity && RESTING_ACTIVITIES.has(activity);
}

/** The path the page holds: the polled one of the running job and the last one kept. */
export interface HeldPaths {
  live: MowPath | null;
  last: MowPath | null;
}

/**
 * What a poll of Telemetry's path does to the held paths. `polled` is the gateway's path, null
 * when it published `path: null` or answered 404 `no-path`. A path comes in unless it is the one
 * the owner ended (`endedHash`: a plain "Nur zur Station" keeps the job and its path, but
 * "Zur Station, Aufgabe abbrechen" ends it, and the gateway may still answer the old path for a
 * moment). No path clears the last one only when the mower is docked or idle: while it is
 * paused or returning the job (and its path) may still exist, the answer is just not there yet.
 */
export function reconcilePath(
  held: HeldPaths,
  polled: MowPath | null,
  activity: string | null | undefined,
  endedHash: string | null = null,
): HeldPaths {
  if (polled && polled.path_hash !== endedHash) return { live: polled, last: polled };
  return { live: null, last: isRestingActivity(activity) || polled ? null : held.last };
}

/**
 * True for commands that drop the shown path at once: ending the job on the way to the dock
 * (`return_to_dock` with `end_job`), and a new start (the new job brings its own path).
 */
export function commandClearsPath(command: { kind: string; end_job?: boolean }): boolean {
  return (
    (command.kind === 'return_to_dock' && command.end_job === true) || command.kind === 'start_mow'
  );
}

const STORAGE_KEY = 'mowdeck.mowPath';

/** The "Mähpfad" toggle: on unless the viewer switched it off (remembered per browser). */
export function loadPathShown(storage: Pick<Storage, 'getItem'> | null = safeStorage()): boolean {
  try {
    return storage?.getItem(STORAGE_KEY) !== 'false';
  } catch {
    return true;
  }
}

export function savePathShown(
  shown: boolean,
  storage: Pick<Storage, 'setItem'> | null = safeStorage(),
): void {
  try {
    storage?.setItem(STORAGE_KEY, String(shown));
  } catch {
    // A per-viewer convenience only.
  }
}

function safeStorage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

/** What a preview was computed for: the areas in order and the pattern sent with the start. */
export function previewKey(areaIds: readonly string[], pattern: object | null | undefined): string {
  const sorted = pattern
    ? Object.fromEntries(Object.entries(pattern).sort(([a], [b]) => a.localeCompare(b)))
    : null;
  return JSON.stringify([areaIds, sorted]);
}

/** The state of the preview in the start sheet. */
export type PreviewState =
  | { kind: 'idle' }
  | { kind: 'loading'; key: string }
  | { kind: 'ready'; key: string; path: MowPath }
  | { kind: 'error'; key: string; reason: string };

/** The preview to draw: only one computed for the sheet's current areas and pattern. */
export function currentPreview(state: PreviewState, key: string): MowPath | null {
  return state.kind === 'ready' && state.key === key ? state.path : null;
}

/** True when a preview exists (or failed) for other areas or another pattern than chosen now. */
export function previewOutdated(state: PreviewState, key: string): boolean {
  return state.kind !== 'idle' && state.kind !== 'loading' && state.key !== key;
}

/** Which path the map shows and whether it is a preview. */
export interface ShownPath {
  path: MapMowPath | null;
  preview: boolean;
}

/**
 * The path for the map: the preview of the open start sheet (asked for explicitly, so also
 * while the toggle is off), else, unless the toggle is off, the running job's path, else the
 * last job's path (kept while the job exists on the mower).
 */
export function shownPath(options: {
  shown: boolean;
  preview: MowPath | null;
  live: MowPath | null;
  last: MowPath | null;
}): ShownPath {
  if (options.preview) return { path: options.preview, preview: true };
  if (!options.shown) return { path: null, preview: false };
  return { path: options.live ?? options.last, preview: false };
}

/** Done share in whole percent (null without progress). */
export function donePercent(path: MowPath | null | undefined): number | null {
  if (!path?.progress || path.points_total <= 0) return null;
  return Math.round((Math.min(path.progress.index, path.points_total) / path.points_total) * 100);
}
