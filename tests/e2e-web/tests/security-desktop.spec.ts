/**
 * Security center (ADR 0065), desktop: a sign-in from a device the user never used before
 * arrives as a "new sign-in" notification; its link opens the security section with that
 * session flagged. The user sees every signed-in device (this browser marked), signs the
 * stranger's session out after a step-up, and the stranger's refresh token stops working at
 * once while this browser stays signed in.
 */
import { expect, request as playwrightRequest, test } from '@playwright/test';

import { actor, ageConfirmations, loginByUi, registerByApi, watchCsp } from './support';

const IDENTITY = '/api/v1/identity';

/** The `sid` claim of an access token: the login session it belongs to. */
function sessionOf(accessToken: string): string {
  const payload = accessToken.split('.')[1] ?? '';
  return (JSON.parse(Buffer.from(payload, 'base64url').toString('utf-8')) as { sid: string }).sid;
}

test('security: new-device notification, session list, sign a stranger out', async ({
  page,
  request,
  baseURL,
}) => {
  test.setTimeout(180_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('security'));
  // The shared registration helper turns the alerts off; this test is about them.
  const alertsOn = await request.put(`${IDENTITY}/me/security/alerts`, {
    headers: { Authorization: `Bearer ${owner.token}` },
    data: { new_login_alerts: true },
  });
  expect(alertsOn.status(), await alertsOn.text()).toBe(200);

  // A stranger signs in from another device (its own cookie jar and user agent).
  const stranger = await playwrightRequest.newContext({
    baseURL,
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0',
  });
  const strangerLogin = await stranger.post(`${IDENTITY}/session`, {
    data: { username: owner.username, password: owner.password },
  });
  expect(strangerLogin.status(), await strangerLogin.text()).toBe(200);
  const strangerTokens = (await strangerLogin.json()) as {
    access_token: string;
    refresh_token: string;
  };
  const strangerSession = sessionOf(strangerTokens.access_token);

  // The owner's browser: the inbox has the notification about the stranger's sign-in.
  await loginByUi(page, owner);
  await page.goto('/notifications');
  const alert = page
    .locator('[data-testid="inbox-item"][data-kind="security_new_login"]')
    .filter({ hasText: 'Firefox' });
  await expect(async () => {
    await page.reload();
    await expect(alert).toBeVisible({ timeout: 5_000 });
  }).toPass({ timeout: 60_000, intervals: [2_000] });

  // Its link: the security section with the stranger's session flagged.
  const inbox = await request.get('/api/v1/notifications/inbox', {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  expect(inbox.status(), await inbox.text()).toBe(200);
  const items = ((await inbox.json()) as { items: { kind: string; url: string }[] }).items;
  const link = items.find(
    (i) => i.kind === 'security_new_login' && i.url.includes(strangerSession),
  );
  expect(link, JSON.stringify(items)).toBeTruthy();
  expect(new URL(link!.url, 'http://app.invalid').pathname).toBe('/settings/security');

  // Signing out another device needs a fresh confirmation.
  ageConfirmations(owner.username);
  await page.goto(`/settings/security?revoke=${strangerSession}`);
  await page.waitForURL(/\/settings\?revoke=.*#security$/);
  const section = page.getByTestId('settings-section-security');
  await expect(section.getByTestId('security-posture')).toBeVisible();
  await expect(section.getByTestId('session-current')).toContainText('Dieses Gerät');
  await expect(section.getByTestId(`session-${strangerSession}`)).toContainText(
    'Firefox auf Windows',
  );
  const flagged = section.getByTestId('security-flagged');
  await expect(flagged).toContainText('Firefox auf Windows');
  await flagged.getByTestId('security-flagged-revoke').click();

  const sheet = page.getByTestId('stepup-sheet');
  await expect(sheet).toBeVisible();
  await sheet.locator('#stepup-password').fill(owner.password);
  await sheet.getByTestId('stepup-confirm').click();

  await expect(section.getByTestId(`session-${strangerSession}`)).toHaveCount(0);
  await expect(flagged).toHaveCount(0);
  await expect(section.getByTestId('security-event-session.revoked')).toBeVisible();

  // The stranger's refresh fails at once; the owner's browser stays signed in.
  const refreshed = await stranger.post(`${IDENTITY}/session/refresh`, {
    data: { refresh_token: strangerTokens.refresh_token },
  });
  expect(refreshed.status()).toBe(401);
  const strangerMe = await stranger.get(`${IDENTITY}/me`, {
    headers: { Authorization: `Bearer ${strangerTokens.access_token}` },
  });
  expect(strangerMe.status()).toBe(401);
  await page.reload();
  await expect(section.getByTestId('session-current')).toBeVisible();
  await stranger.dispose();
  csp.assertNone();
});
