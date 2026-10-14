/**
 * Notifications in the PWA against the stack's Notifications service (desktop, 1280 x 800):
 * preferences and an ntfy target are saved and survive a reload, the test button answers 202
 * and then 429 with a cooldown, the unread badge appears and clears, and a simulated job that
 * ends at the dock shows up as "mowing finished" in the inbox. Web Push delivery itself needs a
 * push service and is covered by the unit tests with a fake PushManager.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import {
  RUN,
  actor,
  commandByApi,
  createMowerByApi,
  loginByUi,
  registerByApi,
  waitForMap,
  watchCsp,
  type Actor,
} from './support';

const API = '/api/v1/notifications';

async function switchOn(page: Page, testid: string): Promise<void> {
  const control = page.getByTestId(testid);
  if ((await control.getAttribute('aria-checked')) !== 'true') await control.click();
  await expect(control).toHaveAttribute('aria-checked', 'true');
}

async function followAllMowers(request: APIRequestContext, who: Actor): Promise<void> {
  const response = await request.put(`${API}/preferences`, {
    headers: { Authorization: `Bearer ${who.token}` },
    data: {
      enabled_kinds: ['mowing_finished'],
      all_mowers: true,
      timezone: 'Europe/Berlin',
      language: 'de',
      channels: ['log'],
    },
  });
  expect(response.status(), await response.text()).toBe(200);
}

test('notification settings: preferences, ntfy target, test cooldown, unread badge', async ({
  page,
  request,
}) => {
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('notif'));
  await createMowerByApi(request, owner, 'Garden south');
  await loginByUi(page, owner);

  await page.getByTestId('nav-notifications').click();
  await page.waitForURL(/\/notifications$/);
  await expect(page.getByTestId('inbox-empty')).toBeVisible();
  await page.getByTestId('notification-settings-link').click();
  await page.waitForURL(/\/settings#notifications$/);

  // Preferences: finished and stuck, every mower, quiet hours; saved and still there after reload.
  await switchOn(page, 'kind-mowing_finished');
  await switchOn(page, 'kind-mower_stuck');
  await switchOn(page, 'all-mowers');
  await switchOn(page, 'quiet-hours');
  await page.locator('#quiet-start').fill('22:30');
  await page.locator('#quiet-end').fill('06:45');
  await expect(page.getByTestId('notif-timezone')).toContainText('Europe/Berlin');
  const saved = page.waitForResponse(
    (r) => r.url().endsWith(`${API}/preferences`) && r.request().method() === 'PUT',
  );
  await page.getByTestId('notif-save').click();
  expect((await saved).status()).toBe(200);
  await page.reload();
  await expect(page.getByTestId('kind-mowing_finished')).toHaveAttribute('aria-checked', 'true');
  await expect(page.getByTestId('kind-mower_stuck')).toHaveAttribute('aria-checked', 'true');
  await expect(page.getByTestId('all-mowers')).toHaveAttribute('aria-checked', 'true');
  await expect(page.locator('#quiet-start')).toHaveValue('22:30');
  await expect(page.locator('#quiet-end')).toHaveValue('06:45');

  // Web Push needs HTTPS or localhost: on the stack's http://localhost the browser may offer it.
  await expect(page.getByTestId('push-panel')).toBeVisible();
  await expect(page.getByTestId('push-subscriptions')).toBeVisible();

  // ntfy: the token is write-only ("set" afterwards, never shown).
  const topic = `mowdeck-e2e-${RUN}`;
  await page.locator('#ntfy-server').fill('https://ntfy.sh');
  await page.locator('#ntfy-topic').fill(topic);
  await page.locator('#ntfy-token').fill('tk_e2e_secret');
  await page.getByTestId('ntfy-save').click();
  await expect(page.getByTestId('ntfy-status')).toContainText(topic);
  await expect(page.getByTestId('ntfy-token-set')).toBeVisible();
  await expect(page.locator('#ntfy-token')).toHaveValue('');
  await page.reload();
  await expect(page.getByTestId('ntfy-status')).toContainText(`https://ntfy.sh/${topic}`);
  await expect(page.getByTestId('ntfy-token-set')).toBeVisible();
  expect(await page.content()).not.toContain('tk_e2e_secret');
  // Remove it again before the test notification, so nothing is sent to ntfy.sh.
  await page.getByTestId('ntfy-remove').click();
  await expect(page.getByTestId('ntfy-status')).toContainText('nicht eingerichtet');

  // Test notification: 202, then 429 with a cooldown.
  const first = page.waitForResponse((r) => r.url().endsWith(`${API}/test`));
  await page.getByTestId('notif-test').click();
  expect((await first).status()).toBe(202);
  await expect(page.getByTestId('notif-test-result')).toBeVisible();
  const second = page.waitForResponse((r) => r.url().endsWith(`${API}/test`));
  await page.getByTestId('notif-test').click();
  expect((await second).status()).toBe(429);
  await expect(page.getByTestId('notif-test-cooldown')).toContainText(/Wieder möglich in \d+ s/);
  await expect(page.getByTestId('notif-test')).toBeDisabled();

  // The test notification is unread: the badge shows it until everything is marked read.
  const badge = page.getByTestId('nav-notifications-badge');
  await expect(badge).toHaveText(/1/);
  await page.getByTestId('nav-notifications').click();
  await expect(page.locator('[data-testid="inbox-item"][data-kind="test"]')).toBeVisible();
  await expect(page.locator('[data-testid="inbox-item"][data-unread="true"]')).toHaveCount(1);
  await page.getByTestId('inbox-mark-all').click();
  await expect(badge).toHaveCount(0);
  await expect(page.locator('[data-testid="inbox-item"][data-unread="true"]')).toHaveCount(0);
  csp.assertNone();
});

test('inbox: a finished simulated job arrives as "mowing finished"', async ({ page, request }) => {
  test.setTimeout(360_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('inbox'));
  const mowerId = await createMowerByApi(request, owner, 'Luba inbox');
  await waitForMap(request, owner, mowerId);
  await followAllMowers(request, owner);

  // A job of more than NOTIFICATIONS_MOWING_MIN_SECONDS (10 s in the stack) that ends docked.
  await commandByApi(request, owner, mowerId, { kind: 'start_mow', area_ids: ['front'] });
  await page.waitForTimeout(20_000);
  await commandByApi(request, owner, mowerId, { kind: 'return_to_dock' });

  await loginByUi(page, owner);
  await page.goto('/notifications');
  const finished = page.locator('[data-testid="inbox-item"][data-kind="mowing_finished"]');
  await expect(async () => {
    await page.reload();
    await expect(finished).toBeVisible({ timeout: 5_000 });
  }).toPass({ timeout: 240_000, intervals: [5_000] });
  await expect(finished).toContainText('Luba inbox');
  await expect(finished).toHaveAttribute('data-unread', 'true');
  await expect(page.getByTestId('nav-notifications-badge')).toBeVisible();

  // Opening it marks it read and leads to the mower.
  await finished.click();
  await page.waitForURL(new RegExp(`/mowers/${mowerId}$`));
  await expect(page.getByTestId('emergency-stop')).toBeInViewport();
  await expect(page.getByTestId('nav-notifications-badge')).toHaveCount(0);
  csp.assertNone();
});
