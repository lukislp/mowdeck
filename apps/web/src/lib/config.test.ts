import { afterEach, describe, expect, it, vi } from 'vitest';

import { DEFAULT_CONFIG, loadConfig, parseConfig, resetConfig } from './config';
import { DEFAULT_SOURCE } from './legal/source';

const OSM = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';

function json(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
    ...init,
  });
}

describe('parseConfig', () => {
  it('defaults to the grid without tiles', () => {
    expect(parseConfig(null)).toEqual(DEFAULT_CONFIG);
    expect(parseConfig({})).toEqual(DEFAULT_CONFIG);
    expect(parseConfig({ mapTiles: null })).toEqual({
      mapTiles: null,
      legalEnabled: false,
      cameraEnabled: false,
      apiExplorer: false,
      source: DEFAULT_SOURCE,
    });
    expect(parseConfig('nope')).toEqual(DEFAULT_CONFIG);
  });

  it('accepts a tile template with attribution and max zoom', () => {
    expect(
      parseConfig({ mapTiles: { url: OSM, attribution: ' Tiles: me ', maxZoom: 18 } }),
    ).toEqual({
      mapTiles: { url: OSM, attribution: 'Tiles: me', maxZoom: 18 },
      legalEnabled: false,
      cameraEnabled: false,
      apiExplorer: false,
      source: DEFAULT_SOURCE,
    });
  });

  it('fills the OSM attribution and the default zoom', () => {
    expect(parseConfig({ mapTiles: { url: OSM, maxZoom: 99 } })).toEqual({
      mapTiles: { url: OSM, attribution: '© OpenStreetMap contributors', maxZoom: 19 },
      legalEnabled: false,
      cameraEnabled: false,
      apiExplorer: false,
      source: DEFAULT_SOURCE,
    });
    const other = parseConfig({ mapTiles: { url: 'https://t.example/{z}/{x}/{y}.png' } });
    expect(other.mapTiles?.attribution).toBeNull();
  });

  it('rejects templates without placeholders or with another scheme', () => {
    expect(parseConfig({ mapTiles: { url: 'https://t.example/tile.png' } })).toEqual(
      DEFAULT_CONFIG,
    );
    expect(parseConfig({ mapTiles: { url: 'javascript:{z}{x}{y}' } })).toEqual(DEFAULT_CONFIG);
    expect(parseConfig({ mapTiles: { url: 'https://t.example/"{z}/{x}/{y}' } })).toEqual(
      DEFAULT_CONFIG,
    );
  });
});

describe('parseConfig legalEnabled', () => {
  it('keeps the legal links off unless the flag is exactly true', () => {
    expect(DEFAULT_CONFIG.legalEnabled).toBe(false);
    expect(parseConfig({ legalEnabled: false }).legalEnabled).toBe(false);
    expect(parseConfig({ legalEnabled: 'true' }).legalEnabled).toBe(false);
    expect(parseConfig({ legalEnabled: 1 }).legalEnabled).toBe(false);
    expect(parseConfig({ mapTiles: null }).legalEnabled).toBe(false);
  });

  it('turns the legal links on with or without map tiles', () => {
    expect(parseConfig({ mapTiles: null, legalEnabled: true })).toEqual({
      mapTiles: null,
      legalEnabled: true,
      cameraEnabled: false,
      apiExplorer: false,
      source: DEFAULT_SOURCE,
    });
    const both = parseConfig({ mapTiles: { url: OSM }, legalEnabled: true });
    expect(both.legalEnabled).toBe(true);
    expect(both.mapTiles?.url).toBe(OSM);
    const badTiles = parseConfig({ mapTiles: { url: 'ftp://x/{z}/{x}/{y}' }, legalEnabled: true });
    expect(badTiles).toEqual({
      mapTiles: null,
      legalEnabled: true,
      cameraEnabled: false,
      apiExplorer: false,
      source: DEFAULT_SOURCE,
    });
  });
});

describe('cameraEnabled', () => {
  it('is on only for a literal true', () => {
    expect(parseConfig({ mapTiles: null, cameraEnabled: true }).cameraEnabled).toBe(true);
    expect(parseConfig({ cameraEnabled: 'true' }).cameraEnabled).toBe(false);
    expect(parseConfig({ cameraEnabled: 1 }).cameraEnabled).toBe(false);
    expect(parseConfig({ cameraEnabled: true, mapTiles: { url: 'bad' } })).toEqual({
      mapTiles: null,
      legalEnabled: false,
      cameraEnabled: true,
      apiExplorer: false,
      source: DEFAULT_SOURCE,
    });
  });
});

describe('apiExplorer', () => {
  it('is off unless the flag is exactly true', () => {
    expect(DEFAULT_CONFIG.apiExplorer).toBe(false);
    expect(parseConfig({ apiExplorer: 'true' }).apiExplorer).toBe(false);
    expect(parseConfig({ apiExplorer: 1 })).toBe(DEFAULT_CONFIG);
    expect(parseConfig({ apiExplorer: true })).toEqual({
      mapTiles: null,
      legalEnabled: false,
      cameraEnabled: false,
      apiExplorer: true,
      source: DEFAULT_SOURCE,
    });
  });
});

describe('loadConfig', () => {
  afterEach(() => resetConfig());

  it('fetches once and caches', async () => {
    const fetchFn = vi.fn(async () => json({ mapTiles: { url: OSM } }));
    const first = await loadConfig(fetchFn);
    const second = await loadConfig(fetchFn);
    expect(first.mapTiles?.url).toBe(OSM);
    expect(second).toBe(first);
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });

  it('reads the legal flag from /config.json', async () => {
    const config = await loadConfig(async () => json({ mapTiles: null, legalEnabled: true }));
    expect(config.legalEnabled).toBe(true);
  });

  it('falls back on errors, missing files and HTML', async () => {
    expect(await loadConfig(async () => Promise.reject(new Error('offline')))).toEqual(
      DEFAULT_CONFIG,
    );
    resetConfig();
    expect(await loadConfig(async () => new Response('', { status: 404 }))).toEqual(DEFAULT_CONFIG);
    resetConfig();
    const html = new Response('<!doctype html>', { headers: { 'content-type': 'text/html' } });
    expect(await loadConfig(async () => html)).toEqual(DEFAULT_CONFIG);
  });
});

describe('source', () => {
  it('reads the build and its repository', () => {
    const config = parseConfig({
      source: { url: 'https://github.com/someone/fork', version: '0.4.0', ref: 'v0.4.0' },
    });
    expect(config.source).toEqual({
      repository: 'https://github.com/someone/fork',
      version: '0.4.0',
      ref: 'v0.4.0',
    });
  });

  it('keeps the official repository for a development build', () => {
    expect(parseConfig({ source: { url: '', version: '', ref: '' } })).toBe(DEFAULT_CONFIG);
    expect(parseConfig({ source: { url: 'javascript:alert(1)' } }).source).toBe(DEFAULT_SOURCE);
  });
});
