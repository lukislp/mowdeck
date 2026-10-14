/**
 * Phase 4 Task 22, operator console and plan display (desktop 1280 x 800, plus phone shots): a
 * fresh user made an operator admin through Identity's CLI (`grant-platform-admin`, run in the
 * identity container, see `grantPlatformAdmin`) sees the operator module, a normal owner does not
 * and gets the "operators only" notice on /operator. The operator finds the owner's account in the
 * list, assigns the plan `fleet` with a reason and sees it in the history; the plans page lists
 * `home` and `fleet`. With the stack's default (billing disabled) the owner's account page shows
 * the plan `unlimited` with the "no subscription limits" note and no billing element, and the
 * sign-in page has no links to legal pages. Screenshots:
 * `operator-accounts-desktop`, `operator-plan-sheet-phone`, `account-plan-phone`.
 */
import { fileURLToPath } from 'node:url';

import { expect, test, type Browser, type Page } from '@playwright/test';

import {
  PLATFORM_ADMIN_ENABLED,
  RUN,
  actor,
  grantPlatformAdmin,
  loginByUi,
  registerByApi,
  watchCsp,
} from './support';

const OUT = fileURLToPath(new URL('../../../apps/web/screenshots/', import.meta.url));

async function shot(page: Page, name: string): Promise<void> {
  await page.waitForTimeout(400); // fonts and the last state flash
  await page.screenshot({ path: `${OUT}${name}.png`, animations: 'disabled' });
}

async function phonePage(browser: Browser): Promise<Page> {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 1,
    locale: 'de-DE',
    timezoneId: 'Europe/Berlin',
    colorScheme: 'light',
    isMobile: true,
    hasTouch: true,
  });
  return context.newPage();
}

test.skip(!PLATFORM_ADMIN_ENABLED, 'MOWDECK_E2E_PLATFORM_ADMIN=off: no operator admin possible');

