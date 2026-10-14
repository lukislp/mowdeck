import { afterEach, describe, expect, it, vi } from 'vitest';

const post = vi.fn();
const get = vi.fn();
vi.mock('$lib/api/client', () => ({
  fleetApi: { POST: (...a: unknown[]) => post(...a), GET: (...a: unknown[]) => get(...a) },
  unwrap: async (p: Promise<unknown>) => p,
}));

import { createSite, deviceTimeZone, listSites, newSiteBody } from './sites';

afterEach(() => {
  vi.restoreAllMocks();
  post.mockReset();
  get.mockReset();
});

describe('deviceTimeZone', () => {
  it('uses the browser zone', () => {
    vi.spyOn(Intl, 'DateTimeFormat').mockReturnValue({
      resolvedOptions: () => ({ timeZone: 'Europe/Berlin' }),
    } as Intl.DateTimeFormat);
    expect(deviceTimeZone()).toBe('Europe/Berlin');
  });

  it('falls back to UTC when empty or failing', () => {
    vi.spyOn(Intl, 'DateTimeFormat').mockReturnValue({
      resolvedOptions: () => ({ timeZone: '' }),
    } as Intl.DateTimeFormat);
    expect(deviceTimeZone()).toBe('UTC');
    vi.spyOn(Intl, 'DateTimeFormat').mockImplementation(() => {
      throw new Error('no intl');
    });
    expect(deviceTimeZone()).toBe('UTC');
  });
});

describe('newSiteBody', () => {
  it('trims the name and keeps a given zone', () => {
    expect(newSiteBody({ name: '  Garden ', timezone: 'Asia/Tokyo' })).toEqual({
      name: 'Garden',
      timezone: 'Asia/Tokyo',
    });
  });

  it('fills a missing or blank zone with the device zone', () => {
    vi.spyOn(Intl, 'DateTimeFormat').mockReturnValue({
      resolvedOptions: () => ({ timeZone: 'Europe/Berlin' }),
    } as Intl.DateTimeFormat);
    expect(newSiteBody({ name: 'A' }).timezone).toBe('Europe/Berlin');
    expect(newSiteBody({ name: 'A', timezone: '  ' }).timezone).toBe('Europe/Berlin');
  });
});

describe('requests', () => {
  it('creates a site with the normalised body', async () => {
    post.mockResolvedValue({ id: 's1' });
    await createSite({ name: ' X ', timezone: 'UTC' });
    expect(post).toHaveBeenCalledWith('/api/v1/fleet/sites', {
      body: { name: 'X', timezone: 'UTC' },
    });
  });

  it('lists the sites with the default one', async () => {
    get.mockResolvedValue({ items: [{ id: 'a' }] });
    expect(await listSites()).toEqual([{ id: 'a' }]);
    expect(get).toHaveBeenCalledWith('/api/v1/fleet/sites', {
      params: { query: { include_default: true } },
    });
  });
});
