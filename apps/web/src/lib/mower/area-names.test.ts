import { describe, expect, it, vi } from 'vitest';

import { AREA_NAME_MAX, initialRenameValue, renameIntent } from './area-names';

describe('renameIntent', () => {
  const area = { id: 'a1', name: 'Front' };

  it('sets a new trimmed name', () => {
    expect(renameIntent('  Vor   garten ', area, {})).toEqual({ kind: 'set', name: 'Vor garten' });
    expect(renameIntent('Hof', area, { a1: 'Vorgarten' })).toEqual({ kind: 'set', name: 'Hof' });
  });

  it('drops the custom name for a blank field or the mower name, else changes nothing', () => {
    expect(renameIntent('  ', area, { a1: 'Vorgarten' })).toEqual({ kind: 'reset' });
    expect(renameIntent('Front', area, { a1: 'Vorgarten' })).toEqual({ kind: 'reset' });
    expect(renameIntent('', area, {})).toEqual({ kind: 'none' });
    expect(renameIntent('Front', area, null)).toEqual({ kind: 'none' });
    expect(renameIntent('Vorgarten', area, { a1: 'Vorgarten' })).toEqual({ kind: 'none' });
  });

  it('refuses names above the limit', () => {
    expect(renameIntent('x'.repeat(AREA_NAME_MAX), area, {}).kind).toBe('set');
    expect(renameIntent('x'.repeat(AREA_NAME_MAX + 1), area, {})).toEqual({ kind: 'too_long' });
  });

  it('treats a hash name of the mower as no name', () => {
    const hashed = { id: 'h', name: '4525245168006937683' };
    expect(renameIntent('4525245168006937683', hashed, {})).toEqual({
      kind: 'set',
      name: '4525245168006937683',
    });
  });
});

describe('initialRenameValue', () => {
  it('starts with the custom name, else the own name, else empty', () => {
    expect(initialRenameValue({ id: 'a', name: 'Front' }, { a: 'Hof' })).toBe('Hof');
    expect(initialRenameValue({ id: 'a', name: ' Front ' }, {})).toBe('Front');
    expect(initialRenameValue({ id: 'a', name: '4525245168006937683' }, undefined)).toBe('');
  });
});

describe('saveAreaName', () => {
  it('PUTs the name and answers all names', async () => {
    const put = vi.fn(async () => ({ data: { names: { a1: 'Hof' } }, error: undefined }));
    vi.doMock('$lib/api/client', () => ({
      fleetApi: { PUT: put },
      unwrap: async (p: Promise<{ data: unknown }>) => (await p).data,
    }));
    vi.resetModules();
    const { saveAreaName } = await import('./area-names');
    await expect(saveAreaName('m-1', 'a1', null)).resolves.toEqual({ a1: 'Hof' });
    expect(put).toHaveBeenCalledWith('/api/v1/fleet/mowers/{mower_id}/area-names/{area_id}', {
      params: { path: { mower_id: 'm-1', area_id: 'a1' } },
      body: { name: null },
    });
    vi.doUnmock('$lib/api/client');
  });
});
