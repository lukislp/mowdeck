import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const get = vi.fn();
const post = vi.fn();
vi.mock('$lib/api/client', () => ({
  identityApi: {
    GET: (...a: unknown[]) => get(...a),
    POST: (...a: unknown[]) => post(...a),
    DELETE: vi.fn(),
  },
  unwrap: async (p: Promise<unknown>) => p,
}));
vi.mock('$lib/auth/session.svelte', () => ({ session: { me: { account_id: 'acc-1' } } }));

import { setLocale } from '$lib/paraglide/runtime';
import { CATALOGUE } from '$lib/settings/token-permissions.fixture';

import ApiTokensSection from './ApiTokensSection.svelte';

const TOKEN = {
  id: 't1',
  name: 'Home Assistant',
  prefix: 'mdk_ab12',
  created_by: 'u1',
  grants: [],
  permissions: ['fleet.manage', 'mower.control', 'mower.drive', 'mower.view', 'schedule.edit'],
  created_at: '2026-10-20T10:00:00Z',
  expires_at: null,
  last_used_at: null,
  revoked_at: null,
};

beforeEach(() => {
  setLocale('de', { reload: false });
  get.mockImplementation(async (path: string) =>
    path.endsWith('/permissions') ? CATALOGUE : [TOKEN],
  );
  post.mockResolvedValue({ ...TOKEN, token: 'mdk_secret' });
});

afterEach(() => {
  get.mockReset();
  post.mockReset();
});

async function named(value: string): Promise<void> {
  await fireEvent.input(screen.getByLabelText(/Name/), { target: { value } });
}

function sentPermissions(): string[] {
  const options = post.mock.calls[0]![1] as {
    body: { grants: { level: { type: string; id: string }; permissions: string[] }[] };
  };
  expect(options.body.grants).toHaveLength(1);
  const [grant] = options.body.grants;
  expect(grant!.level).toEqual({ type: 'account', id: 'acc-1' });
  return grant!.permissions;
}

describe('ApiTokensSection', () => {
  it('lists each token with its rights as chips, powerful ones marked', async () => {
    render(ApiTokensSection);
    const row = await screen.findByTestId('token-row');
    const chips = within(row).getByTestId('token-chips');
    const keys = [...chips.querySelectorAll('[data-key]')].map((c) => c.getAttribute('data-key'));
    // In reading order, not the API's alphabetical one.
    expect(keys).toEqual([
      'mower.view',
      'mower.control',
      'schedule.edit',
      'mower.drive',
      'fleet.manage',
    ]);
    expect(chips.textContent).toContain('Fahren');
    expect(chips.querySelector('[data-key="mower.drive"]')?.classList).toContain('powerful');
    expect(chips.querySelector('[data-key="mower.view"]')?.classList).not.toContain('powerful');
    // mower.view brings four account keys along, with mower.control one more.
    expect((await within(row).findByTestId('token-included')).textContent).toContain(
      '+5 inbegriffen',
    );
  });

  it('defaults to the Home Assistant basics and explains the difference', async () => {
    render(ApiTokensSection);
    expect((screen.getByTestId('token-preset-basic') as HTMLInputElement).checked).toBe(true);
    expect(screen.getByTestId('token-presets').textContent).toContain(
      'Home Assistant – Grundfunktionen',
    );
    expect(screen.getByTestId('token-presets').textContent).toContain(
      'Home Assistant – vollständig',
    );
    expect(screen.getByTestId('token-preset-difference').textContent).toContain('Fahrtasten');
    expect(screen.queryByTestId('token-warning')).toBeNull();

    await named('HA');
    await fireEvent.click(screen.getByTestId('token-create'));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(sentPermissions()).toEqual(['mower.control', 'mower.view']);
    expect(await screen.findByTestId('token-secret')).toBeTruthy();
  });

  it('creates a full Home Assistant token and warns about its powerful rights', async () => {
    render(ApiTokensSection);
    await fireEvent.click(screen.getByTestId('token-preset-full'));
    const warning = screen.getByTestId('token-warning');
    expect(warning.textContent).toContain('mower.drive');
    expect(warning.textContent).toContain('fleet.manage');
    expect(warning.textContent).toContain('Firmware');

    await named('  Home Assistant  ');
    await fireEvent.click(screen.getByTestId('token-create'));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect((post.mock.calls[0]![1] as { body: { name: string } }).body.name).toBe('Home Assistant');
    expect(sentPermissions()).toEqual([
      'fleet.manage',
      'mower.control',
      'mower.drive',
      'mower.view',
      'schedule.edit',
    ]);
  });

  it('view only sends mower.view alone', async () => {
    render(ApiTokensSection);
    await fireEvent.click(screen.getByTestId('token-preset-view'));
    await named('Dashboard');
    await fireEvent.click(screen.getByTestId('token-create'));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(sentPermissions()).toEqual(['mower.view']);
  });

  it('custom: rights from the catalogue, implied ones included, none refused', async () => {
    render(ApiTokensSection);
    await screen.findByTestId('token-row'); // loaded
    await fireEvent.click(screen.getByTestId('token-preset-custom'));
    const custom = await screen.findByTestId('token-custom');

    // Only what a token may hold on the account; users-only keys are missing.
    expect(within(custom).queryByTestId('token-perm-users.manage')).toBeNull();
    expect(within(custom).queryByTestId('token-perm-site.only')).toBeNull();
    expect(custom.textContent).toContain('Mäher manuell fahren');
    expect(custom.textContent).toContain('vendor.thing (catalogue)');

    // Starts from the preset looked at last (basics).
    const view = screen.getByTestId('token-perm-mower.view') as HTMLInputElement;
    const control = screen.getByTestId('token-perm-mower.control') as HTMLInputElement;
    expect(control.checked).toBe(true);
    expect(view.disabled).toBe(true); // needed by mower.control
    const telemetry = screen.getByTestId('token-perm-telemetry.view') as HTMLInputElement;
    expect(telemetry.checked).toBe(true);
    expect(telemetry.disabled).toBe(true);
    expect(custom.textContent).toContain('inbegriffen über mower.view');

    // Drop everything: creating is refused.
    await fireEvent.click(control);
    expect(view.disabled).toBe(false);
    await fireEvent.click(view);
    expect(telemetry.checked).toBe(false);
    await named('Nothing');
    expect(screen.getByTestId('token-none')).toBeTruthy();
    expect((screen.getByTestId('token-create') as HTMLButtonElement).disabled).toBe(true);

    // Drive alone brings mower.view along and is flagged.
    await fireEvent.click(screen.getByTestId('token-perm-mower.drive'));
    expect(view.checked).toBe(true);
    expect(screen.getByTestId('token-warning').textContent).toContain('mower.drive');
    await fireEvent.click(screen.getByTestId('token-perm-schedule.edit'));
    await fireEvent.click(screen.getByTestId('token-create'));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(sentPermissions()).toEqual(['mower.drive', 'mower.view', 'schedule.edit']);
  });
});
