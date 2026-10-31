import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  compareVersions,
  entryText,
  limitEntries,
  parseVersion,
  parseWhatsNew,
  unseenReleases,
  type WhatsNewRelease,
} from './model.js';
import { fetchWhatsNew, SEEN_KEY } from './whats-new.svelte.js';

const entry = (id: string, extra: Partial<WhatsNewRelease['entries'][number]> = {}) => ({
  id,
  kind: 'feature' as const,
  en: `English ${id}`,
  de: null,
  href: null,
  ...extra,
});
const release = (version: string, ...ids: string[]): WhatsNewRelease => ({
  version,
  date: '2026-10-25',
  entries: ids.map((id) => entry(id)),
});

describe('versions', () => {
  it('parses plain versions only', () => {
    expect(parseVersion('v1.2.3')).toEqual([1, 2, 3]);
    expect(parseVersion('1.2.3-rc.1')).toBeNull();
    expect(parseVersion(null)).toBeNull();
  });

  it('compares numerically, not as text', () => {
    expect(compareVersions('0.10.0', '0.9.0')).toBeGreaterThan(0);
    expect(compareVersions('1.0.0', 'v1.0.0')).toBe(0);
    expect(compareVersions('0.1.0', '0.1.1')).toBeLessThan(0);
    expect(compareVersions('x', '1.0.0')).toBeNull();
  });
});

describe('parseWhatsNew', () => {
  it('drops invalid releases and entries and sorts newest first', () => {
    const out = parseWhatsNew({
      releases: [
        {
          version: '0.2.0',
          date: '2026-01-01',
          entries: [{ id: 'a', en: 'A', href: 'https://evil.example' }],
        },
        {
          version: '0.10.0',
          date: 'x',
          entries: [{ id: 'b', en: 'B', de: 'Be', kind: 'fix', href: '/mowers' }, { nope: 1 }],
        },
        { version: 'bad', entries: [{ id: 'c', en: 'C' }] },
        { version: '0.3.0', entries: [] },
      ],
    });
    expect(out.map((r) => r.version)).toEqual(['0.10.0', '0.2.0']);
    expect(out[0]?.entries).toEqual([{ id: 'b', kind: 'fix', en: 'B', de: 'Be', href: '/mowers' }]);
    expect(out[0]?.date).toBe('');
    expect(out[1]?.entries[0]?.href).toBeNull();
    expect(parseWhatsNew('<html>')).toEqual([]);
  });
});

describe('unseenReleases', () => {
  const all = [release('0.3.0', 'c'), release('0.2.0', 'b'), release('0.1.0', 'a')];

  it('lists every release since the last seen one, several at once', () => {
    expect(unseenReleases(all, '0.1.0', '0.3.0').map((r) => r.version)).toEqual(['0.3.0', '0.2.0']);
  });

  it('is empty when up to date or on a dev build', () => {
    expect(unseenReleases(all, '0.3.0', '0.3.0')).toEqual([]);
    expect(unseenReleases(all, null, null)).toEqual([]);
    expect(unseenReleases(all, '0.1.0', '0.4.0-rc.1')).toEqual([]);
  });

  it('ignores releases newer than the running version', () => {
    expect(unseenReleases(all, '0.1.0', '0.2.0').map((r) => r.version)).toEqual(['0.2.0']);
  });

  it('shows everything on a device that has not seen any version', () => {
    expect(unseenReleases(all, null, '0.3.0')).toHaveLength(3);
    expect(unseenReleases(all, 'garbage', '0.3.0')).toHaveLength(3);
  });
});

describe('texts', () => {
  it('prefers German for German, falls back to the English subject', () => {
    const curated = entry('a', { de: 'Deutsch' });
    expect(entryText(curated, 'de')).toBe('Deutsch');
    expect(entryText(curated, 'en')).toBe('English a');
    expect(entryText(entry('b'), 'de')).toBe('English b');
  });

  it('caps the entries across releases and counts the rest', () => {
    const { releases, hidden } = limitEntries(
      [release('0.2.0', 'a', 'b'), release('0.1.0', 'c', 'd')],
      3,
    );
    expect(releases.map((r) => r.entries.length)).toEqual([2, 1]);
    expect(hidden).toBe(1);
  });
});

describe('store', () => {
  const file = { releases: [release('0.2.0', 'b'), release('0.1.0', 'a')] };

  function serve(version: string | null, notes: unknown = file, config: object = {}) {
    const fetchFn = vi.fn(async (url: string) => {
      const body = url === '/config.json' ? { ...config, source: { version } } : notes;
      return new Response(JSON.stringify(body), {
        status: notes === null && url !== '/config.json' ? 404 : 200,
        headers: { 'content-type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchFn);
    return fetchFn;
  }

  beforeEach(() => {
    localStorage.clear();
  });
  afterEach(() => vi.unstubAllGlobals());

  async function fresh() {
    vi.resetModules();
    const mod = await import('./whats-new.svelte.js');
    return mod.whatsNew;
  }

  it('opens once after a version change and stays closed after dismissing', async () => {
    localStorage.setItem(SEEN_KEY, '0.1.0');
    serve('0.2.0');
    let store = await fresh();
    await store.load();
    expect(store.open).toBe(true);
    expect(store.unseen.map((r) => r.version)).toEqual(['0.2.0']);
    store.dismiss();
    expect(store.open).toBe(false);
    expect(localStorage.getItem(SEEN_KEY)).toBe('0.2.0');

    store = await fresh();
    await store.load();
    expect(store.open).toBe(false);
  });

  it('records the version silently when nothing user-facing is new', async () => {
    localStorage.setItem(SEEN_KEY, '0.2.0');
    serve('0.2.1');
    const store = await fresh();
    await store.load();
    expect(store.open).toBe(false);
    expect(localStorage.getItem(SEEN_KEY)).toBe('0.2.1');
  });

  it('does nothing without notes or on a dev build', async () => {
    serve('0.2.0', null);
    let store = await fresh();
    await store.load();
    expect(store.open).toBe(false);
    expect(localStorage.getItem(SEEN_KEY)).toBeNull();

    serve(null);
    store = await fresh();
    await store.load();
    expect(store.open).toBe(false);
  });

  it('does not ask for the notes when the image ships none', async () => {
    const fetchFn = serve('0.2.0', file, { whatsNew: false });
    const store = await fresh();
    await store.load();
    expect(store.loaded).toBe(true);
    expect(store.open).toBe(false);
    expect(fetchFn.mock.calls.map(([url]) => url)).toEqual(['/config.json']);
    expect(localStorage.getItem(SEEN_KEY)).toBeNull();
  });

  it('asks for the notes when the image ships them', async () => {
    const fetchFn = serve('0.2.0', file, { whatsNew: true });
    const store = await fresh();
    await store.load();
    expect(fetchFn.mock.calls.map(([url]) => url)).toEqual(['/config.json', '/whats-new.json']);
    expect(store.open).toBe(true);
  });

  it('fetchWhatsNew rejects the SPA fallback page', async () => {
    const html = vi.fn(
      async () => new Response('<html>', { headers: { 'content-type': 'text/html' } }),
    );
    expect(await fetchWhatsNew(html as unknown as typeof fetch)).toBeNull();
  });
});
