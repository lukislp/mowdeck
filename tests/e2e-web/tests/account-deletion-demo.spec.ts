// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * The public demo (ADR 0068) deletes nothing (ADR 0082, step 4): the danger area of Settings ->
 * Profile is there, says so and is locked like every other refused action. Against a demo stack
 * only (CI job e2e-demo); on any other stack the test skips.
 */
import { expect, test, type APIRequestContext } from '@playwright/test';

async function demoStack(request: APIRequestContext): Promise<boolean> {
  const response = await request.get('/config.json');
  if (!response.ok()) return false;
  const config = (await response.json()) as Record<string, unknown>;
  return typeof config.demo === 'object' && config.demo !== null;
}

test('the danger area is locked in the demo', async ({ page, request }) => {
  test.skip(!(await demoStack(request)), 'not a demo stack (docker-compose.demo.yml)');
  await page.goto('/login');
  await page.getByTestId('login-demo-start').click();
  await expect(page).toHaveURL(/\/mowers$/);

  await page.goto('/settings#profile');
  const panel = page.getByTestId('deletion-panel');
  await expect(panel).toBeVisible();
  await expect(panel.getByTestId('deletion-demo')).toContainText(
    'In der öffentlichen Demo lässt sich nichts löschen',
  );
  const lock = panel.getByTestId('demo-lock-deletion');
  await expect(lock).toContainText('In der Demo nicht verfügbar');
  const buttons = lock.locator('button');
  for (const button of await buttons.all()) await expect(button).toBeDisabled();
  await expect(page.getByTestId('deletion-banner')).toHaveCount(0);
});
