import { describe, expect, it, vi } from 'vitest';

import { ApiProblem } from '$lib/api/problem';

import {
  DEFAULT_HEAT_PREFS,
  HEAT_PREFS_KEY,
  fetchHeatmap,
  heatLayerData,
  heatmapUrl,
  isHeatLayer,
  isHeatPeriod,
  isNoMapYet,
  jobAnchor,
  legendKindOf,
  loadHeatPrefs,
  periodWindow,
  saveHeatPrefs,
  sortedGaps,
  type HeatmapResponse,
} from './heatmap';

const NOW = new Date('2026-10-11T12:00:00Z');

const answer = (over: Partial<HeatmapResponse> = {}): HeatmapResponse => ({
  mower_id: 'm1',
  layer: 'coverage',
  cell_m: 1,
  from: '2026-10-04T12:00:00Z',
  to: '2026-10-11T12:00:00Z',
  reference: { origin_lat: 48, origin_lon: 11, rotation_deg: 0 },
  cells: [{ x: 1, y: 2, value: 0.5, n: 3 }],
  legend: { kind: 'ratio', min: 0, max: 1 },
  gaps: [
    {
      polygon: [
        [0, 0],
        [1, 0],
        [1, 1],
      ],
      area_m2: 0.5,
    },
    {
      polygon: [
        [2, 2],
        [5, 2],
        [5, 4],
      ],
      area_m2: 3,
    },
  ],
  samples: 12,
  truncated: false,
  ...over,
});

function memory(initial: Record<string, string> = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => void data.set(key, value),
    data,
  };
}

describe('periodWindow', () => {
  it('counts 24 h, 7 and 30 days back from now', () => {
    expect(periodWindow('24h', NOW)).toEqual({
      from: '2026-10-10T12:00:00.000Z',
      to: NOW.toISOString(),
    });
    expect(periodWindow('7d', NOW).from).toBe('2026-10-04T12:00:00.000Z');
    expect(periodWindow('30d', NOW).from).toBe('2026-09-11T12:00:00.000Z');
  });

  it('starts the last job at the newest start and ends it at the next return', () => {
    const commands = [
      { kind: 'return_to_dock', issued_at: '2026-10-11T10:30:00Z' },
      { kind: 'start_mow', issued_at: '2026-10-11T09:00:00Z' },
      { kind: 'pause_mow', issued_at: '2026-10-11T09:30:00Z' },
      { kind: 'start_mow', issued_at: '2026-10-10T09:00:00Z' },
    ];
    expect(periodWindow('job', NOW, commands)).toEqual({
      from: '2026-10-11T09:00:00.000Z',
      to: '2026-10-11T10:30:00Z',
    });
  });

  it('keeps a running job open until now', () => {
    const commands = [
      { kind: 'return_to_dock', issued_at: '2026-10-11T08:00:00Z' },
      { kind: 'start_mow', issued_at: '2026-10-11T11:00:00Z' },
    ];
    expect(periodWindow('job', NOW, commands).to).toBe(NOW.toISOString());
  });

  it('falls back to 24 h without a start', () => {
    expect(periodWindow('job', NOW, [{ kind: 'pause_mow', issued_at: 'x' }])).toEqual({
      from: '2026-10-10T12:00:00.000Z',
      to: NOW.toISOString(),
      fallback: true,
    });
  });
});

