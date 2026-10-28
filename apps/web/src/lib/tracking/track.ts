/**
 * Pure helpers of the theft tracking views: merging polled track points, the bounds to fit, the
 * newest point's time (the `after` of the next poll) and the offset between the server's clock and
 * this device's, so "last reported 12 s ago" does not depend on a correct phone clock.
 */

export interface TrackPoint {
  lat: number;
  lon: number;
  /** ISO 8601 time of the position. */
  at: string;
}

export interface Bounds {
  south: number;
  west: number;
  north: number;
  east: number;
}

/**
 * The track extended by the polled points, oldest first. Points with the same time are one point
 * (the later one wins); points without a usable time or position are dropped.
 */
export function mergeTrack(
  existing: readonly TrackPoint[],
  incoming: readonly TrackPoint[],
): TrackPoint[] {
  const byTime = new Map<number, TrackPoint>();
  for (const point of [...existing, ...incoming]) {
    const time = Date.parse(point.at);
    if (!Number.isFinite(time) || !Number.isFinite(point.lat) || !Number.isFinite(point.lon)) {
      continue;
    }
    byTime.set(time, point);
  }
  return [...byTime.entries()].sort(([a], [b]) => a - b).map(([, point]) => point);
}

/** The time of the newest point (the `after` of the next poll), or null for an empty track. */
export function newestAt(track: readonly TrackPoint[]): string | null {
  let best: { time: number; at: string } | null = null;
  for (const point of track) {
    const time = Date.parse(point.at);
    if (Number.isFinite(time) && (!best || time > best.time)) best = { time, at: point.at };
  }
  return best?.at ?? null;
}

/** The box around the positions, or null without any. */
export function trackBounds(points: readonly { lat: number; lon: number }[]): Bounds | null {
  if (!points.length) return null;
  let south = Infinity;
  let north = -Infinity;
  let west = Infinity;
  let east = -Infinity;
  for (const { lat, lon } of points) {
    south = Math.min(south, lat);
    north = Math.max(north, lat);
    west = Math.min(west, lon);
    east = Math.max(east, lon);
  }
  return { south, west, north, east };
}

/** Milliseconds to add to this device's clock to get the server's. 0 for an unreadable time. */
export function serverOffsetMs(serverTime: string, deviceNow: number): number {
  const server = Date.parse(serverTime);
  return Number.isFinite(server) ? server - deviceNow : 0;
}

/** Seconds from `at` to `now` (server-corrected by `offset`), never negative; null if unreadable. */
export function ageSeconds(at: string | null | undefined, now: number, offset = 0): number | null {
  if (!at) return null;
  const time = Date.parse(at);
  return Number.isFinite(time) ? Math.max(0, Math.round((now + offset - time) / 1000)) : null;
}
