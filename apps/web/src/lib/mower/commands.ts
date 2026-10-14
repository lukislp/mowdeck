/**
 * Which command buttons are enabled for an activity (design 4.2). Pure, so it is unit tested
 * without a page. The server and the mower still decide; this only avoids offering the obvious
 * refusal. The emergency stop is not here: it is always enabled.
 */
import type { Activity } from '@mowdeck/ui';

export type BarCommand = 'start' | 'pause' | 'resume' | 'dock';

export interface Availability {
  enabled: boolean;
  /** Why it is disabled: a key of the reason messages. */
  reason: 'start' | 'pause' | 'resume' | 'dock' | 'offline' | null;
}

const AT_REST: ReadonlySet<Activity> = new Set(['docked', 'charging', 'idle']);
const DRIVING: ReadonlySet<Activity> = new Set(['mowing', 'returning']);
const AWAY: ReadonlySet<Activity> = new Set(['mowing', 'paused', 'idle', 'error']);

export function availability(command: BarCommand, activity: Activity | null): Availability {
  if (activity === null || activity === 'offline' || activity === 'unknown') {
    return { enabled: false, reason: 'offline' };
  }
  const ok = (enabled: boolean, reason: Availability['reason']): Availability =>
    enabled ? { enabled: true, reason: null } : { enabled: false, reason };
  switch (command) {
    case 'start':
      return ok(AT_REST.has(activity), 'start');
    case 'pause':
      return ok(DRIVING.has(activity), 'pause');
    case 'resume':
      return ok(activity === 'paused', 'resume');
    case 'dock':
      return ok(AWAY.has(activity), 'dock');
  }
}

/** The button that is the main action for the activity (filled style). */
export function primaryCommand(activity: Activity | null): BarCommand | null {
  switch (activity) {
    case 'docked':
    case 'charging':
    case 'idle':
      return 'start';
    case 'mowing':
      return 'pause';
    case 'paused':
      return 'resume';
    case 'error':
      return 'dock';
    default:
      return null;
  }
}

/** Area of a polygon in square metres (shoelace), for the area list. */
export function polygonArea(points: ReadonlyArray<ReadonlyArray<number>>): number {
  let sum = 0;
  for (let i = 0; i < points.length; i += 1) {
    const a = points[i] ?? [0, 0];
    const b = points[(i + 1) % points.length] ?? [0, 0];
    sum += (a[0] ?? 0) * (b[1] ?? 0) - (b[0] ?? 0) * (a[1] ?? 0);
  }
  return Math.abs(sum) / 2;
}
