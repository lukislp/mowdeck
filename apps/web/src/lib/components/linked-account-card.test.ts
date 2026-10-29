import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import { ApiProblem, PROBLEM_PREFIX } from '#lib/api/problem.js';
import type { LinkedAccount } from '#lib/linked/accounts.js';
import { ACME, ZETA } from '#lib/linked/provider-fixtures.js';
import { m } from '#lib/paraglide/messages.js';

import LinkedAccountCard from './LinkedAccountCard.svelte';

const ACCOUNT: LinkedAccount = {
  id: 'la-1',
  provider: 'acme',
  identity: 'conn-1@fake.mowdeck.test',
  public_credentials: { email: 'conn-1@fake.mowdeck.test' },
  email: 'conn-1@fake.mowdeck.test',
  created_at: '2026-10-11T08:00:00Z',
  updated_at: '2026-10-11T08:00:00Z',
  last_synced_at: '2026-10-11T08:00:00Z',
  last_sync_error: null,
  device_count: 3,
  mower_count: 2,
};

function props(extra: Record<string, unknown> = {}) {
  return {
    account: ACCOUNT,
    provider: ACME,
    onsync: vi.fn(() => Promise.resolve()),
    onremove: vi.fn(() => Promise.resolve()),
    ...extra,
  };
}

describe('LinkedAccountCard', () => {
  it('shows identity, vendor, status, counts and the last read', () => {
    render(LinkedAccountCard, { props: props() });
    const card = screen.getByTestId('linked-account');
    expect(card.dataset.email).toBe('conn-1@fake.mowdeck.test');
    expect(card).toHaveTextContent('Acme Cloud');
    expect(screen.getByTestId('linked-account-status')).toHaveTextContent(m.provider_status_ok());
    expect(screen.queryByTestId('linked-account-untested')).toBeNull();
    expect(screen.getByTestId('linked-account-devices')).toHaveTextContent('3');
    expect(screen.getByTestId('linked-account-mowers')).toHaveTextContent('2');
    expect(screen.queryByTestId('linked-account-error')).toBeNull();
  });

  it('shows the last error prominently', () => {
    render(LinkedAccountCard, {
      props: props({ account: { ...ACCOUNT, last_sync_error: 'login refused' } }),
    });
    const error = screen.getByTestId('linked-account-error');
    expect(error).toHaveAttribute('role', 'alert');
    expect(error).toHaveTextContent(m.linked_sync_error());
    expect(error).toHaveTextContent('login refused');
    expect(screen.getByTestId('linked-account-status')).toHaveTextContent(
      m.provider_status_failing(),
    );
  });

  it('shows the non-secret fields and the untested label of the provider', () => {
    const account: LinkedAccount = {
      ...ACCOUNT,
      provider: 'zeta',
      identity: 'key-1',
      email: 'key-1',
      public_credentials: { app_key: 'key-1', region: 'us' },
    };
    render(LinkedAccountCard, { props: props({ account, provider: ZETA }) });
    expect(screen.getByTestId('linked-account')).toHaveTextContent('Zeta Connect');
    expect(screen.getByText('Region').nextElementSibling).toHaveTextContent('United States');
    expect(screen.getByTestId('linked-account-untested')).toHaveTextContent(m.provider_untested());
  });

  it('cannot read an account of a provider that is no longer enabled', () => {
    render(LinkedAccountCard, { props: props({ provider: null }) });
    expect(screen.getByTestId('linked-account')).toHaveTextContent('acme');
    expect(screen.getByTestId('linked-account-status')).toHaveTextContent(
      m.provider_status_disabled(),
    );
    expect(screen.getByTestId('linked-account-sync')).toBeDisabled();
    expect(screen.getByTestId('linked-account-delete')).not.toBeDisabled();
  });

  it('reads again, with new secrets only when they are typed', async () => {
    const p = props();
    render(LinkedAccountCard, { props: p });
    await fireEvent.click(screen.getByTestId('linked-account-sync'));
    expect(p.onsync).toHaveBeenLastCalledWith(undefined);
    await fireEvent.click(screen.getByTestId('linked-account-password-toggle'));
    const field = screen.getByLabelText('Password');
    expect(field).toHaveValue(''); // never prefilled
    expect(screen.queryByLabelText('E-mail address')).toBeNull(); // secrets only
    await fireEvent.input(field, { target: { value: 'new-secret' } });
    await fireEvent.click(screen.getByTestId('linked-account-sync'));
    await waitFor(() => expect(p.onsync).toHaveBeenLastCalledWith({ password: 'new-secret' }));
  });

  it('shows a refused new password at the field', async () => {
    const p = props({
      onsync: vi.fn(() =>
        Promise.reject(
          new ApiProblem({ type: `${PROBLEM_PREFIX}linked-account-login-failed`, status: 422 }),
        ),
      ),
    });
    render(LinkedAccountCard, { props: p });
    await fireEvent.click(screen.getByTestId('linked-account-password-toggle'));
    const field = screen.getByLabelText('Password');
    await fireEvent.input(field, { target: { value: 'wrong' } });
    await fireEvent.click(screen.getByTestId('linked-account-sync'));
    await waitFor(() => expect(field).toHaveAttribute('aria-invalid', 'true'));
    expect(field).toHaveValue('');
    expect(screen.getByText(m.linked_login_failed())).toBeInTheDocument();
  });

  it('asks before unlinking and names the mowers that still use it', async () => {
    const p = props({
      onremove: vi.fn(() =>
        Promise.reject(
          new ApiProblem({
            type: `${PROBLEM_PREFIX}linked-account-in-use`,
            status: 409,
            errors: [{ pointer: '#/mowers/m-1', message: 'Front lawn', code: 'in_use' }],
          }),
        ),
      ),
    });
    render(LinkedAccountCard, { props: p });
    await fireEvent.click(screen.getByTestId('linked-account-delete'));
    expect(p.onremove).not.toHaveBeenCalled();
    expect(screen.getByText(m.linked_delete_body())).toBeInTheDocument();
    await fireEvent.click(screen.getByTestId('linked-account-delete-confirm'));
    expect(p.onremove).toHaveBeenCalledOnce();
    const inUse = await screen.findByTestId('linked-account-in-use');
    expect(inUse).toHaveTextContent(m.linked_in_use());
    expect(screen.getByRole('link', { name: 'Front lawn' })).toHaveAttribute(
      'href',
      '/mowers/m-1/settings',
    );
    // Back to the unconfirmed button.
    expect(screen.getByTestId('linked-account-delete')).toBeInTheDocument();
  });

  it('hides the controls when read-only', () => {
    render(LinkedAccountCard, { props: props({ readonly: true }) });
    expect(screen.queryByTestId('linked-account-sync')).toBeNull();
    expect(screen.queryByTestId('linked-account-delete')).toBeNull();
  });
});
