import { describe, expect, it } from 'vitest';

import { appendLive, isRouteWindow, windowStart } from './route';

const now = new Date(2026, 9, 9, 14, 30, 0);

describe('windowStart', () => {
  it('computes the three windows', () => {
    expect(windowStart('1h', now).getTime()).toBe(now.getTime() - 3_600_000);
    expect(windowStart('24h', now).getTime()).toBe(now.getTime() - 86_400_000);
    expect(windowStart('today', now)).toEqual(new Date(2026, 9, 9, 0, 0, 0, 0));
  });

  it('recognises window values', () => {
    expect(isRouteWindow('today')).toBe(true);
    expect(isRouteWindow('week')).toBe(false);
    expect(isRouteWindow(null)).toBe(false);
  });
});

describe('appendLive', () => {
  const since = new Date('2026-10-09T10:00:00Z');
  const point = (s: number, lat = 48) => ({
    lat,
    lon: 11,
    observed_at: new Date(Date.UTC(2026, 9, 9, 11, 0, s)).toISOString(),
  });

  it('appends a newer position with its RTK fix', () => {
    const route = [point(0)];
    const state = { ...point(5, 48.1), rtk_fix: 'fixed', position: { lat: 48.1, lon: 11 } };
    const next = appendLive(route, state, since);
    expect(next).toHaveLength(2);
    expect(next[1]).toEqual({
      lat: 48.1,
      lon: 11,
      observed_at: state.observed_at,
      rtk_fix: 'fixed',
    });
  });

  it('ignores missing positions, older, duplicate and out-of-window states', () => {
    const route = [point(10)];
    expect(appendLive(route, null, since)).toBe(route);
    expect(appendLive(route, { observed_at: point(20).observed_at }, since)).toBe(route);
    const older = { observed_at: point(5).observed_at, position: { lat: 1, lon: 1 } };
    expect(appendLive(route, older, since)).toBe(route);
    const same = { observed_at: point(20).observed_at, position: { lat: 48, lon: 11 } };
    expect(appendLive(route, same, since)).toBe(route);
    const early = { observed_at: '2026-10-09T09:00:00Z', position: { lat: 1, lon: 1 } };
    expect(appendLive([], early, since)).toEqual([]);
  });

  it('keeps at most the limit', () => {
    const route = [point(0), point(1, 48.01), point(2, 48.02)];
    const state = { observed_at: point(3).observed_at, position: { lat: 48.03, lon: 11 } };
    const next = appendLive(route, state, since, 3);
    expect(next).toHaveLength(3);
    expect(next[2]?.lat).toBe(48.03);
    expect(next[0]?.lat).toBe(48.01);
  });
});
