import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import { ApiProblem, PROBLEM_PREFIX } from '$lib/api/problem';
import { m } from '$lib/paraglide/messages';

import LinkedAccountConnect from './LinkedAccountConnect.svelte';

async function fill(email: string, password: string): Promise<void> {
  await fireEvent.input(screen.getByLabelText(m.linked_email()), { target: { value: email } });
  await fireEvent.input(screen.getByLabelText(m.linked_password()), {
    target: { value: password },
  });
}

describe('LinkedAccountConnect', () => {
  it('checks e-mail and password before sending anything', async () => {
    const onconnect = vi.fn();
    render(LinkedAccountConnect, { props: { onconnect } });
    await fireEvent.click(screen.getByTestId('linked-connect-submit'));
    expect(onconnect).not.toHaveBeenCalled();
    expect(screen.getByLabelText(m.linked_email())).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByLabelText(m.linked_password())).toHaveAttribute('aria-invalid', 'true');
    await waitFor(() => expect(document.activeElement?.id).toBe('linked-email'));
  });

  it('shows the calm pending state while Mammotion logs in, then clears the password', async () => {
    let finish: () => void = () => undefined;
    const onconnect = vi.fn(() => new Promise<void>((resolve) => (finish = resolve)));
    render(LinkedAccountConnect, { props: { onconnect } });
    await fill('conn-1@fake.mowdeck.test', 'fake-cloud-password');
    await fireEvent.click(screen.getByTestId('linked-connect-submit'));
    expect(onconnect).toHaveBeenCalledWith('conn-1@fake.mowdeck.test', 'fake-cloud-password');
    expect(screen.getByTestId('linked-connect-pending')).toHaveTextContent(m.linked_connecting());
    finish();
    await waitFor(() => expect(screen.queryByTestId('linked-connect-pending')).toBeNull());
    expect(screen.getByLabelText(m.linked_password())).toHaveValue('');
    expect(screen.getByLabelText(m.linked_email())).toHaveValue('');
  });

  it('puts a refused login on the password field and focuses it', async () => {
    const onconnect = vi.fn(() =>
      Promise.reject(
        new ApiProblem({
          type: `${PROBLEM_PREFIX}linked-account-login-failed`,
          status: 422,
          errors: [{ pointer: '#/password', message: 'refused', code: 'login_failed' }],
        }),
      ),
    );
    render(LinkedAccountConnect, { props: { onconnect } });
    await fill('conn-1@fake.mowdeck.test', 'wrong');
    await fireEvent.click(screen.getByTestId('linked-connect-submit'));
    await waitFor(() => expect(screen.getByText(m.linked_login_failed())).toBeInTheDocument());
    const password = screen.getByLabelText(m.linked_password());
    expect(password).toHaveAttribute('aria-invalid', 'true');
    expect(password).toHaveValue(''); // write-only: never kept
    expect(screen.getByLabelText(m.linked_email())).toHaveValue('conn-1@fake.mowdeck.test');
    await waitFor(() => expect(document.activeElement).toBe(password));
  });

  it('shows an unreachable cloud above the form with Fleet detail', async () => {
    const onconnect = vi.fn(() =>
      Promise.reject(
        new ApiProblem({ status: 503, detail: 'no encryption key configured', type: 'x' }),
      ),
    );
    render(LinkedAccountConnect, { props: { onconnect } });
    await fill('conn-1@fake.mowdeck.test', 'secret');
    await fireEvent.click(screen.getByTestId('linked-connect-submit'));
    const notice = await screen.findByTestId('linked-connect-problem');
    expect(notice).toHaveTextContent(m.linked_unavailable());
    expect(notice).toHaveTextContent('no encryption key configured');
  });

  it('marks an account linked already on the e-mail', async () => {
    const onconnect = vi.fn(() => Promise.reject(new ApiProblem({ status: 409 })));
    const oncancel = vi.fn();
    render(LinkedAccountConnect, { props: { onconnect, oncancel } });
    await fill('conn-1@fake.mowdeck.test', 'secret');
    await fireEvent.click(screen.getByTestId('linked-connect-submit'));
    await waitFor(() =>
      expect(screen.getByLabelText(m.linked_email())).toHaveAttribute('aria-invalid', 'true'),
    );
    expect(screen.getByText(m.linked_already_linked())).toBeInTheDocument();
    await fireEvent.click(screen.getByText(m.cancel()));
    expect(oncancel).toHaveBeenCalledOnce();
  });
});
