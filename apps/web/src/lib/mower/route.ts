/**
 * The driven route of the detail page: the time window the user picked, the initial track from
 * `GET /telemetry/mowers/{id}/positions` and the live points appended from the socket. Pure, so
 * it is unit tested without a page.
 */
import type { RoutePoint } from '@mowdeck/ui';

export type RouteWindow = '1h' | 'today' | '24h';

export const ROUTE_WINDOWS: readonly RouteWindow[] = ['1h', 'today', '24h'];

/** Most points requested from Telemetry and kept in memory. */
export const ROUTE_LIMIT = 5000;

/** Start of the window at `now` (`today` is local midnight). */
export function windowStart(window: RouteWindow, now: Date): Date {
  switch (window) {
    case '1h':
      return new Date(now.getTime() - 3_600_000);
    case '24h':
      return new Date(now.getTime() - 86_400_000);
    case 'today': {
      const start = new Date(now);
      start.setHours(0, 0, 0, 0);
      return start;
    }
  }
}

export function isRouteWindow(value: unknown): value is RouteWindow {
  return typeof value === 'string' && (ROUTE_WINDOWS as readonly string[]).includes(value);
}

/** The state fields a live route point is made of. */
export interface LiveState {
  observed_at: string;
  rtk_fix?: string;
  position?: { lat: number; lon: number } | null;
}

/**
 * Append the position of a live state when it is newer than the last point and lies inside the
 * window; keeps at most `limit` points. Returns the same array when nothing changes.
 */
export function appendLive(
  route: RoutePoint[],
  state: LiveState | null | undefined,
  since: Date,
  limit = ROUTE_LIMIT,
): RoutePoint[] {
  const position = state?.position;
  if (!state || !position) return route;
  const t = Date.parse(state.observed_at);
  if (!Number.isFinite(t) || t < since.getTime()) return route;
  const last = route[route.length - 1];
  if (last && Date.parse(last.observed_at) >= t) return route;
  if (last && last.lat === position.lat && last.lon === position.lon) return route;
  const next = [
    ...route,
    {
      lat: position.lat,
      lon: position.lon,
      observed_at: state.observed_at,
      rtk_fix: state.rtk_fix,
    },
  ];
  return next.length > limit ? next.slice(next.length - limit) : next;
}
