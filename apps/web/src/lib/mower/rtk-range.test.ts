import { describe, expect, it } from 'vitest';

import { isRtkRange, loadRange, rangeStart, type PositionPage } from './rtk-range';

const point = (minute: number) => ({
  lat: 48,
  lon: 11,
  observed_at: new Date(Date.UTC(2026, 9, 10, 8, minute)).toISOString(),
  rtk_fix: 'fixed',
});

/** A fake Telemetry over `count` points one minute apart (newest `limit` in `[from, to)`). */
function telemetry(count: number): { fetch: PositionPage; calls: string[] } {
  const all = Array.from({ length: count }, (_, i) => point(i));
  const calls: string[] = [];
  return {
    calls,
    fetch: async ({ from, to, limit }) => {
      calls.push(to);
      const inRange = all.filter((p) => p.observed_at >= from && p.observed_at < to);
      return { positions: inRange.slice(-limit), truncated: inRange.length > limit };
    },
  };
}

describe('rangeStart', () => {
  const now = new Date(2026, 9, 10, 15, 30);

  it('starts today at local midnight and the others 7 and 30 days back', () => {
    expect(rangeStart('today', now)).toEqual(new Date(2026, 9, 10, 0, 0));
    expect(rangeStart('7d', now).getTime()).toBe(now.getTime() - 7 * 86_400_000);
    expect(rangeStart('30d', now).getTime()).toBe(now.getTime() - 30 * 86_400_000);
  });

  it('accepts only known ranges', () => {
    expect(isRtkRange('7d')).toBe(true);
    expect(isRtkRange('1h')).toBe(false);
    expect(isRtkRange(null)).toBe(false);
  });
});

describe('loadRange', () => {
  const from = new Date(Date.UTC(2026, 9, 10, 0));
  const to = new Date(Date.UTC(2026, 9, 11, 0));

  it('stops after one page when nothing was cut', async () => {
    const fake = telemetry(30);
    const result = await loadRange(fake.fetch, from, to, { limit: 50 });
    expect(result).toMatchObject({ truncated: false, pages: 1 });
    expect(result.points).toHaveLength(30);
  });

  it('pages back with `to` at the oldest point and keeps the order', async () => {
    const fake = telemetry(25);
    const result = await loadRange(fake.fetch, from, to, { limit: 10 });
    expect(result.pages).toBe(3);
    expect(result.truncated).toBe(false);
    expect(result.points.map((p) => p.observed_at)).toEqual(
      Array.from({ length: 25 }, (_, i) => point(i).observed_at),
    );
    expect(fake.calls[1]).toBe(point(15).observed_at);
  });

  it('reports truncation when the page cap is reached', async () => {
    const fake = telemetry(100);
    const result = await loadRange(fake.fetch, from, to, { limit: 10, maxPages: 2 });
    expect(result).toMatchObject({ truncated: true, pages: 2 });
    expect(result.points[0]?.observed_at).toBe(point(80).observed_at);
  });
});
