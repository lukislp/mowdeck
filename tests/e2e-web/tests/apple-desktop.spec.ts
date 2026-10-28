/**
 * Sign in with Apple (ADR 0052), desktop Chromium against the test stack's fake Apple
 * (tests/e2e-web/fake-apple, only in docker-compose.e2e.yml): the browser goes to its
 * authorization page at /fake-apple and picks the Apple account (`sub`) there, Identity
 * redeems the code with an ES256 client secret signed with the stack's key and validates the
 * RS256 ID token against the key set inside the network.
 *
 * 1. Settings: "Mit Apple verknüpfen" (password again), approve at Apple, the account is listed
 *    with a neutral label.
 * 2. Log out, "Mit Apple anmelden" signs in to the same user.
 * 3. Settings: "Verknüpfung lösen"; log out, Apple no longer signs in (the account is unknown,
 *    never registered); the password still works.
 *
 * Every callback and the password check count against the per-address login limit; the test
 * stack loosens it (docker-compose.e2e.yml), so the test need not wait for it to refill.
 */
import { expect, test, type Page } from '@playwright/test';

import { actor, loginByUi, RUN, watchCsp } from './support';

async function logout(page: Page): Promise<void> {
  await page.goto('/settings#profile');
  await page.getByTestId('logout').click();
  await page.waitForURL(/\/login/);
}

/** At the fake Apple: approve as the account `sub`. */
async function approveAtApple(page: Page, sub: string): Promise<void> {
  await page.waitForURL(/\/fake-apple\/auth\/authorize\?/);
  const url = new URL(page.url());
  // What MowDeck asked Apple for: a redirect (query) response, no scope (neither name nor
  // e-mail), a nonce and the PKCE challenge.
  expect(url.searchParams.get('response_mode')).toBe('query');
  expect(url.searchParams.has('scope')).toBe(false);
  expect(url.searchParams.get('nonce')).toBeTruthy();
  expect(url.searchParams.get('code_challenge_method')).toBe('S256');
  expect(url.searchParams.get('redirect_uri')).toMatch(
    /\/api\/v1\/identity\/oauth\/apple\/callback$/,
  );
  await page.getByTestId('fake-apple-sub').fill(sub);
  await page.getByTestId('fake-apple-authorize').click();
}

test('Apple: link, sign in with Apple, unlink, refused afterwards', async ({ page, request }) => {
  const csp = watchCsp(page);
  const owner = actor('apple');
  const registered = await request.post('/api/v1/identity/register', {
    data: { username: owner.username, password: owner.password, account_name: 'Apple home' },
  });
  expect(registered.status(), await registered.text()).toBe(201);
  // An Apple account of this run only (the fake accepts any subject).
  const sub = `001${Math.floor(Math.random() * 999_999)}.${RUN}.0420`;

  // 1. Link in the settings.
  await loginByUi(page, owner);
  await page.goto('/settings#profile');
  const panel = page.getByTestId('login-links-panel');
  // Not linked yet: no account in the list, a connect button in the row.
  await expect(panel.getByTestId('apple-linked')).toHaveCount(0);
  await expect(panel.getByRole('button', { name: /^Mit Apple verknüpfen$/ })).toBeVisible();
  await panel.getByTestId('apple-link').click();
  await panel.locator('#apple-password').fill(owner.password);
  await panel.getByTestId('apple-link-continue').click();
  await approveAtApple(page, sub);
  await page.waitForURL(/\/settings/);
  const linked = page.getByTestId('apple-linked');
  // A neutral label: Apple tells MowDeck nothing else (no scope, no e-mail address).
  await expect(linked).toContainText('Apple');
  await expect(linked).toContainText('Verknüpft');
  await expect(linked).not.toContainText(sub);
  expect(new URL(page.url()).searchParams.get('linked')).toBeNull();

  // 2. Sign in with Apple.
  await logout(page);
  await page.getByTestId('login-apple').click();
  await approveAtApple(page, sub);
  await page.waitForURL(/\/mowers/);

  // 3. Unlink; Apple no longer signs in, the password does.
  await page.goto('/settings#profile');
  await page.getByTestId('apple-unlink').click();
  // MowDeck keeps no Apple token, so the authorization at Apple is the user's to remove.
  await expect(page.getByTestId('apple-unlink-revoke-hint')).toBeVisible();
  await page.getByTestId('apple-unlink-confirm').click();
  await expect(page.getByTestId('apple-linked')).toHaveCount(0);
  await expect(page.getByTestId('apple-link')).toBeVisible();
  await expect(page.getByTestId('apple-revoke-hint')).toBeVisible();
  await logout(page);
  await page.getByTestId('login-apple').click();
  await approveAtApple(page, sub);
  // Back on the sign-in page (the fake's own address contains /login as well).
  await page.waitForURL((url) => url.pathname === '/login');
  await expect(
    page.getByText('Anmeldung mit Apple nicht möglich.', { exact: false }),
  ).toBeVisible();
  expect(new URL(page.url()).searchParams.get('sign_in_error')).toBeNull();
  await loginByUi(page, owner);
  csp.assertNone();
});

test('Apple: cancelling at Apple ends the round trip with a message', async ({ page, request }) => {
  const owner = actor('apple-cancel');
  const registered = await request.post('/api/v1/identity/register', {
    data: { username: owner.username, password: owner.password, account_name: 'Apple cancel' },
  });
  expect(registered.status(), await registered.text()).toBe(201);
  await page.goto('/login');
  await page.getByTestId('login-apple').click();
  await page.waitForURL(/\/fake-apple\/auth\/authorize\?/);
  await page.getByTestId('fake-apple-cancel').click();
  await page.waitForURL((url) => url.pathname === '/login');
  await expect(page.getByText('Die Anmeldung bei Apple wurde abgebrochen.')).toBeVisible();
});

test('Apple: the autofill offer failing in the browser does not replace the result message', async ({
  page,
}) => {
  // The passkey offer in the username field is a background attempt. Where the browser cannot
  // run it (here: it rejects with NotSupportedError, as headless browsers without an
  // authenticator may), the page must still show what the Apple round trip ended with, not "no
  // connection to the server".
  await page.addInitScript(() => {
    Object.defineProperty(navigator.credentials, 'get', {
      value: () => Promise.reject(new DOMException('not supported here', 'NotSupportedError')),
    });
  });
  const challenge = page.waitForResponse((r) => r.url().endsWith('/passkeys/challenge'));
  await page.goto('/login?sign_in_error=cancelled&provider=apple');
  await challenge;
  await expect(page.getByText('Die Anmeldung bei Apple wurde abgebrochen.')).toBeVisible();
  await expect(page.getByText('Keine Verbindung zum Server.')).toHaveCount(0);
});
