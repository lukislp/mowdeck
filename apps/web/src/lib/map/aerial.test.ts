import { describe, expect, it, vi } from 'vitest';

import {
  AERIAL_PREFS_KEY,
  DEFAULT_AERIAL_PREFS,
  clampOpacity,
  fetchAerialInfo,
  loadAerialPrefs,
  mapAerial,
  saveAerialPrefs,
  tileLoader,
  type AerialInfo,
} from './aerial.js';

const INFO: AerialInfo = {
  mower_id: 'm1',
  enabled: true,
  tile_url: '/api/v1/telemetry/mowers/m1/aerial/{z}/{x}/{y}',
  attribution: 'Bayerische Vermessungsverwaltung – www.geodaten.bayern.de',
  min_zoom: 14,
  max_zoom: 19,
  bounds: { south: 48.1, west: 11.5, north: 48.2, east: 11.6 },
};

function store(initial?: string) {
  const data = new Map<string, string>();
  if (initial !== undefined) data.set(AERIAL_PREFS_KEY, initial);
  return {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
  };
}

describe('aerial prefs', () => {
  it('start off with a readable opacity', () => {
    expect(loadAerialPrefs(store())).toEqual(DEFAULT_AERIAL_PREFS);
    expect(DEFAULT_AERIAL_PREFS.shown).toBe(false);
  });

  it('round-trip and survive broken or hostile storage', () => {
    const s = store();
    saveAerialPrefs({ shown: true, opacity: 0.5 }, s);
    expect(loadAerialPrefs(s)).toEqual({ shown: true, opacity: 0.5 });
    expect(loadAerialPrefs(store('{not json'))).toEqual(DEFAULT_AERIAL_PREFS);
    expect(loadAerialPrefs(store('{"shown":"yes","opacity":"x"}'))).toEqual(DEFAULT_AERIAL_PREFS);
    expect(loadAerialPrefs(null)).toEqual(DEFAULT_AERIAL_PREFS);
    const throwing = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
    };
    expect(loadAerialPrefs(throwing)).toEqual(DEFAULT_AERIAL_PREFS);
    expect(() => saveAerialPrefs(DEFAULT_AERIAL_PREFS, throwing)).not.toThrow();
  });

  it('clamp the opacity to the slider range', () => {
    expect(clampOpacity(0)).toBe(0.2);
    expect(clampOpacity(5)).toBe(1);
    expect(clampOpacity(0.456)).toBe(0.46);
    expect(clampOpacity(NaN)).toBe(DEFAULT_AERIAL_PREFS.opacity);
    expect(clampOpacity('0.5')).toBe(DEFAULT_AERIAL_PREFS.opacity);
  });
});

describe('mapAerial', () => {
  const load = vi.fn();

  it('is null while off, not offered or without a tile route', () => {
    expect(mapAerial(INFO, { shown: false, opacity: 1 }, load)).toBeNull();
    expect(mapAerial(null, { shown: true, opacity: 1 }, load)).toBeNull();
    expect(mapAerial({ ...INFO, enabled: false }, { shown: true, opacity: 1 }, load)).toBeNull();
    expect(mapAerial({ ...INFO, tile_url: null }, { shown: true, opacity: 1 }, load)).toBeNull();
  });

  it('hands the map the route, zoom range, bounds as south-west-north-east and the credit', () => {
    expect(mapAerial(INFO, { shown: true, opacity: 0.6 }, load)).toEqual({
      url: INFO.tile_url,
      opacity: 0.6,
      attribution: INFO.attribution,
      minZoom: 14,
      maxZoom: 19,
      bounds: [48.1, 11.5, 48.2, 11.6],
      load,
    });
  });
});

describe('fetchAerialInfo', () => {
  it('asks the mower route and returns the body', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify(INFO), { status: 200 }));
    await expect(fetchAerialInfo(fetcher as unknown as typeof fetch, 'a/b')).resolves.toEqual(INFO);
    expect(fetcher).toHaveBeenCalledWith(
      '/api/v1/telemetry/mowers/a%2Fb/aerial',
      expect.objectContaining({ headers: { Accept: 'application/json' } }),
    );
  });

  it('turns an error answer into a problem and a network failure into a network problem', async () => {
    const problem = new Response(
      JSON.stringify({ type: 'urn:mowdeck:problem:no-map-yet', title: 'No map yet', status: 404 }),
      { status: 404, headers: { 'Content-Type': 'application/problem+json' } },
    );
    await expect(
      fetchAerialInfo((async () => problem) as unknown as typeof fetch, 'm1'),
    ).rejects.toMatchObject({ status: 404, slug: 'no-map-yet' });
    await expect(
      fetchAerialInfo(
        (async () => {
          throw new TypeError('offline');
        }) as unknown as typeof fetch,
        'm1',
      ),
    ).rejects.toMatchObject({ status: 0 });
  });
});

describe('tileLoader', () => {
  const signal = new AbortController().signal;

  it('returns the image blob', async () => {
    const fetcher = vi.fn(
      async () =>
        new Response(new Uint8Array([1, 2]), {
          status: 200,
          headers: { 'Content-Type': 'image/jpeg' },
        }),
    );
    const blob = await tileLoader(fetcher as unknown as typeof fetch)('/t/1/2/3', signal);
    expect(blob.type).toBe('image/jpeg');
    expect(fetcher).toHaveBeenCalledWith(
      '/t/1/2/3',
      expect.objectContaining({ headers: { Accept: 'image/*' }, signal }),
    );
  });

  it('rejects a failed answer and anything that is not an image', async () => {
    const failing = (async () =>
      new Response('{}', {
        status: 429,
        headers: { 'Content-Type': 'application/problem+json' },
      })) as unknown as typeof fetch;
    await expect(tileLoader(failing)('/t', signal)).rejects.toMatchObject({ status: 429 });
    const html = (async () =>
      new Response('<html>', {
        status: 200,
        headers: { 'Content-Type': 'text/html' },
      })) as unknown as typeof fetch;
    await expect(tileLoader(html)('/t', signal)).rejects.toThrow('not an image');
  });
});
