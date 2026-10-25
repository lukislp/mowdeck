import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const get = vi.fn();
const del = vi.fn();
vi.mock('#lib/api/client.js', () => ({
  identityApi: {
    GET: (...a: unknown[]) => get(...a),
    POST: vi.fn(),
    DELETE: (...a: unknown[]) => del(...a),
  },
  unwrap: async (p: Promise<unknown>) => p,
}));
vi.mock('$app/state', () => ({
  page: { url: new URL('https://mowdeck.test/settings#profile'), state: {} },
}));
vi.mock('$app/navigation', () => ({ replaceState: vi.fn() }));
vi.mock('#lib/auth/github.js', async (original) => ({
  ...(await original<typeof import('#lib/auth/github.js')>()),
  githubEnabled: async () => true,
}));

import { setLocale } from '#lib/paraglide/runtime.js';

import GitHubPanel from './GitHubPanel.svelte';

const LINK = {
  provider: 'github',
  display_name: 'octocat',
  linked_at: '2026-10-25T09:00:00Z',
  last_used_at: null,
};

beforeEach(() => {
  setLocale('de', { reload: false });
  let links = [LINK];
  get.mockImplementation(async () => links);
  del.mockImplementation(async () => {
    links = [];
  });
});

afterEach(() => {
  get.mockReset();
  del.mockReset();
});

function text(element: HTMLElement): string {
  return (element.textContent ?? '').replace(/\s+/g, ' ').trim();
}

function expectGitHubApplicationsLink(hint: HTMLElement): void {
  expect(text(hint)).toBe(
    'Die Freigabe bei GitHub kannst du unter Einstellungen → Applications entfernen.',
  );
  const anchor = within(hint).getByRole('link', { name: 'Einstellungen → Applications' });
  expect(anchor.getAttribute('href')).toBe('https://github.com/settings/applications');
  expect(anchor.getAttribute('target')).toBe('_blank');
  expect(anchor.getAttribute('rel')).toBe('noopener noreferrer');
}

describe('GitHubPanel', () => {
  it('points to GitHub for the authorization MowDeck cannot remove when unlinking', async () => {
    render(GitHubPanel);
    await fireEvent.click(await screen.findByTestId('github-unlink'));

    // The confirmation already says that GitHub keeps its authorization.
    expectGitHubApplicationsLink(screen.getByTestId('github-unlink-revoke-hint'));

    await fireEvent.click(screen.getByTestId('github-unlink-confirm'));
    await waitFor(() => expect(screen.getByTestId('github-not-linked')).toBeTruthy());
    expect(del).toHaveBeenCalledOnce();
    // And the result keeps saying it after the toast is gone.
    expectGitHubApplicationsLink(screen.getByTestId('github-revoke-hint'));
  });

  it('shows no hint without an unlink and never the installation’s GitHub settings', async () => {
    get.mockImplementation(async () => []);
    const { container } = render(GitHubPanel);
    await screen.findByTestId('github-not-linked');
    expect(screen.queryByTestId('github-revoke-hint')).toBeNull();
    expect(container.textContent ?? '').not.toMatch(/client|callback|oauth/i);
  });

  it('speaks English as well', async () => {
    setLocale('en', { reload: false });
    render(GitHubPanel);
    await fireEvent.click(await screen.findByTestId('github-unlink'));
    const hint = screen.getByTestId('github-unlink-revoke-hint');
    expect(text(hint)).toBe(
      'You can remove the authorization on GitHub under Settings → Applications.',
    );
    expect(within(hint).getByRole('link').getAttribute('href')).toBe(
      'https://github.com/settings/applications',
    );
  });
});