describe('request', () => {
  const window = { from: '2026-10-10T12:00:00.000Z', to: '2026-10-11T12:00:00.000Z' };

  it('builds the query of the contract', () => {
    const url = new URL(heatmapUrl('a b', 'wifi', window, 2), 'http://x');
    expect(url.pathname).toBe('/api/v1/telemetry/mowers/a%20b/heatmap');
    expect(Object.fromEntries(url.searchParams)).toEqual({
      layer: 'wifi',
      from: window.from,
      to: window.to,
      cell: '2',
    });
  });

  it('returns the answer', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify(answer()), { status: 200 }));
    const result = await fetchHeatmap(
      fetcher as unknown as typeof fetch,
      'm1',
      'coverage',
      window,
      1,
    );
    expect(result.samples).toBe(12);
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it('turns 404 no-map-yet and network failures into problems', async () => {
    const noMap = vi.fn(
      async () =>
        new Response(
          JSON.stringify({ type: 'urn:mowdeck:problem:no-map-yet', title: 'No map', status: 404 }),
          { status: 404, headers: { 'content-type': 'application/problem+json' } },
        ),
    );
    const problem = await fetchHeatmap(
      noMap as unknown as typeof fetch,
      'm1',
      'rtk',
      window,
      1,
    ).catch((e: unknown) => e);
    expect(problem).toBeInstanceOf(ApiProblem);
    expect(isNoMapYet(problem as ApiProblem)).toBe(true);

    const down = vi.fn(async () => {
      throw new TypeError('offline');
    });
    const network = await fetchHeatmap(
      down as unknown as typeof fetch,
      'm1',
      'rtk',
      window,
      1,
    ).catch((e: unknown) => e);
    expect((network as ApiProblem).slug).toBe('network');
    expect(isNoMapYet(network as ApiProblem)).toBe(false);
  });
});

describe('answer helpers', () => {
  it('maps the answer to the map layer; gaps only for coverage', () => {
    expect(heatLayerData(null)).toBeNull();
    const coverage = heatLayerData(answer({ cell_m: 2 }));
    expect(coverage).toMatchObject({ cellM: 2, legend: { kind: 'ratio' } });
    expect(coverage?.gaps).toHaveLength(2);
    expect(heatLayerData(answer({ layer: 'wifi', legend: { kind: 'dbm' } }))?.gaps).toEqual([]);
  });

  it('sorts gaps by area and keeps their index', () => {
    expect(sortedGaps(answer()).map((g) => g.index)).toEqual([1, 0]);
    expect(sortedGaps(null)).toEqual([]);
  });

  it('validates layers and periods', () => {
    expect(isHeatLayer('mnet')).toBe(true);
    expect(isHeatLayer('gps')).toBe(false);
    expect(isHeatPeriod('job')).toBe(true);
    expect(isHeatPeriod(7)).toBe(false);
  });
});

describe('prefs', () => {
  it('round-trips the choice', () => {
    const store = memory();
    saveHeatPrefs({ layer: 'wifi', period: '30d', cell: 2 }, store);
    expect(JSON.parse(store.data.get(HEAT_PREFS_KEY)!)).toEqual({
      layer: 'wifi',
      period: '30d',
      cell: 2,
    });
    expect(loadHeatPrefs(store)).toEqual({ layer: 'wifi', period: '30d', cell: 2 });
  });

  it('falls back field by field and on broken storage', () => {
    expect(loadHeatPrefs(memory({ [HEAT_PREFS_KEY]: '{"layer":"rtk","cell":7}' }))).toEqual({
      ...DEFAULT_HEAT_PREFS,
      layer: 'rtk',
    });
    expect(loadHeatPrefs(memory({ [HEAT_PREFS_KEY]: 'not json' }))).toEqual(DEFAULT_HEAT_PREFS);
    expect(loadHeatPrefs(null)).toEqual(DEFAULT_HEAT_PREFS);
    const throwing = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
    };
    expect(loadHeatPrefs(throwing)).toEqual(DEFAULT_HEAT_PREFS);
    expect(() => saveHeatPrefs(DEFAULT_HEAT_PREFS, throwing)).not.toThrow();
  });
});

describe('legendKindOf', () => {
  it('maps each layer to its scale', () => {
    expect(legendKindOf('coverage')).toBe('ratio');
    expect(legendKindOf('off')).toBe('ratio');
    expect(legendKindOf('rtk')).toBe('fix');
    expect(legendKindOf('wifi')).toBe('dbm');
    expect(legendKindOf('mnet')).toBe('dbm');
  });
});

describe('jobAnchor', () => {
  it('changes only with the job', () => {
    const start = { kind: 'start_mow', issued_at: '2026-10-11T09:00:00Z' };
    const pause = { kind: 'pause_mow', issued_at: '2026-10-11T09:10:00Z' };
    expect(jobAnchor([])).toBe('');
    expect(jobAnchor([start])).toBe(jobAnchor([start, pause]));
    expect(
      jobAnchor([start, { kind: 'return_to_dock', issued_at: '2026-10-11T10:00:00Z' }]),
    ).not.toBe(jobAnchor([start]));
  });
});
