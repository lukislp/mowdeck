import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { nextDelay, startPoller } from './poll.js';
import { parseRetryAfter, viewPublic } from './public.js';
import { shareUrl, tokenFromHash } from './share.js';
import { ageSeconds, mergeTrack, newestAt, serverOffsetMs, trackBounds } from './track.js';

const pt = (at: string, lat = 48, lon = 11) => ({ lat, lon, at });

describe('track', () => {
  it('merges by time, sorted, newer duplicate wins, junk dropped', () => {
    const merged = mergeTrack(
      [pt('2026-10-27T10:00:10Z', 1, 1), pt('2026-10-27T10:00:00Z', 0, 0)],
      [pt('2026-10-27T10:00:10Z', 2, 2), pt('2026-10-27T10:00:20Z', 3, 3), pt('nope'), pt('', NaN)],
    );
    expect(merged.map((p) => p.lat)).toEqual([0, 2, 3]);
  });

  it('knows the newest time and the bounds', () => {
    expect(newestAt([])).toBeNull();
    expect(newestAt([pt('2026-10-27T10:00:10Z'), pt('2026-10-27T10:00:00Z')])).toBe(
      '2026-10-27T10:00:10Z',
    );
    expect(trackBounds([])).toBeNull();
    expect(
      trackBounds([
        { lat: 1, lon: 5 },
        { lat: -2, lon: 9 },
      ]),
    ).toEqual({ south: -2, west: 5, north: 1, east: 9 });
  });

  it('corrects ages by the server offset and never goes negative', () => {
    const device = Date.parse('2026-10-27T10:00:00Z');
    const offset = serverOffsetMs('2026-10-27T10:05:00Z', device);
    expect(offset).toBe(300_000);
    expect(ageSeconds('2026-10-27T10:04:48Z', device, offset)).toBe(12);
    expect(ageSeconds('2026-10-27T11:00:00Z', device)).toBe(0);
    expect(ageSeconds(null, device)).toBeNull();
    expect(serverOffsetMs('garbage', device)).toBe(0);
  });
});

describe('share', () => {
  const token = 'abcdefghijklmnopqrstuvwxyz_-0123456789';
  it('builds the address with the token in the fragment', () => {
    expect(shareUrl('https://deck.example/', token)).toBe(`https://deck.example/track#${token}`);
  });

  it('reads only a well-formed token from the hash', () => {
    expect(tokenFromHash(`#${token}`)).toBe(token);
    expect(tokenFromHash(token)).toBe(token);
    expect(tokenFromHash('')).toBeNull();
    expect(tokenFromHash('#short')).toBeNull();
    expect(tokenFromHash('#<script>alert(1)</script>aaaaaaaa')).toBeNull();
  });
});

describe('poll', () => {
  it('backs off on failures and honours Retry-After', () => {
    expect(nextDelay(0)).toBe(8_000);
    expect(nextDelay(1)).toBe(16_000);
    expect(nextDelay(9)).toBe(60_000);
    expect(nextDelay(0, 120)).toBe(120_000);
    expect(nextDelay(1, 1)).toBe(16_000);
    expect(nextDelay(0, 99_999)).toBe(300_000);
    expect(nextDelay(0, -5)).toBe(8_000);
  });

  describe('poller', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it('repeats, backs off after a failure, stops on request', async () => {
      const task = vi
        .fn<() => Promise<{ failed?: boolean; stop?: boolean } | void>>()
        .mockResolvedValueOnce(undefined)
        .mockResolvedValueOnce({ failed: true })
        .mockResolvedValueOnce({ stop: true });
      startPoller(task, 1_000);
      await vi.advanceTimersByTimeAsync(0);
      expect(task).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(1_000);
      expect(task).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(1_999);
      expect(task).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(1);
      expect(task).toHaveBeenCalledTimes(3);
      await vi.advanceTimersByTimeAsync(600_000);
      expect(task).toHaveBeenCalledTimes(3);
    });

    it('the stop function ends it', async () => {
      const task = vi.fn().mockResolvedValue(undefined);
      const stop = startPoller(task, 1_000);
      await vi.advanceTimersByTimeAsync(0);
      stop();
      await vi.advanceTimersByTimeAsync(10_000);
      expect(task).toHaveBeenCalledTimes(1);
    });
  });
});

describe('public view', () => {
  const call = (response: Response | Error) =>
    viewPublic(
      'tok',
      vi.fn(() =>
        response instanceof Error ? Promise.reject(response) : Promise.resolve(response),
      ),
    );

  it('sends no credentials and maps the statuses', async () => {
    const fetchFn = vi
      .fn()
      .mockResolvedValue(new Response('{"mower_name":"Luba"}', { status: 200 }));
    const result = await viewPublic('tok', fetchFn);
    expect(result.kind).toBe('ok');
    const init = fetchFn.mock.calls[0]![1] as RequestInit;
    expect(init.credentials).toBe('omit');
    expect(init.referrerPolicy).toBe('no-referrer');
    expect(new Headers(init.headers).has('Authorization')).toBe(false);

    expect(await call(new Response('{}', { status: 404 }))).toEqual({ kind: 'invalid' });
    expect(
      await call(new Response('{}', { status: 429, headers: { 'Retry-After': '30' } })),
    ).toEqual({ kind: 'limited', retryAfter: 30 });
    expect(await call(new Response('{}', { status: 500 }))).toEqual({ kind: 'error' });
    expect(await call(new Error('offline'))).toEqual({ kind: 'error' });
    expect(parseRetryAfter('abc')).toBeNull();
  });
});
