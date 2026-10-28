/**
 * Step-up re-authentication (ADR 0058), desktop Chromium with its virtual authenticator:
 *
 * 1. The last confirmation is older than the window: deleting a mower opens the re-authentication
 *    sheet, a wrong password keeps it open (and the session alive), the password confirms and the
 *    deletion goes through without a second click.
 * 2. The same with a passkey: the virtual authenticator confirms, the mower is gone.
 */
import { expect, test, type Page } from '@playwright/test';

import {
  actor,
  ageConfirmations,
  createMowerByApi,
  loginByApi,
  loginByUi,
  registerByApi,
  watchCsp,
} from './support';

async function virtualAuthenticator(page: Page) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('WebAuthn.enable');
  await cdp.send('WebAuthn.addVirtualAuthenticator', {
    options: {
      protocol: 'ctap2',
      transport: 'internal',
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  });
}

async function deleteViaSheet(page: Page, mowerId: string): Promise<void> {
  await page.goto(`/mowers/${mowerId}/settings`);
  await page.getByTestId('mower-delete').click();
  await page.getByTestId('mower-delete').click();
}

test('step-up: deleting a mower asks for the password again', async ({ page, request }) => {
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('stepup-pw'));
  const mowerId = await createMowerByApi(request, owner, 'Step-up Mäher');
  await loginByUi(page, owner);
  ageConfirmations(owner.username);
  await page.reload(); // the refresh now carries the old auth_time

  await deleteViaSheet(page, mowerId);
  const sheet = page.getByTestId('stepup-sheet');
  await expect(sheet).toBeVisible();

  await sheet.locator('#stepup-password').fill('not the password at all');
  await sheet.getByTestId('stepup-confirm').click();
  await expect(sheet.getByTestId('stepup-error')).toBeVisible();
  await expect(sheet).toBeVisible();

  await sheet.locator('#stepup-password').fill(owner.password);
  await sheet.getByTestId('stepup-confirm').click();
  await page.waitForURL(/\/mowers$/);
  const gone = await request.get(`/api/v1/fleet/mowers/${mowerId}`, {
    headers: { Authorization: `Bearer ${(await loginByApi(request, owner)).token}` },
  });
  expect(gone.status()).toBe(404);
  csp.assertNone();
});

test('step-up: a passkey confirms the deletion', async ({ page, request }) => {
  const csp = watchCsp(page);
  await virtualAuthenticator(page);
  const owner = await registerByApi(request, actor('stepup-pk'));
  const mowerId = await createMowerByApi(request, owner, 'Passkey Mäher');
  await loginByUi(page, owner);

  await page.goto('/settings#profile');
  const panel = page.getByTestId('passkeys-panel');
  await panel.getByTestId('passkey-add').click();
  await panel.locator('#passkey-password').fill(owner.password);
  await panel.getByTestId('passkey-create').click();
  await expect(panel.getByTestId('passkeys')).toBeVisible();

  ageConfirmations(owner.username);
  await page.reload();
  await deleteViaSheet(page, mowerId);
  const sheet = page.getByTestId('stepup-sheet');
  await expect(sheet).toBeVisible();
  await sheet.getByTestId('stepup-passkey').click();
  await page.waitForURL(/\/mowers$/);
  csp.assertNone();
});
