/**
 * The guided tour (ADR 0073) in the public demo (ADR 0068), against a demo stack (CI job
 * e2e-demo, projects demo and demo-phone); every test skips on another stack. It starts by itself
 * on the first visit of a browser and explains what the demo locks instead of triggering it.
 *
 * Where the outcome is kept depends on the demo: with the shared demo user only per browser (a
 * server-side outcome would end the tour for every visitor); with demo sandboxes (ADR 0076) per
 * user in Identity and per browser, so a returning visitor whose sandbox was replaced by a fresh
 * one does not get it again. Either way a new browser (a new visitor) gets it.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { FRESH_BROWSER, expectStep, shots, storedHints } from './tour';

test.use({ storageState: FRESH_BROWSER });

const DEMO_SESSION = '/api/v1/identity/demo/session';

async function demoConfig(
  request: APIRequestContext,
): Promise<{ sandbox?: { idleMinutes: number } } | null> {
  const response = await request.get('/config.json');
  if (!response.ok()) return null;
  const config = (await response.json()) as { demo?: { sandbox?: { idleMinutes: number } } };
  return typeof config.demo === 'object' && config.demo !== null ? config.demo : null;
}

test.beforeEach(async ({ request }) => {
  test.skip(!(await demoConfig(request)), 'not a demo stack (docker-compose.demo.yml)');
});

/** The access token of the next demo session the page starts. */
function nextDemoToken(page: Page): Promise<string> {
  return page
    .waitForResponse(
      (response) =>
        new URL(response.url()).pathname === DEMO_SESSION &&
        response.request().method() === 'POST' &&
        response.status() === 200,
      { timeout: 60_000 },
    )
    .then(async (response) => ((await response.json()) as { access_token: string }).access_token);
}

async function tryTheDemo(page: Page): Promise<string> {
  await page.goto('/login');
  const token = nextDemoToken(page);
  await page.getByTestId('login-demo-start').click();
  await expect(page).toHaveURL(/\/mowers$/);
  return await token;
}

test('the demo shows the tour on a first visit and explains what it locks', async ({
  page,
  browser,
  request,
}, testInfo) => {
  const phone = testInfo.project.name === 'demo-phone';
  const sandbox = (await demoConfig(request))?.sandbox !== undefined;
  const token = await tryTheDemo(page);
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
  if (sandbox) {
    // The sandbox user is the visitor's own: Identity keeps the outcome too.
    await expect
      .poll(() => storedHints(request, { username: '', password: '', token }))
      .toContain('tour-skipped');
  }

  // Not again in this browser ...
  await page.goto('/mowers');
  await expect(page.locator('[data-testid^="mower-row-"]')).toHaveCount(3, { timeout: 60_000 });
  await page.waitForTimeout(1_500);
  await expect(page.getByTestId('tour')).toHaveCount(0);

  if (sandbox) {
    // ... not even when the sandbox is gone and this browser gets a fresh one (a new user that
    // never saw the tour): the browser remembers it.
    await page.evaluate(() =>
      fetch('/api/v1/identity/session/logout', {
        method: 'POST',
        headers: { 'X-Requested-With': 'mowdeck-web', 'X-Mowdeck-Refresh': 'cookie-only' },
        credentials: 'same-origin',
      }),
    );
    const fresh = nextDemoToken(page);
    await page.goto('/mowers');
    expect(await fresh).not.toBe(token);
    await expect(page).toHaveURL(/\/mowers$/);
    await expect(page.locator('[data-testid^="mower-row-"]')).toHaveCount(3, { timeout: 60_000 });
    await page.waitForTimeout(1_500);
    await expect(page.getByTestId('tour')).toHaveCount(0);
  }

  // ... but the next visitor (a new browser) gets it.
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
