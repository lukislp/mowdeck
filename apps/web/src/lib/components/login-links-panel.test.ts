import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const get = vi.fn();
const post = vi.fn();
const del = vi.fn();
const enabled = vi.hoisted(() => ({ ids: ['github', 'apple'] as string[] }));
vi.mock('#lib/api/client.js', () => ({
  identityApi: {
    GET: (...a: unknown[]) => get(...a),
    POST: (...a: unknown[]) => post(...a),
    DELETE: (...a: unknown[]) => del(...a),
  },
  unwrap: async (p: Promise<unknown>) => p,
}));
vi.mock('$app/state', () => ({
  page: { url: new URL('https://mowdeck.test/settings#profile'), state: {} },
}));
vi.mock('$app/navigation', () => ({ replaceState: vi.fn() }));
vi.mock('#lib/auth/oauth.js', async (original) => ({
  ...(await original<typeof import('#lib/auth/oauth.js')>()),
  providerEnabled: async (id: string) => enabled.ids.includes(id),
  linkProvider: async (start: () => Promise<unknown>) => {
    await start();
  },
}));

import { setLocale } from '#lib/paraglide/runtime.js';

import LoginLinksPanel from './LoginLinksPanel.svelte';

const GITHUB = {
  provider: 'github',
  display_name: 'octocat',
  linked_at: '2026-10-25T09:00:00Z',
  last_used_at: null,
};
const APPLE = {
  provider: 'apple',
  display_name: 'Apple ID',
  linked_at: '2026-10-25T09:00:00Z',
  last_used_at: null,
};

let links: unknown[];

beforeEach(() => {
  setLocale('de', { reload: false });
  enabled.ids = ['github', 'apple'];
  links = [];
  get.mockImplementation(async () => links);
  post.mockResolvedValue({ authorize_url: 'https://example.test/authorize' });
  del.mockImplementation(async () => {
    links = [];
  });
});

afterEach(() => {
  get.mockReset();
  post.mockReset();
  del.mockReset();
});

function text(element: HTMLElement): string {
  return (element.textContent ?? '').replace(/\s+/g, ' ').trim();
}

describe('LoginLinksPanel', () => {
  it('is hidden when no provider is configured and none is linked', async () => {
    enabled.ids = [];
    const { container } = render(LoginLinksPanel);
    await waitFor(() => expect(get).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByTestId('login-links-panel')).toBeNull();
    expect(text(container)).toBe('');
  });

  it('offers both providers as buttons side by side, with the accessible names', async () => {
    render(LoginLinksPanel);
    const github = await screen.findByRole('button', { name: 'Mit GitHub verknüpfen' });
    const apple = screen.getByRole('button', { name: 'Mit Apple verknüpfen' });
    expect(text(github)).toBe('GitHub');
    expect(text(apple)).toBe('Apple');
    expect(github.parentElement).toBe(apple.parentElement);
    expect(screen.queryByTestId('github-linked')).toBeNull();
    // Nothing of the installation's provider configuration shows.
    expect(text(screen.getByTestId('login-links-panel'))).not.toMatch(
      /client|callback|oauth|secret/i,
    );
  });

  it('offers only the configured provider', async () => {
    enabled.ids = ['apple'];
    render(LoginLinksPanel);
    await screen.findByRole('button', { name: 'Mit Apple verknüpfen' });
    expect(screen.queryByRole('button', { name: 'Mit GitHub verknüpfen' })).toBeNull();
  });

  it('lists a linked provider and keeps the button only for the other one', async () => {
    links = [GITHUB];
    render(LoginLinksPanel);
    const row = await screen.findByTestId('github-linked');
    expect(text(row)).toContain('GitHub · octocat');
    expect(text(row)).toMatch(/Verknüpft .*2026/);
    expect(screen.queryByRole('button', { name: 'Mit GitHub verknüpfen' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Mit Apple verknüpfen' })).toBeTruthy();
  });

  it('shows Apple without an account name and no button row when all are linked', async () => {
    links = [GITHUB, APPLE];
    render(LoginLinksPanel);
    const row = await screen.findByTestId('apple-linked');
    expect(text(row)).toContain('Apple');
    expect(text(row)).not.toContain('Apple ID');
    expect(screen.queryByRole('group', { name: 'Konto verknüpfen' })).toBeNull();
    expect(screen.queryByTestId('github-link')).toBeNull();
  });

  it('still lists a link whose provider is no longer offered', async () => {
    enabled.ids = [];
    links = [GITHUB];
    render(LoginLinksPanel);
    expect(await screen.findByTestId('github-linked')).toBeTruthy();
    expect(screen.queryByTestId('apple-link')).toBeNull();
  });

  it('asks for the password again, then starts the link', async () => {
    render(LoginLinksPanel);
    await fireEvent.click(await screen.findByTestId('apple-link'));
    const form = screen.getByTestId('apple-link-form');
    const continueButton = within(form).getByTestId('apple-link-continue');
    expect((continueButton as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.input(form.querySelector('#apple-password') as HTMLInputElement, {
      target: { value: 'secret-pass' },
    });
    await fireEvent.click(continueButton);
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(post.mock.calls[0]?.[1]).toMatchObject({
      params: { path: { provider: 'apple' } },
      body: { password: 'secret-pass' },
    });
  });

  it('points to GitHub for the authorization MowDeck cannot remove when unlinking', async () => {
    links = [GITHUB];
    render(LoginLinksPanel);
    await fireEvent.click(await screen.findByRole('button', { name: 'Verknüpfung lösen' }));

    const hint = screen.getByTestId('github-unlink-revoke-hint');
    expect(text(hint)).toBe(
      'Die Freigabe bei GitHub kannst du unter Einstellungen → Applications entfernen.',
    );
    const anchor = within(hint).getByRole('link', { name: 'Einstellungen → Applications' });
    expect(anchor.getAttribute('href')).toBe('https://github.com/settings/applications');
    expect(anchor.getAttribute('target')).toBe('_blank');
    expect(anchor.getAttribute('rel')).toBe('noopener noreferrer');

    await fireEvent.click(screen.getByTestId('github-unlink-confirm'));
    await waitFor(() => expect(screen.queryByTestId('github-linked')).toBeNull());
    expect(del).toHaveBeenCalledOnce();
    // The result keeps saying it after the toast is gone; the button is back.
    expect(screen.getByTestId('github-revoke-hint')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Mit GitHub verknüpfen' })).toBeTruthy();
  });

  it('names where the authorization at Apple is removed', async () => {
    links = [APPLE];
    render(LoginLinksPanel);
    await fireEvent.click(await screen.findByTestId('apple-unlink'));
    const hint = screen.getByTestId('apple-unlink-revoke-hint');
    expect(text(hint)).toContain('Anmeldung und Sicherheit → Mit Apple anmelden');
    expect(within(hint).queryByRole('link')).toBeNull();
  });

  it('speaks English as well', async () => {
    setLocale('en', { reload: false });
    links = [GITHUB];
    render(LoginLinksPanel);
    expect(await screen.findByText('Linked accounts')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Link with Apple' })).toBeTruthy();
    await fireEvent.click(screen.getByTestId('github-unlink'));
    expect(text(screen.getByTestId('github-unlink-revoke-hint'))).toBe(
      'You can remove the authorization on GitHub under Settings → Applications.',
    );
  });
});
