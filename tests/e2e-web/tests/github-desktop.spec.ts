/**
 * Sign in with GitHub (ADR 0051), desktop Chromium against the test stack's fake GitHub
 * (tests/e2e-web/fake-github, only in docker-compose.e2e.yml): the browser goes to its
 * authorization page at /fake-github and picks the GitHub account there, Identity redeems the
 * code with the PKCE verifier and reads the account inside the network.
 *
 * 1. Settings, "Verknüpfte Konten": "Mit GitHub verknüpfen" (password again), approve at GitHub, the account is listed.
 * 2. Log out, "Mit GitHub anmelden" signs in to the same user.
 * 3. Settings: "Verknüpfung lösen"; log out, GitHub no longer signs in (the account is unknown,
 *    never registered); the password still works.
 *
 * Every GitHub callback and the password check count against the per-address login limit; the
 * test stack loosens it (docker-compose.e2e.yml), so the test need not wait for it to refill.
 */
import { expect, test, type Page } from '@playwright/test';

import { actor, loginByUi, RUN, watchCsp } from './support';

async function logout(page: Page): Promise<void> {
  await page.goto('/settings#profile');
  await page.getByTestId('logout').click();
  await page.waitForURL(/\/login/);
}

/** At the fake GitHub: approve as the account `id` / `login`. */
async function approveAtGitHub(page: Page, id: number, login: string): Promise<void> {
  await page.waitForURL(/\/fake-github\/login\/oauth\/authorize\?/);
  const url = new URL(page.url());
  // What MowDeck asked GitHub for: the minimal scope and a PKCE S256 challenge.
  expect(url.searchParams.get('scope')).toBe('read:user');
  expect(url.searchParams.get('code_challenge_method')).toBe('S256');
  expect(url.searchParams.get('redirect_uri')).toMatch(
    /\/api\/v1\/identity\/oauth\/github\/callback$/,
  );
  await page.getByTestId('fake-github-id').fill(String(id));
  await page.getByTestId('fake-github-login').fill(login);
  await page.getByTestId('fake-github-authorize').click();
}

test('GitHub: link, sign in with GitHub, unlink, refused afterwards', async ({ page, request }) => {
  const csp = watchCsp(page);
  const methods = await request.get('/api/v1/identity/login-methods');
  // Only a flag: nothing of the GitHub settings is published.
  expect(await methods.json()).toEqual({
    password: true,
    passkey: true,
    github: true,
    apple: true,
  });
  const owner = actor('github');
  const registered = await request.post('/api/v1/identity/register', {
    data: { username: owner.username, password: owner.password, account_name: 'GitHub home' },
  });
  expect(registered.status(), await registered.text()).toBe(201);
  // A GitHub account of this run only (the fake accepts any numeric id).
  const githubId = 700_000_000 + Math.floor(Math.random() * 99_999_999);
  const githubLogin = `octo-${RUN}`;

  // 1. Link in the settings.
  await loginByUi(page, owner);
  await page.goto('/settings#profile');
  const panel = page.getByTestId('login-links-panel');
  // Not linked yet: no account in the list, a connect button in the row.
  await expect(panel.getByTestId('github-linked')).toHaveCount(0);
  await expect(panel.getByRole('button', { name: /^Mit GitHub verknüpfen$/ })).toBeVisible();
  await panel.getByTestId('github-link').click();
  await panel.locator('#github-password').fill(owner.password);
  await panel.getByTestId('github-link-continue').click();
  await approveAtGitHub(page, githubId, githubLogin);
  await page.waitForURL(/\/settings/);
  const linked = page.getByTestId('github-linked');
  await expect(linked).toContainText(githubLogin);
  // The result parameters are gone from the address (a reload does not repeat the toast).
  expect(new URL(page.url()).searchParams.get('linked')).toBeNull();

  // 2. Sign in with GitHub.
  await logout(page);
  await page.getByTestId('login-github').click();
  await approveAtGitHub(page, githubId, githubLogin);
  await page.waitForURL(/\/mowers/);

  // 3. Unlink; GitHub no longer signs in, the password does.
  await page.goto('/settings#profile');
  await page.getByTestId('github-unlink').click();
  // MowDeck keeps no GitHub token, so the authorization on GitHub's side is the user's to remove.
  await expect(page.getByTestId('github-unlink-revoke-hint')).toBeVisible();
  await page.getByTestId('github-unlink-confirm').click();
  await expect(page.getByTestId('github-linked')).toHaveCount(0);
  await expect(page.getByTestId('github-link')).toBeVisible();
  await expect(
    page
      .getByTestId('github-revoke-hint')
      .getByRole('link', { name: 'Einstellungen → Applications' }),
  ).toHaveAttribute('href', 'https://github.com/settings/applications');
  await logout(page);
  await page.getByTestId('login-github').click();
  await approveAtGitHub(page, githubId, githubLogin);
  // Back on the sign-in page (the fake's own address contains /login as well).
  await page.waitForURL((url) => url.pathname === '/login');
  await expect(
    page.getByText('Anmeldung mit GitHub nicht möglich.', { exact: false }),
  ).toBeVisible();
  expect(new URL(page.url()).searchParams.get('sign_in_error')).toBeNull();
  await loginByUi(page, owner);
  csp.assertNone();
});
