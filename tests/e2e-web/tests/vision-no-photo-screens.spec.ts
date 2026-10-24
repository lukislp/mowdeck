/**
 * Review screenshots of an automatic analysis that got no camera photo (run: pnpm --filter
 * e2e-web screens): the assist sheet opened by the notification's link shows why ("Das WLAN des
 * Mähers war zu schwach (-79 dBm) ...") and offers to start the analysis by hand; light and
 * dark, phone (390 x 844) and desktop (1280 x 800), written to
 * apps/web/screenshots/assist-no-photo-*.png.
 */
import { fileURLToPath } from 'node:url';

import { expect, test, type Page } from '@playwright/test';

import { actor, createMowerByApi, loginByUi, registerByApi, waitForMap } from './support';
import { routeNoPhoto } from './vision-no-photo';

const OUT = fileURLToPath(new URL('../../../apps/web/screenshots/', import.meta.url));
const SIZES = {
  phone: { width: 390, height: 844 },
  desktop: { width: 1280, height: 800 },
} as const;
const PROPOSAL = '7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d';

async function both(page: Page, name: string): Promise<void> {
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.waitForTimeout(400); // fonts
    await page.screenshot({ path: `${OUT}${name}-${scheme}.png`, animations: 'disabled' });
  }
  await page.emulateMedia({ colorScheme: 'light' });
}

for (const size of Object.keys(SIZES) as (keyof typeof SIZES)[]) {
  test(`assist without photo screenshots at ${size} width`, async ({ browser, request }) => {
    const owner = await registerByApi(request, actor(`shot-nophoto-${size}`));
    const saved = await request.put('/api/v1/vision/me/provider', {
      headers: { Authorization: `Bearer ${owner.token}` },
      data: { model: 'fake/recover' },
    });
    expect(saved.status(), await saved.text()).toBe(200);
    const mowerId = await createMowerByApi(request, owner, 'Luba 2 AWD');
    await waitForMap(request, owner, mowerId);
    const context = await browser.newContext({
      viewport: SIZES[size],
      deviceScaleFactor: 1,
      locale: 'de-DE',
      colorScheme: 'light',
      isMobile: size === 'phone',
      hasTouch: size === 'phone',
    });
    const page = await context.newPage();
    await loginByUi(page, owner);
    await routeNoPhoto(page, mowerId, PROPOSAL);
    await page.goto(`/mowers/${mowerId}?assist=${PROPOSAL}`);
    const sheet = page.getByRole('dialog', { name: 'KI-Hilfe' });
    await expect(sheet.getByTestId('assist-photo-problem')).toBeVisible({ timeout: 30_000 });
    await both(page, `assist-no-photo-${size}`);
    await context.close();
  });
}
