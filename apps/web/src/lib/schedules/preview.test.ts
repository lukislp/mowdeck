import { describe, expect, it } from 'vitest';

import { hourBars, parsePreview, verdictBadge } from './preview';
import type { ForecastHour, Preview, RainDecision } from './types';

const decision = (verdict: RainDecision['verdict'], hours: ForecastHour[] = []) =>
  ({
    verdict,
    holds: verdict === 'hold',
    reason: 'test',
    provider: 'static',
    latitude: 48.1,
    longitude: 11.5,
    location_source: 'site',
    attribution: null,
    wet: false,
    pause_until: null,
    // The settings the Scheduler applied (the schedule's); the web shows them, it has no defaults.
    settings: {
      enabled: true,
      probability_threshold: 60,
      precipitation_mm_threshold: 0.5,
      lookahead_hours: 3,
      cooldown_hours: 2,
      run_when_unknown: true,
    },
    hours: hours.map((hour) => ({ past: false, ...hour })),
  }) satisfies RainDecision;

const item = (at: string, skipped = false) => ({
  at,
  window_end: new Date(Date.parse(at) + 3_600_000).toISOString(),
  skipped,
});

describe('verdictBadge', () => {
  it('maps every verdict to one of the five badges', () => {
    expect(verdictBadge('run')).toBe('run');
    expect(verdictBadge('hold')).toBe('hold');
    expect(verdictBadge('unknown_hold')).toBe('hold');
    expect(verdictBadge('unknown_run')).toBe('unknown');
    expect(verdictBadge('no_location')).toBe('no_location');
    expect(verdictBadge('disabled')).toBe('disabled');
    expect(verdictBadge(null)).toBe('unknown');
  });
});

describe('parsePreview', () => {
  it('finds the next firing after a skipped one and orders items and hours', () => {
    const preview: Preview = {
      items: [item('2026-10-14T05:00:00Z'), item('2026-10-12T05:00:00Z', true)],
      weather: decision('hold', [
        {
          start: '2026-10-14T06:00:00Z',
          end: '2026-10-14T07:00:00Z',
          precipitation_mm: 1,
          probability_percent: 90,
          rain_mm: 1,
        },
        {
          start: '2026-10-14T05:00:00Z',
          end: '2026-10-14T06:00:00Z',
          precipitation_mm: 0,
          probability_percent: 10,
          rain_mm: 0,
        },
      ]),
      weather_mower_id: 'm1',
    };
    const view = parsePreview(preview);
    expect(view.next?.at).toBe('2026-10-14T05:00:00Z');
    expect(view.skipped?.at).toBe('2026-10-12T05:00:00Z');
    expect(view.badge).toBe('hold');
    expect(view.hours.map((h) => h.start)).toEqual([
      '2026-10-14T05:00:00Z',
      '2026-10-14T06:00:00Z',
    ]);
  });

  it('has no skipped item when the first firing runs', () => {
    const view = parsePreview({
      items: [item('2026-10-12T05:00:00Z')],
      weather: decision('run'),
      weather_mower_id: 'm1',
    });
    expect(view.skipped).toBeNull();
    expect(view.badge).toBe('run');
  });

  it('is unknown without weather and disabled without any firing', () => {
    expect(
      parsePreview({ items: [item('2026-10-12T05:00:00Z')], weather: null, weather_mower_id: null })
        .badge,
    ).toBe('unknown');
    const empty = parsePreview(null);
    expect(empty).toMatchObject({ next: null, skipped: null, badge: 'disabled', hours: [] });
  });

  it('reports a skipped only firing', () => {
    const view = parsePreview({
      items: [item('2026-10-12T05:00:00Z', true)],
      weather: null,
      weather_mower_id: null,
    });
    expect(view.next).toBeNull();
    expect(view.skipped?.at).toBe('2026-10-12T05:00:00Z');
  });
});

describe('hourBars', () => {
  const settings = { probability_threshold: 60, precipitation_mm_threshold: 0.5 };

  it('scales the probability and marks hours that reach a threshold', () => {
    const bars = hourBars(
      [
        { start: 'a', end: 'b', precipitation_mm: 0, probability_percent: 20, rain_mm: 0 },
        { start: 'b', end: 'c', precipitation_mm: 0.2, probability_percent: 60, rain_mm: 0.2 },
        { start: 'c', end: 'd', precipitation_mm: 0.7, probability_percent: 10, rain_mm: 0.7 },
        { start: 'd', end: 'e', precipitation_mm: null, probability_percent: null, rain_mm: 0.1 },
      ],
      settings,
    );
    expect(bars.map((b) => b.wet)).toEqual([false, true, true, false]);
    expect(bars[0]?.probability).toBe(0.2);
    expect(bars[3]).toMatchObject({ probability: null, mm: 0.1 });
  });
});
