import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('#lib/api/client.js', () => ({ apiFetch: vi.fn() }));

import { ApiProblem } from '#lib/api/problem.js';
import {
  matterCalls,
  qrPath,
  type MatterApi,
  type MatterMower,
  type MatterPairing,
} from '#lib/matter/api.js';
import { setLocale } from '#lib/paraglide/runtime.js';

import MatterSection from './MatterSection.svelte';

const MOWER: MatterMower = {
  mower_id: '11111111-2222-4333-8444-555555555555',
  name: 'Luba Garten',
  commissioned: false,
  fabric_count: 0,
};

const PAIRING: MatterPairing = {
  ...MOWER,
  manual_code: '3497-011-2332',
  qr_payload: 'MT:Y.K9042C00KA0648G00',
  qr_matrix: ['111', '101', '111'],
};

function fakeApi(overrides: Partial<MatterApi> = {}): MatterApi {
  return {
    mowers: vi.fn(async () => [MOWER]),
    pairing: vi.fn(async () => PAIRING),
    reset: vi.fn(async () => ({ ...PAIRING, manual_code: '1111-222-3333' })),
    ...overrides,
  };
}

beforeEach(() => {
  setLocale('de', { reload: false });
});

describe('MatterSection', () => {
  it('lists the mowers and shows the code only on request', async () => {
    const api = fakeApi();
    render(MatterSection, { api });
    const row = await screen.findByTestId('matter-mower');
    expect(within(row).getByText('Luba Garten')).toBeTruthy();
    expect(within(row).getByTestId('matter-state').textContent).toContain('Nicht gekoppelt');
    expect(screen.queryByTestId('matter-code')).toBeNull();
    expect(api.pairing).not.toHaveBeenCalled();

    await fireEvent.click(screen.getByTestId('matter-show-code'));
    expect((await screen.findByTestId('matter-code')).textContent).toBe('3497-011-2332');
    expect(screen.getByTestId('matter-qr').getAttribute('aria-label')).toContain('Luba Garten');

    await fireEvent.click(screen.getByTestId('matter-show-code'));
    await waitFor(() => expect(screen.queryByTestId('matter-code')).toBeNull());
  });

  it('resets only after a confirmation and shows the new code', async () => {
    const api = fakeApi();
    render(MatterSection, { api });
    await fireEvent.click(await screen.findByTestId('matter-reset'));
    expect(api.reset).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByTestId('matter-reset-confirm'));
    expect(api.reset).toHaveBeenCalledWith(MOWER.mower_id);
    expect((await screen.findByTestId('matter-code')).textContent).toBe('1111-222-3333');
  });

  it('explains an empty list', async () => {
    render(MatterSection, { api: fakeApi({ mowers: vi.fn(async () => []) }) });
    expect((await screen.findByTestId('matter-empty')).textContent).toContain('API-Token');
  });

  it('shows a refused code request at the mower', async () => {
    const api = fakeApi({
      pairing: vi.fn(async () => {
        throw new ApiProblem({ status: 403, title: 'Forbidden' });
      }),
    });
    render(MatterSection, { api });
    await fireEvent.click(await screen.findByTestId('matter-show-code'));
    await waitFor(() => expect(api.pairing).toHaveBeenCalled());
    expect(screen.queryByTestId('matter-code')).toBeNull();
  });
});

describe('matter api', () => {
  it('draws the dark modules with a quiet zone', () => {
    expect(qrPath(['10', '01'], 1)).toEqual({ d: 'M1 1h1v1h-1zM2 2h1v1h-1z', size: 4 });
  });

  it('calls the Matter server without caching and reports problems', async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      expect(init?.cache).toBe('no-store');
      if (url.endsWith('/mowers')) return Response.json({ items: [MOWER] });
      if (url.endsWith('/pairing/reset')) {
        expect(init?.method).toBe('POST');
        return Response.json(PAIRING);
      }
      return new Response(JSON.stringify({ title: 'Not found', status: 404 }), {
        status: 404,
        headers: { 'content-type': 'application/problem+json' },
      });
    });
    const api = matterCalls(fetcher as typeof fetch);
    expect(await api.mowers()).toEqual([MOWER]);
    expect((await api.reset(MOWER.mower_id)).manual_code).toBe('3497-011-2332');
    await expect(api.pairing('x')).rejects.toMatchObject({ status: 404 });
    expect(fetcher.mock.calls[0]![0]).toBe('/api/v1/matter/mowers');
  });
});
