import { describe, expect, it, vi } from 'vitest';

import {
  LAYER_KEY,
  PUBLIC_TILE,
  loadLayer,
  mapLayers,
  coordinatesText,
  mapLinks,
  OSM_COPYRIGHT,
  ownerTileUrl,
  parsePublicTile,
  publicTileLoader,
  publicTileUrl,
  saveLayer,
  type MapLayer,
} from './basemap.js';

const OFFERED: MapLayer[] = [
  {
    id: 'map',
    attribution: '© OpenStreetMap contributors',
    min_zoom: 8,
    max_zoom: 19,
    covers_last: true,
    coverage: null,
    margin_m: 2000,
  },
  {
    id: 'aerial',
    attribution: '',
    min_zoom: 14,
    max_zoom: 19,
    covers_last: false,
    coverage: { south: 47.27, west: 8.97, north: 50.57, east: 13.84 },
    margin_m: 2000,
  },
];

function memoryStore(initial: Record<string, string> = {}) {
  const items = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => items.get(key) ?? null,
    setItem: (key: string, value: string) => void items.set(key, value),
    items,
  };
}

describe('tile routes', () => {
  it('builds the owner route per layer and session', () => {
    expect(ownerTileUrl('m 1', 'map')).toBe(
      '/api/v1/telemetry/mowers/m%201/tracking/map/map/{z}/{x}/{y}',
    );
    expect(ownerTileUrl('m', 'aerial', 's1')).toBe(
      '/api/v1/telemetry/mowers/m/tracking/map/aerial/{z}/{x}/{y}?session_id=s1',
    );
  });

  it('parses only filled public templates', () => {
    expect(publicTileUrl('map')).toBe('map/{z}/{x}/{y}');
    expect(parsePublicTile('aerial/17/68954/45410')).toEqual({
      layer: 'aerial',
      z: 17,
      x: 68954,
      y: 45410,
    });
    expect(parsePublicTile('map/{z}/{x}/{y}')).toBeNull();
    expect(parsePublicTile('osm/1/2/3')).toBeNull();
    expect(parsePublicTile('https://evil.example/map/1/2/3')).toBeNull();
  });
});

describe('publicTileLoader', () => {
  it('posts the token and the tile without cookies or referrer', async () => {
    const fetchFn = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response('x', { status: 200, headers: { 'Content-Type': 'image/png' } }),
    );
    const blob = await publicTileLoader('tok', fetchFn as typeof fetch)(
      'map/15/1/2',
      new AbortController().signal,
    );
    expect(blob.type).toBe('image/png');
    const [url, init] = fetchFn.mock.calls[0]!;
    expect(url).toBe(PUBLIC_TILE);
    expect(init?.method).toBe('POST');
    expect(init?.credentials).toBe('omit');
    expect(init?.referrerPolicy).toBe('no-referrer');
    expect(JSON.parse(String(init?.body))).toEqual({
      token: 'tok',
      layer: 'map',
      z: 15,
      x: 1,
      y: 2,
    });
  });

  it('fails on errors, non-images and foreign URLs', async () => {
    const signal = new AbortController().signal;
    const notFound = vi.fn(async () => new Response('', { status: 404 }));
    await expect(
      publicTileLoader('t', notFound as typeof fetch)('map/1/0/0', signal),
    ).rejects.toThrow();
    const html = vi.fn(
      async () => new Response('<p>', { status: 200, headers: { 'Content-Type': 'text/html' } }),
    );
    await expect(
      publicTileLoader('t', html as typeof fetch)('map/1/0/0', signal),
    ).rejects.toThrow();
    const never = vi.fn();
    await expect(
      publicTileLoader('t', never as typeof fetch)('/etc/1/0/0', signal),
    ).rejects.toThrow();
    expect(never).not.toHaveBeenCalled();
  });
});

describe('mapLayers', () => {
  it('maps the offered layers with labels, zooms, credits and the served area', () => {
    const load = vi.fn();
    const layers = mapLayers(OFFERED, publicTileUrl, load, (id) => id.toUpperCase());
    expect(layers).toEqual([
      {
        id: 'map',
        label: 'MAP',
        url: 'map/{z}/{x}/{y}',
        attribution: '© OpenStreetMap contributors',
        attributionUrl: OSM_COPYRIGHT,
        minZoom: 8,
        maxZoom: 19,
        covered: true,
        margin: 2000,
        coverage: null,
        load,
      },
      {
        id: 'aerial',
        label: 'AERIAL',
        url: 'aerial/{z}/{x}/{y}',
        attribution: null,
        attributionUrl: null,
        minZoom: 14,
        maxZoom: 19,
        covered: false,
        margin: 2000,
        coverage: [47.27, 8.97, 50.57, 13.84],
        load,
      },
    ]);
    expect(mapLayers(undefined, publicTileUrl, load, String)).toEqual([]);
  });
});

describe('remembered layer', () => {
  it('reads and writes only known layers', () => {
    const store = memoryStore({ [LAYER_KEY]: 'aerial' });
    expect(loadLayer(store)).toBe('aerial');
    saveLayer('map', store);
    expect(store.items.get(LAYER_KEY)).toBe('map');
    saveLayer('osm', store);
    saveLayer(null, store);
    expect(store.items.get(LAYER_KEY)).toBe('map');
    expect(loadLayer(memoryStore({ [LAYER_KEY]: 'junk' }))).toBeNull();
    expect(loadLayer(null)).toBeNull();
    const broken = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
    };
    expect(loadLayer(broken)).toBeNull();
    expect(() => saveLayer('map', broken)).not.toThrow();
  });
});

describe('position as text and map links', () => {
  const point = { lat: 48.1371539, lon: -11.5761251 };
  const token = 'Zk3_secret-token-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';

  it('writes decimal degrees with six places and a dot', () => {
    expect(coordinatesText(point)).toBe('48.137154, -11.576125');
    expect(coordinatesText({ lat: 0, lon: 0 })).toBe('0.000000, 0.000000');
  });

  it('builds Apple, Google and OpenStreetMap links on every platform', () => {
    for (const ua of ['Mozilla/5.0 (iPhone)', 'Mozilla/5.0 (Windows NT 10.0)']) {
      const links = mapLinks(point, 'Mäher & Co', ua);
      expect(links.apple).toBe(
        'https://maps.apple.com/?ll=48.137154,-11.576125&q=M%C3%A4her%20%26%20Co',
      );
      expect(links.google).toBe(
        'https://www.google.com/maps/search/?api=1&query=48.137154%2C-11.576125',
      );
      expect(links.osm).toBe(
        'https://www.openstreetmap.org/?mlat=48.137154&mlon=-11.576125#map=18/48.137154/-11.576125',
      );
      expect(links.geo).toBeNull();
    }
    expect(mapLinks(point, 'Mower', 'Mozilla/5.0 (Linux; Android 15)').geo).toBe(
      'geo:48.137154,-11.576125?q=48.137154%2C-11.576125',
    );
  });

  it('never carries the share token or the page address', () => {
    const before = globalThis.location?.href;
    const links = mapLinks(point, 'Mäher', 'Mozilla/5.0 (Linux; Android 15)');
    for (const url of Object.values(links)) {
      expect(url).not.toContain(token);
      expect(url).not.toContain('/track');
      if (before) expect(url).not.toContain(before);
    }
  });
});
