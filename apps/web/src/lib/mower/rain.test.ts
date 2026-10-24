import { describe, expect, it } from 'vitest';
import type { HourBar } from '#lib/schedules/preview.js';
import { currentBar, rainNow, rainWarning, readoutBars, stripBars } from './rain';

const H = 3_600_000;
const T0 = Date.parse('2026-06-01T10:00:00Z');
const iso = (t: number) => new Date(t).toISOString();
function bar(i: number, p: number | null, mm: number | null, wet = false): HourBar {
  return { start: iso(T0 + i * H), end: iso(T0 + (i + 1) * H), probability: p, mm, wet };
}
const now = T0 + 20 * 60_000;

describe('rainNow', () => {
  it('finds the current hour', () => {
    const bars = [bar(0, 0.1, 0), bar(1, 0.9, 1, true)];
    expect(currentBar(bars, now)).toBe(bars[0]);
    expect(rainNow(bars, now).raining).toBe(false);
    expect(rainNow([bar(0, 0.8, 0.4)], now)).toEqual({ raining: true, mm: 0.4, probability: 0.8 });
  });
  it('is dry and unknown without data', () => {
    expect(rainNow([], now)).toEqual({ raining: false, mm: null, probability: null });
  });
});

describe('stripBars', () => {
  it('keeps past hours before the current one and caps both sides', () => {
    const bars = [bar(-2, 0, 0), bar(-1, 0, 0), bar(0, 0, 0), bar(1, 0, 0), bar(2, 0, 0)];
    expect(stripBars(bars, now, 2, 1).map((b) => b.start)).toEqual([
      iso(T0 - H),
      iso(T0),
      iso(T0 + H),
    ]);
  });
});

describe('readoutBars', () => {
  const hour = (i: number, p: number, mm: number) => ({
    start: iso(T0 + i * H),
    end: iso(T0 + (i + 1) * H),
    probability_percent: p,
    precipitation_mm: mm,
    rain_mm: null,
  });
  const th = { probability_threshold: 60, precipitation_mm_threshold: 0.5 };
  it('marks past hours wet by millimetres only', () => {
    const bars = readoutBars([hour(-2, 90, 0), hour(-1, 0, 1), hour(1, 90, 0)], th, now);
    expect(bars.map((b) => b.wet)).toEqual([false, true, true]);
  });
});

describe('rainWarning', () => {
  it('warns when it rains now', () => {
    expect(rainWarning([bar(0, 0.7, 0.6, true)], now, 60)).toEqual({
      kind: 'now',
      start: iso(T0),
      percent: 70,
    });
  });
  it('warns for a wet hour inside the job only', () => {
    const bars = [bar(0, 0.1, 0), bar(1, 0.1, 0), bar(2, 0.8, 1, true)];
    expect(rainWarning(bars, now, 60)).toBeNull();
    expect(rainWarning(bars, now, 150)).toEqual({
      kind: 'soon',
      start: iso(T0 + 2 * H),
      percent: 80,
    });
  });
  it('is null when dry', () => {
    expect(rainWarning([bar(0, 0, 0)], now, 120)).toBeNull();
  });
  it('warns about a wet lawn until it is dry', () => {
    const until = iso(T0 + 2 * H);
    expect(rainWarning([bar(0, 0, 0)], now, 60, until)).toEqual({
      kind: 'wet',
      start: until,
      percent: null,
    });
    expect(rainWarning([bar(0, 0, 0)], T0 + 3 * H, 60, until)).toBeNull();
  });
});
