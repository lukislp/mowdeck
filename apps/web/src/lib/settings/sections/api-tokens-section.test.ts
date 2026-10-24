import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const get = vi.fn();
const post = vi.fn();
const del = vi.fn();
vi.mock('#lib/api/client.js', () => ({
  identityApi: {
    GET: (...a: unknown[]) => get(...a),
    POST: (...a: unknown[]) => post(...a),
    DELETE: (...a: unknown[]) => del(...a),
  },
  unwrap: async (p: Promise<unknown>) => p,
}));
vi.mock('#lib/auth/session.svelte.js', () => ({ session: { me: { account_id: 'acc-1' } } }));

import { ApiProblem } from '#lib/api/problem.js';
import { setLocale } from '#lib/paraglide/runtime.js';
import { CATALOGUE } from '#lib/settings/token-permissions.fixture.js';

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
  del.mockReset();
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

  it('shows the new secret on top, focused and described, and copies it', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    render(ApiTokensSection);
    await screen.findByTestId('token-row');
    await named('Home Assistant');
    await fireEvent.click(screen.getByTestId('token-create'));

    const secret = await screen.findByTestId('token-secret');
    // Above the list, so it is the first thing in the section.
    const list = screen.getByTestId('token-list');
    expect(secret.compareDocumentPosition(list) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(secret));
    expect(secret.getAttribute('tabindex')).toBe('-1');
    const title = document.getElementById(secret.getAttribute('aria-labelledby')!);
    expect(title?.textContent).toContain('Neues Token');
    expect(title?.textContent).toContain('Home Assistant');
    const note = document.getElementById(secret.getAttribute('aria-describedby')!);
    expect(note?.textContent).toContain('nur jetzt angezeigt');
    expect(screen.getByTestId('token-secret-value').textContent).toBe('mdk_secret');

    await fireEvent.click(screen.getByTestId('token-secret-copy'));
    expect(writeText).toHaveBeenCalledWith('mdk_secret');
    await waitFor(() =>
      expect(screen.getByTestId('token-secret-status').textContent).toContain('Kopiert'),
    );
    expect(screen.getByTestId('token-secret-status').getAttribute('role')).toBe('status');

    // Stays until dismissed; then the focus goes back to the form.
    await fireEvent.click(screen.getByTestId('token-secret-dismiss'));
    expect(screen.queryByTestId('token-secret')).toBeNull();
    expect(document.activeElement?.id).toBe('token-name');
    Reflect.deleteProperty(navigator, 'clipboard');
  });

  it('without a clipboard says how to copy the secret by hand', async () => {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true });
    render(ApiTokensSection);
    await named('LAN');
    await fireEvent.click(screen.getByTestId('token-create'));
    await screen.findByTestId('token-secret');
    await fireEvent.click(screen.getByTestId('token-secret-copy'));
    const status = screen.getByTestId('token-secret-status');
    await waitFor(() => expect(status.textContent).toContain('Strg+C'));
    expect(status.classList).not.toContain('md-visually-hidden');
    Reflect.deleteProperty(navigator, 'clipboard');
  });

  it('lists active tokens first and folds revoked ones into a closed disclosure', async () => {
    const revokedToken = (id: string, name: string, at: string) => ({
      ...TOKEN,
      id,
      name,
      revoked_at: at,
    });
    get.mockImplementation(async (path: string) =>
      path.endsWith('/permissions')
        ? CATALOGUE
        : [
            revokedToken('r1', 'Altes Tablet', '2026-10-21T08:00:00Z'),
            TOKEN,
            revokedToken('r2', 'Test', '2026-10-22T08:00:00Z'),
            { ...TOKEN, id: 't2', name: 'Neuer', created_at: '2026-10-23T10:00:00Z' },
          ],
    );
    render(ApiTokensSection);
    const list = await screen.findByTestId('token-list');
    expect(
      within(list)
        .getAllByTestId('token-row')
        .map((r) => r.dataset.name),
    ).toEqual(['Neuer', 'Home Assistant']);
    expect(within(list).getAllByRole('button', { name: 'Widerrufen' })).toHaveLength(2);

    const folded = screen.getByTestId('token-revoked') as HTMLDetailsElement;
    expect(folded.tagName).toBe('DETAILS');
    expect(folded.open).toBe(false);
    expect(folded.querySelector('summary')?.textContent).toContain('Widerrufene Tokens (2)');
    const rows = within(folded).getAllByTestId('token-revoked-row');
    // Newest revocation first, no revoke button (only removal), no chips.
    expect(rows.map((r) => r.dataset.name)).toEqual(['Test', 'Altes Tablet']);
    expect(within(folded).queryByRole('button', { name: 'Widerrufen' })).toBeNull();
    expect(within(folded).queryByTestId('token-chips')).toBeNull();
  });

  it('says so when every token is revoked', async () => {
    get.mockImplementation(async (path: string) =>
      path.endsWith('/permissions')
        ? CATALOGUE
        : [{ ...TOKEN, revoked_at: '2026-10-21T08:00:00Z' }],
    );
    render(ApiTokensSection);
    expect((await screen.findByTestId('token-no-active')).textContent).toContain(
      'Keine aktiven API-Tokens',
    );
    expect(screen.queryByTestId('token-list')).toBeNull();
    expect(screen.getByTestId('token-revoked').textContent).toContain('Widerrufene Tokens (1)');
  });

  describe('removing revoked tokens', () => {
    const revokedToken = (id: string, name: string, at: string) => ({
      ...TOKEN,
      id,
      name,
      prefix: `mdk_${id}`,
      revoked_at: at,
    });
    const LIST = [
      TOKEN,
      revokedToken('r1', 'Altes Tablet', '2026-10-21T08:00:00Z'),
      revokedToken('r2', 'Testskript', '2026-10-22T08:00:00Z'),
    ];

    function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void } {
      let resolve!: (v: T) => void;
      const promise = new Promise<T>((r) => (resolve = r));
      return { promise, resolve };
    }

    function revokedNames(): string[] {
      return screen.queryAllByTestId('token-revoked-row').map((r) => r.dataset.name ?? '');
    }

    beforeEach(() => {
      get.mockImplementation(async (path: string) =>
        path.endsWith('/permissions') ? CATALOGUE : LIST,
      );
    });

    it('removes one revoked token at once and asks Identity to purge it', async () => {
      const answer = deferred<undefined>();
      del.mockReturnValue(answer.promise);
      render(ApiTokensSection);
      await screen.findByTestId('token-revoked');
      await fireEvent.click(
        screen.getByRole('button', { name: '„Altes Tablet“ endgültig entfernen' }),
      );

      // Gone before Identity answers.
      expect(revokedNames()).toEqual(['Testskript']);
      expect(screen.getByTestId('token-revoked').textContent).toContain('Widerrufene Tokens (1)');
      expect(del).toHaveBeenCalledWith(
        '/api/v1/identity/accounts/{account_id}/api-tokens/{token_id}',
        { params: { path: { account_id: 'acc-1', token_id: 'r1' }, query: { purge: true } } },
      );
      answer.resolve(undefined);
      await waitFor(() =>
        expect(screen.getByTestId('token-purge-status').textContent).toContain(
          '„Altes Tablet“ entfernt.',
        ),
      );
      // The active token is untouched, and no list reload was needed.
      expect(screen.getAllByTestId('token-row').map((r) => r.dataset.name)).toEqual([
        'Home Assistant',
      ]);
      expect(get.mock.calls.filter(([path]) => String(path).endsWith('/api-tokens'))).toHaveLength(
        1,
      );
    });

    it('puts a token back and shows the problem when the removal fails', async () => {
      del.mockRejectedValue(
        new ApiProblem({ status: 503, title: 'Service Unavailable', detail: 'OpenFGA is down' }),
      );
      render(ApiTokensSection);
      await screen.findByTestId('token-revoked');
      await fireEvent.click(
        screen.getByRole('button', { name: '„Testskript“ endgültig entfernen' }),
      );
      await waitFor(() => expect(screen.getByText('OpenFGA is down')).toBeTruthy());
      expect(revokedNames()).toEqual(['Testskript', 'Altes Tablet']);
      expect(screen.getByTestId('token-purge-status').textContent).toBe('');
    });

    it('treats a token that is already gone as removed', async () => {
      del.mockRejectedValue(new ApiProblem({ status: 404, title: 'Not Found' }));
      render(ApiTokensSection);
      await screen.findByTestId('token-revoked');
      await fireEvent.click(
        screen.getByRole('button', { name: '„Testskript“ endgültig entfernen' }),
      );
      await waitFor(() =>
        expect(screen.getByTestId('token-purge-status').textContent).toContain('entfernt'),
      );
      expect(revokedNames()).toEqual(['Altes Tablet']);
      expect(screen.queryByText('Not Found')).toBeNull();
    });

    it('removes all revoked tokens after a confirmation naming the count', async () => {
      const answer = deferred<{ deleted: number; ids: string[] }>();
      post.mockReturnValue(answer.promise);
      render(ApiTokensSection);
      await screen.findByTestId('token-revoked');
      await fireEvent.click(screen.getByTestId('token-purge-all'));
      expect(screen.getByTestId('token-purge-all').textContent).toContain('Widerrufene entfernen');

      const sheet = screen.getByTestId('token-purge-confirm');
      expect(sheet.getAttribute('aria-label')).toBe('2 widerrufene Tokens entfernen?');
      expect(sheet.textContent).toContain('nicht rückgängig');
      expect(within(sheet).getByTestId('token-purge-names').textContent).toContain('Altes Tablet');
      expect(within(sheet).getByTestId('token-purge-names').textContent).toContain('Testskript');

      // Cancel first: nothing happens.
      await fireEvent.click(within(sheet).getByRole('button', { name: 'Abbrechen' }));
      expect(screen.queryByTestId('token-purge-confirm')).toBeNull();
      expect(post).not.toHaveBeenCalled();

      await fireEvent.click(screen.getByTestId('token-purge-all'));
      await fireEvent.click(screen.getByTestId('token-purge-go'));
      // The whole group is gone at once; the active token stays.
      expect(screen.queryByTestId('token-revoked')).toBeNull();
      expect(screen.queryByTestId('token-purge-confirm')).toBeNull();
      expect(screen.getAllByTestId('token-row')).toHaveLength(1);
      expect(post).toHaveBeenCalledWith(
        '/api/v1/identity/accounts/{account_id}/api-tokens/purge-revoked',
        { params: { path: { account_id: 'acc-1' } } },
      );
      answer.resolve({ deleted: 2, ids: ['r1', 'r2'] });
      await waitFor(() =>
        expect(screen.getByTestId('token-purge-status').textContent).toContain(
          '2 widerrufene Tokens entfernt.',
        ),
      );
    });

    it('brings the revoked tokens back when the bulk removal fails', async () => {
      post.mockRejectedValue(
        new ApiProblem({ status: 403, title: 'Forbidden', detail: 'users.manage needed' }),
      );
      render(ApiTokensSection);
      await screen.findByTestId('token-revoked');
      await fireEvent.click(screen.getByTestId('token-purge-all'));
      await fireEvent.click(screen.getByTestId('token-purge-go'));
      await waitFor(() => expect(screen.getByText('users.manage needed')).toBeTruthy());
      expect(screen.getByTestId('token-revoked').textContent).toContain('Widerrufene Tokens (2)');
      expect(revokedNames().sort()).toEqual(['Altes Tablet', 'Testskript']);
    });

    it('reloads when Identity removed more than the list showed', async () => {
      post.mockResolvedValue({ deleted: 3, ids: ['r1', 'r2', 'r3'] });
      render(ApiTokensSection);
      await screen.findByTestId('token-revoked');
      await fireEvent.click(screen.getByTestId('token-purge-all'));
      await fireEvent.click(screen.getByTestId('token-purge-go'));
      await waitFor(() =>
        expect(
          get.mock.calls.filter(([path]) => String(path).endsWith('/api-tokens')),
        ).toHaveLength(2),
      );
    });

    it('speaks English, with the singular for one token', async () => {
      setLocale('en', { reload: false });
      get.mockImplementation(async (path: string) =>
        path.endsWith('/permissions') ? CATALOGUE : [TOKEN, LIST[1]],
      );
      post.mockResolvedValue({ deleted: 1, ids: ['r1'] });
      render(ApiTokensSection);
      await screen.findByTestId('token-revoked');
      expect(screen.getByTestId('token-purge-all').textContent).toContain('Remove revoked');
      expect(screen.getByRole('button', { name: 'Remove “Altes Tablet” for good' })).toBeTruthy();
      await fireEvent.click(screen.getByTestId('token-purge-all'));
      expect(screen.getByTestId('token-purge-confirm').getAttribute('aria-label')).toBe(
        'Remove 1 revoked token?',
      );
      await fireEvent.click(screen.getByTestId('token-purge-go'));
      await waitFor(() =>
        expect(screen.getByTestId('token-purge-status').textContent).toContain(
          'Removed 1 revoked token.',
        ),
      );
    });
  });
});
