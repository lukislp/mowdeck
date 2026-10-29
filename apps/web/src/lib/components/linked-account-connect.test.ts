import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import { ApiProblem, PROBLEM_PREFIX } from '#lib/api/problem.js';
import { ACME, ZETA } from '#lib/linked/provider-fixtures.js';
import { m } from '#lib/paraglide/messages.js';

import LinkedAccountConnect from './LinkedAccountConnect.svelte';

async function fill(email: string, password: string): Promise<void> {
  await fireEvent.input(screen.getByLabelText('E-mail address'), { target: { value: email } });
  await fireEvent.input(screen.getByLabelText('Password'), { target: { value: password } });
}

describe('LinkedAccountConnect', () => {
  it('renders the single provider without a rail, with its guidance and fields', () => {
    render(LinkedAccountConnect, { props: { providers: [ACME], onconnect: vi.fn() } });
    expect(screen.queryByRole('radiogroup')).toBeNull();
    const guide = screen.getByTestId('provider-guide');
    expect(guide).toHaveTextContent('Acme Cloud');
    expect(guide).toHaveTextContent('Share the mowers with it.');
    expect(screen.getByRole('link', { name: /Acme app/ })).toHaveAttribute(
      'href',
      'https://acme.test/app',
    );
    expect(screen.queryByTestId('provider-untested')).toBeNull();
    expect(document.getElementById('linked-email')).not.toBeNull();
    expect(document.getElementById('linked-password')).toHaveAttribute('type', 'password');
  });

  it('says so when no provider is enabled', () => {
    render(LinkedAccountConnect, { props: { providers: [], onconnect: vi.fn() } });
    expect(screen.getByTestId('linked-no-providers')).toHaveTextContent(m.provider_none());
    expect(screen.queryByTestId('linked-connect')).toBeNull();
  });

  it('switches the vendor and renders exactly its fields with the warnings', async () => {
    const onconnect = vi.fn(() => Promise.resolve());
    render(LinkedAccountConnect, { props: { providers: [ACME, ZETA], onconnect } });
    expect(screen.getByTestId('provider-option-acme')).toHaveAttribute('aria-checked', 'true');
    await fireEvent.click(screen.getByTestId('provider-option-zeta'));
    expect(screen.getByTestId('provider-untested')).toHaveTextContent(m.provider_untested());
    expect(screen.getByTestId('provider-dev-account')).toHaveTextContent(m.provider_dev_account());
    expect(screen.queryByLabelText('E-mail address')).toBeNull();
    await fireEvent.input(screen.getByLabelText('Application key'), {
      target: { value: ' key-1 ' },
    });
    await fireEvent.input(screen.getByLabelText('Application secret'), {
      target: { value: 's3cret' },
    });
    await fireEvent.change(screen.getByLabelText('Region'), { target: { value: 'us' } });
    await fireEvent.click(screen.getByTestId('linked-connect-submit'));
    await waitFor(() =>
      expect(onconnect).toHaveBeenCalledWith('zeta', {
        app_key: 'key-1',
        app_secret: 's3cret',
        region: 'us',
      }),
    );
  });

  it('moves through the vendor rail with the arrow keys', async () => {
    render(LinkedAccountConnect, { props: { providers: [ACME, ZETA], onconnect: vi.fn() } });
    const acme = screen.getByTestId('provider-option-acme');
    await fireEvent.keyDown(acme, { key: 'ArrowRight' });
    expect(screen.getByTestId('provider-option-zeta')).toHaveAttribute('aria-checked', 'true');
    await fireEvent.keyDown(screen.getByTestId('provider-option-zeta'), { key: 'ArrowRight' });
    expect(acme).toHaveAttribute('aria-checked', 'true');
  });

  it('checks the fields before sending anything', async () => {
    const onconnect = vi.fn();
    render(LinkedAccountConnect, { props: { providers: [ACME], onconnect } });
    await fireEvent.click(screen.getByTestId('linked-connect-submit'));
    expect(onconnect).not.toHaveBeenCalled();
    expect(screen.getByLabelText('E-mail address')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByLabelText('Password')).toHaveAttribute('aria-invalid', 'true');
    await waitFor(() => expect(document.activeElement?.id).toBe('linked-email'));
  });

  it('shows the calm pending state with the vendor, then clears the form', async () => {
    let finish: () => void = () => undefined;
    const onconnect = vi.fn(() => new Promise<void>((resolve) => (finish = resolve)));
    render(LinkedAccountConnect, { props: { providers: [ACME], onconnect } });
    await fill('conn-1@fake.mowdeck.test', 'fake-cloud-password');
    await fireEvent.click(screen.getByTestId('linked-connect-submit'));
    expect(onconnect).toHaveBeenCalledWith('acme', {
      email: 'conn-1@fake.mowdeck.test',
      password: 'fake-cloud-password',
    });
    expect(screen.getByTestId('linked-connect-pending')).toHaveTextContent(
      m.linked_connecting({ provider: 'Acme Cloud' }),
    );
    finish();
    await waitFor(() => expect(screen.queryByTestId('linked-connect-pending')).toBeNull());
    expect(screen.getByLabelText('Password')).toHaveValue('');
    expect(screen.getByLabelText('E-mail address')).toHaveValue('');
  });

  it('puts a refused login on the secret field and focuses it', async () => {
    const onconnect = vi.fn(() =>
      Promise.reject(
        new ApiProblem({
          type: `${PROBLEM_PREFIX}linked-account-login-failed`,
          status: 422,
          errors: [{ pointer: '#/credentials/password', message: 'refused', code: 'login_failed' }],
        }),
      ),
    );
    render(LinkedAccountConnect, { props: { providers: [ACME], onconnect } });
    await fill('conn-1@fake.mowdeck.test', 'wrong');
    await fireEvent.click(screen.getByTestId('linked-connect-submit'));
    await waitFor(() => expect(screen.getByText(m.linked_login_failed())).toBeInTheDocument());
    const password = screen.getByLabelText('Password');
    expect(password).toHaveAttribute('aria-invalid', 'true');
    expect(password).toHaveValue(''); // write-only: never kept
    expect(screen.getByLabelText('E-mail address')).toHaveValue('conn-1@fake.mowdeck.test');
    await waitFor(() => expect(document.activeElement).toBe(password));
  });

  it('shows an unreachable cloud above the form with Fleet detail', async () => {
    const onconnect = vi.fn(() =>
      Promise.reject(
        new ApiProblem({ status: 503, detail: 'no encryption key configured', type: 'x' }),
      ),
    );
    render(LinkedAccountConnect, { props: { providers: [ACME], onconnect } });
    await fill('conn-1@fake.mowdeck.test', 'secret');
    await fireEvent.click(screen.getByTestId('linked-connect-submit'));
    const notice = await screen.findByTestId('linked-connect-problem');
    expect(notice).toHaveTextContent(m.linked_unavailable());
    expect(notice).toHaveTextContent('no encryption key configured');
  });

  it('marks an account linked already on the identity', async () => {
    const onconnect = vi.fn(() => Promise.reject(new ApiProblem({ status: 409 })));
    const oncancel = vi.fn();
    render(LinkedAccountConnect, { props: { providers: [ACME], onconnect, oncancel } });
    await fill('conn-1@fake.mowdeck.test', 'secret');
    await fireEvent.click(screen.getByTestId('linked-connect-submit'));
    await waitFor(() =>
      expect(screen.getByLabelText('E-mail address')).toHaveAttribute('aria-invalid', 'true'),
    );
    expect(screen.getByText(m.linked_already_linked())).toBeInTheDocument();
    await fireEvent.click(screen.getByText(m.cancel()));
    expect(oncancel).toHaveBeenCalledOnce();
  });
});