test('operator console: module for operator admins only, plan assignment with history', async ({
  page,
  browser,
  request,
}) => {
  const owner = await registerByApi(request, actor('household'));
  const operator = await registerByApi(request, actor('operator'));
  grantPlatformAdmin(operator);
  const accountName = `${owner.username} home`;

  // A normal owner: no operator module, the console refuses, the account page shows the plan.
  const csp = watchCsp(page);
  await loginByUi(page, owner);
  await expect(page.getByTestId('nav-mowers')).toBeVisible();
  await expect(page.getByTestId('nav-operator')).toHaveCount(0);
  await page.goto('/operator');
  await expect(page.getByTestId('operator-forbidden')).toBeVisible();
  await expect(page.getByTestId('operator-console')).toHaveCount(0);

  await page.goto('/account');
  const plan = page.getByTestId('account-plan');
  await expect(plan).toHaveAttribute('data-plan', 'unlimited');
  await expect(plan).toHaveAttribute('data-billing', 'false');
  await expect(page.getByTestId('plan-no-limits')).toBeVisible();
  await expect(page.getByTestId('plan-status')).toHaveAttribute('data-value', 'active');
  await expect(page.getByTestId('plan-limit-max_mowers')).toHaveAttribute(
    'data-limit',
    'unlimited',
  );
  await expect(page.getByTestId('plan-limit-max_mowers')).toHaveAttribute('data-used', '0');
  // Billing is off in the stack's default: the probe of /api/v1/billing/status finds no service
  // (404 or the SPA fallback), so not one billing element appears, not even for the owner.
  await expect(page.locator('[data-testid^="billing-"]')).toHaveCount(0);
  await page.getByTestId('logout').click();
  await page.waitForURL(/\/login/);
  // Legal pages are off by default (MOWDECK_WEB_LEGAL_ENABLED): no links on the sign-in page.
  await expect(page.getByTestId('login-submit')).toBeVisible();
  await expect(page.getByTestId('legal-links')).toHaveCount(0);

  // The operator admin: the module appears, the account list has the owner's account.
  await loginByUi(page, operator);
  await expect(page.getByTestId('nav-operator')).toBeVisible();
  await page.getByTestId('nav-operator').click();
  await page.waitForURL(/\/operator$/);
  await expect(page.getByTestId('operator-console')).toBeVisible();
  await expect(page.getByTestId('operator-accounts')).toBeVisible();
  await page.getByTestId('operator-search').fill(owner.username);
  const row = page.getByTestId(`operator-account-${owner.accountId}`);
  await expect(row).toBeVisible();
  await expect(row).toContainText(accountName);
  await expect(row).toContainText(owner.username);
  await expect(row).toHaveAttribute('data-plan', 'home');
  await page.getByTestId('operator-search').fill('');
  await expect(page.getByTestId('operator-accounts')).toBeVisible();
  await shot(page, 'operator-accounts-desktop');

  // Assign `fleet` with a reason; the history shows it, the stored plan follows.
  await page.getByTestId('operator-search').fill(owner.username);
  await expect(row).toBeVisible();
  await row.click();
  await page.waitForURL(new RegExp(`/operator/accounts/${owner.accountId}$`));
  await expect(page.getByTestId('operator-assigned')).toHaveAttribute('data-plan', 'home');
  await expect(page.getByTestId('operator-effective')).toHaveAttribute('data-plan', 'unlimited');
  await page.getByTestId('operator-assign').click();
  const sheet = page.getByTestId('operator-assign-sheet');
  await expect(sheet).toBeVisible();
  await sheet.getByTestId('assign-plan-fleet').click();
  await sheet.locator('#assign-status').selectOption('active');
  // The reason is required: no request without it.
  await sheet.getByTestId('assign-save').click();
  await expect(sheet.locator('#assign-reason-error')).toBeVisible();
  const reason = `Upgrade to fleet (${RUN})`;
  await sheet.locator('#assign-reason').fill(reason);
  await sheet.getByTestId('assign-save').click();
  await expect(sheet).toBeHidden();
  await expect(page.getByTestId('operator-assigned')).toHaveAttribute('data-plan', 'fleet');
  const history = page.getByTestId('operator-history');
  await expect(history.locator('li').first()).toHaveAttribute('data-plan', 'fleet');
  await expect(history.locator('li').first()).toContainText(reason);
  await expect(history.locator('li[data-plan="home"]')).toHaveCount(1);
  // No mower data anywhere in the console.
  await expect(page.getByTestId('operator-console')).not.toContainText(/Mäher \d|mower_id/);

  const stored = await request.get(`/api/v1/entitlements/accounts/${owner.accountId}`, {
    headers: { Authorization: `Bearer ${operator.token}` },
  });
  expect(stored.status()).toBe(200);
  const body = (await stored.json()) as {
    assigned: { plan: string; source: string };
    history: { plan: string; reason: string | null }[];
  };
  expect(body.assigned).toMatchObject({ plan: 'fleet', source: 'manual' });
  expect(body.history[0]).toMatchObject({ plan: 'fleet', reason });

  // Plans: home and fleet are offered.
  await page.getByTestId('operator-tab-plans').click();
  await page.waitForURL(/\/operator\/plans$/);
  await expect(page.getByTestId('operator-plan-home')).toContainText('Home');
  await expect(page.getByTestId('operator-plan-fleet')).toContainText('Fleet');
  await page.getByTestId('plan-edit-home').click();
  await expect(page.getByTestId('plan-editor')).toBeVisible();
  await expect(page.locator('#plan-limit-max_mowers')).toHaveValue('4');
  await expect(page.locator('#plan-key')).toBeDisabled();

  // System: every probed service answers through the gateway.
  await page.getByTestId('operator-tab-system').click();
  await expect(page.getByTestId('system-billing')).toHaveAttribute('data-value', 'false');
  for (const service of ['identity', 'entitlements', 'fleet']) {
    await expect(page.getByTestId(`system-service-${service}`)).toHaveAttribute(
      'data-verdict',
      'up',
    );
  }
  csp.assertNone();

  // Phone screenshots: the owner's plan section, the operator's assign sheet.
  const ownerPhone = await phonePage(browser);
  await loginByUi(ownerPhone, owner);
  await ownerPhone.goto('/account');
  await expect(ownerPhone.getByTestId('plan-no-limits')).toBeVisible();
  await ownerPhone.getByTestId('plan-panel').scrollIntoViewIfNeeded();
  await shot(ownerPhone, 'account-plan-phone');
  await ownerPhone.context().close();

  const operatorPhone = await phonePage(browser);
  await loginByUi(operatorPhone, operator);
  await operatorPhone.goto(`/operator/accounts/${owner.accountId}`);
  await operatorPhone.getByTestId('operator-assign').click();
  const phoneSheet = operatorPhone.getByTestId('operator-assign-sheet');
  await expect(phoneSheet).toBeVisible();
  await phoneSheet.getByTestId('assign-plan-fleet').click();
  await phoneSheet.locator('#assign-reason').fill('Fleet customer since October');
  await shot(operatorPhone, 'operator-plan-sheet-phone');
  await operatorPhone.context().close();
});
