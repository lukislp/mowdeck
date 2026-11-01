/**
 * The guided tour (ADR 0073) in the public demo (ADR 0068), against a demo stack (CI job
 * e2e-demo, projects demo and demo-phone); every test skips on another stack. Every visitor
 * shares the demo user, so the tour's outcome is kept per browser: it starts by itself on the
 * first visit of a browser, explains what the demo locks instead of triggering it, and a second
 * browser gets it again after the first one left it.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { FRESH_BROWSER, expectStep, shots } from './tour';

test.use({ storageState: FRESH_BROWSER });

async function demoStack(request: APIRequestContext): Promise<boolean> {
  const response = await request.get('/config.json');
  if (!response.ok()) return false;
  const config = (await response.json()) as Record<string, unknown>;
  return typeof config.demo === 'object' && config.demo !== null;
}

test.beforeEach(async ({ request }) => {
  test.skip(!(await demoStack(request)), 'not a demo stack (docker-compose.demo.yml)');
});

async function tryTheDemo(page: Page): Promise<void> {
  await page.goto('/login');
  await page.getByTestId('login-demo-start').click();
  await expect(page).toHaveURL(/\/mowers$/);
}

test('the demo shows the tour on a first visit and explains what it locks', async ({
  page,
  browser,
}, testInfo) => {
  const phone = testInfo.project.name === 'demo-phone';
  await tryTheDemo(page);
  await expectStep(page, 'welcome', false);
  await expect(page.getByTestId('tour-demo-note')).toContainText('öffentliche Demo');
  await shots(page, `tour-demo-welcome-${phone ? 'phone' : 'desktop'}`);

  await page.keyboard.press('ArrowRight');
  await expectStep(page, 'overview');
  await expect(page.getByTestId('tour-demo-note')).toContainText('in der Demo gesperrt');
  await expect(page.locator('[data-testid^="mower-row-"]')).toHaveCount(3, { timeout: 60_000 });
  await shots(page, `tour-demo-overview-${phone ? 'phone' : 'desktop'}`);

  await page.keyboard.press('ArrowRight');
  await expectStep(page, 'map');
  await page.keyboard.press('ArrowRight');
  await expectStep(page, 'commands');
  // Commands work in the demo; the tour only points at them and triggers nothing.
  await expect(page.getByTestId('tour-demo-note')).toContainText('simulierten Mähern');

  await page.keyboard.press('Escape');
  await expect(page.getByTestId('tour')).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem('mowdeck.tour.demo'))).toBe('skipped');

  // Not again in this browser ...
  await page.goto('/mowers');
  await expect(page.locator('[data-testid^="mower-row-"]')).toHaveCount(3, { timeout: 60_000 });
  await page.waitForTimeout(1_500);
  await expect(page.getByTestId('tour')).toHaveCount(0);

  // ... but the next visitor (a new browser) gets it, although the demo user is shared.
  const other = await browser.newContext({
    storageState: FRESH_BROWSER,
    viewport: page.viewportSize(),
    locale: 'de-DE',
  });
  const visitor = await other.newPage();
  await tryTheDemo(visitor);
  await expectStep(visitor, 'welcome', false);
  await other.close();
});
