// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * Deleting your account or yourself inside the app (ADR 0082, step 4), Settings -> Profile:
 *
 * 1. The owner, the only member, schedules the deletion of the account: the typed confirmation,
 *    the step-up sheet (the last confirmation is older than the window), then the banner with
 *    the date on every page; "Cancel deletion" makes the banner go and the account is active.
 * 2. A member deletes themselves: at once, back on the login page with a neutral notice, and the
 *    same name and password no longer sign in.
 */
import { expect, test, type Page } from '@playwright/test';

import {
  actor,
  ageConfirmations,
  createAccountViewer,
  loginByApi,
  loginByUi,
  registerByApi,
  watchCsp,
  type Actor,
} from './support';

async function confirmDeletion(page: Page, who: Actor): Promise<void> {
  await page.goto('/settings#profile');
  const panel = page.getByTestId('deletion-panel');
  await panel.getByTestId('deletion-start').click();
  const confirm = panel.getByTestId('deletion-confirm');
  await expect(confirm).toBeDisabled();
  await panel.getByTestId('deletion-confirm-input').fill(who.username);
  await confirm.click();
}

test('the owner schedules the deletion of the account and cancels it', async ({
  page,
  request,
}) => {
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('delete-owner'));
  await loginByUi(page, owner);
  ageConfirmations(owner.username);
  await page.reload(); // the refresh now carries the old auth_time: the step-up sheet asks

  await confirmDeletion(page, owner);
  const sheet = page.getByTestId('stepup-sheet');
  await expect(sheet).toBeVisible();
  await sheet.locator('#stepup-password').fill(owner.password);
  await sheet.getByTestId('stepup-confirm').click();

  const banner = page.getByTestId('deletion-banner');
  await expect(banner).toBeVisible();
  await expect(banner).toContainText('Dieses Konto wird am');
  await expect(page.getByTestId('deletion-scheduled')).toBeVisible();
  // The owner keeps full access, and the banner is on every page of the app.
  await page.goto('/mowers');
  await expect(banner).toBeVisible();
  const account = await request.get(`/api/v1/identity/accounts/${owner.accountId}`, {
    headers: { Authorization: `Bearer ${(await loginByApi(request, owner)).token}` },
  });
  expect(((await account.json()) as { status: string }).status).toBe('deleting');

  await banner.getByTestId('deletion-banner-cancel').click();
  await expect(banner).toBeHidden();
  const restored = await request.get(`/api/v1/identity/accounts/${owner.accountId}`, {
    headers: { Authorization: `Bearer ${(await loginByApi(request, owner)).token}` },
  });
  expect(((await restored.json()) as { status: string }).status).toBe('active');
  csp.assertNone();
});

test('a member deletes themselves and cannot sign in again', async ({ page, request }) => {
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('delete-team'));
  const member = await createAccountViewer(request, owner, 'delete-member');
  await loginByUi(page, member);

  await confirmDeletion(page, member);
  // A fresh sign-in counts as a recent confirmation; an older one asks once more.
  const sheet = page.getByTestId('stepup-sheet');
  const notice = page.getByTestId('login-user-deleted');
  await expect(sheet.or(notice)).toBeVisible();
  if (await sheet.isVisible()) {
    await sheet.locator('#stepup-password').fill(member.password);
    await sheet.getByTestId('stepup-confirm').click();
  }
  await expect(notice).toBeVisible();
  await expect(page).toHaveURL(/\/login/);

  const again = await request.post('/api/v1/identity/session', {
    data: { username: member.username, password: member.password },
  });
  expect(again.status()).toBe(401);
  // The account and its owner go on.
  await loginByApi(request, owner);
  csp.assertNone();
});
