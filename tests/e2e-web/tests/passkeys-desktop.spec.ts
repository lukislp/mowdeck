/**
 * Passkeys (ADR 0050), desktop Chromium with its virtual authenticator (CDP WebAuthn domain: a
 * platform authenticator with resident keys and user verification, presence simulated):
 *
 * 1. Settings: add a passkey (password again), it is listed with its name.
 * 2. Log out, "Mit Passkey anmelden" signs in without username or password.
 * 3. The "app" (here the test with the PKCE verifier) registers its challenge; the sign-in page
 *    opened with the request id (`?app_request=...&app_state=...`) hands the passkey login over,
 *    after the user confirms, as a one-time code in `mowdeck://auth/callback`, which the app
 *    redeems for a session; the browser session ends.
 * 4. The passkey offered in the username field's autofill (conditional UI) signs in; remove
 *    the passkey, a passkey login with it is refused.
 *
 * Every login counts against the per-address login limit; the test stack loosens it
 * (docker-compose.e2e.yml), so the test need not wait for it to refill.
 */
import { createHash, randomBytes } from 'node:crypto';

import { expect, test, type Page } from '@playwright/test';

import { actor, loginByUi, watchCsp } from './support';

async function virtualAuthenticator(page: Page) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('WebAuthn.enable');
  const { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', {
    options: {
      protocol: 'ctap2',
      transport: 'internal',
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  });
  return {
    /** Presence off: the passkey dialog waits (the autofill request cannot answer by itself). */
    async presence(enabled: boolean): Promise<void> {
      await cdp.send('WebAuthn.setAutomaticPresenceSimulation', { authenticatorId, enabled });
    },
    async count(): Promise<number> {
      const { credentials } = await cdp.send('WebAuthn.getCredentials', { authenticatorId });
      return credentials.length;
    },
  };
}

const b64url = (data: Buffer) => data.toString('base64url');

async function logout(page: Page): Promise<void> {
  await page.goto('/settings#profile');
  await page.getByTestId('logout').click();
  await page.waitForURL(/\/login/);
}

test('passkeys: add, sign in without a password, hand over to the app, remove', async ({
  page,
  request,
}) => {
  const csp = watchCsp(page);
  const authenticator = await virtualAuthenticator(page);
  // The stack has passkeys on (IDENTITY_PUBLIC_URL in stack.env / scripts/stack.mjs), and
  // GitHub against its fake GitHub (docker-compose.e2e.yml).
  const methods = await request.get('/api/v1/identity/login-methods');
  expect(await methods.json()).toEqual({ password: true, passkey: true, github: true });
  // Registered without the API login of registerByApi: every login counts (see below).
  const owner = actor('passkey');
  const registered = await request.post('/api/v1/identity/register', {
    data: { username: owner.username, password: owner.password, account_name: 'Passkey home' },
  });
  expect(registered.status(), await registered.text()).toBe(201);

  // 1. Add a passkey in the settings.
  await loginByUi(page, owner);
  await page.goto('/settings#profile');
  const panel = page.getByTestId('passkeys-panel');
  await expect(panel).toBeVisible();
  await expect(panel.getByTestId('passkeys-empty')).toBeVisible();
  await panel.getByTestId('passkey-add').click();
  await panel.locator('#passkey-password').fill(owner.password);
  await panel.locator('#passkey-name').fill('E2E Laptop');
  await panel.getByTestId('passkey-create').click();
  await expect(panel.getByTestId('passkey-E2E Laptop')).toBeVisible();
  expect(await authenticator.count()).toBe(1);

  // 2. Sign in with the passkey: no username, no password. Presence is held back until the
  // button is pressed, so the autofill request started with the page cannot answer first.
  await authenticator.presence(false);
  await logout(page);
  await authenticator.presence(true);
  await page.getByTestId('login-passkey').click();
  await page.waitForURL(/\/mowers/);

  // 3. The sign-in page opened for the app hands the login over.
  await authenticator.presence(false);
  await logout(page);
  const verifier = b64url(randomBytes(32));
  const challenge = b64url(createHash('sha256').update(verifier).digest());
  const state = b64url(randomBytes(16));
  // The "app" registers its challenge; the page gets only the request id.
  const started = await request.post('/api/v1/identity/app-login/start', {
    data: { code_challenge: challenge },
  });
  expect(started.status(), await started.text()).toBe(200);
  const requestId = ((await started.json()) as { request_id: string }).request_id;
  await page.goto(`/login?app_request=${requestId}&app_state=${state}`);
  await expect(page.getByTestId('login-app-notice')).toBeVisible();
  await authenticator.presence(true);
  await page.getByTestId('login-passkey').click();
  // Signed in for the app: the user confirms before the login goes there.
  await page.getByTestId('login-app-continue').click();
  const back = page.getByTestId('login-app-return');
  await expect(back).toBeVisible();
  const link = new URL((await back.getAttribute('href'))!);
  expect(link.protocol).toBe('mowdeck:');
  expect(link.searchParams.get('state')).toBe(state);
  const code = link.searchParams.get('code')!;
  expect(code).toMatch(/^mda_/);
  // The browser keeps no session: the refresh cookie was dropped with the hand-off.
  const browserRefresh = await page.request.post('/api/v1/identity/session/refresh', {
    headers: { 'X-Requested-With': 'e2e' },
  });
  expect(browserRefresh.ok()).toBe(false);
  // The app redeems code and verifier (single use and the wrong verifier: unit tests).
  const app = await request.post('/api/v1/identity/app-login/session', {
    data: { code, code_verifier: verifier },
  });
  expect(app.status(), await app.text()).toBe(200);

  // 4. The autofill (conditional UI) signs in: the virtual authenticator picks the passkey by
  // itself. Then remove the passkey: it no longer signs in.
  await page.goto('/login');
  await page.waitForURL(/\/mowers/);
  await page.goto('/settings#profile');
  await page.getByTestId('passkey-remove-E2E Laptop').click();
  await page.getByTestId('passkey-remove-confirm').click();
  await expect(page.getByTestId('passkeys-empty')).toBeVisible();
  await logout(page);
  await authenticator.presence(true);
  await page.getByTestId('login-passkey').click();
  await expect(
    page.getByText('Dieser Passkey wurde nicht akzeptiert.', { exact: false }),
  ).toBeVisible();
  csp.assertNone();
});
