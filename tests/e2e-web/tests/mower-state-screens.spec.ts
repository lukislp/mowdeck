/**
 * Review screenshots of the typed mower state (run: pnpm --filter e2e-web screens): the job card
 * and the reception and health panel of a mowing mower, light and dark, at phone (390 x 844) and
 * desktop (1280 x 800) width, written to apps/web/screenshots/mower-state-*.png.
 */
import { fileURLToPath } from 'node:url';

import { expect, test, type Page } from '@playwright/test';

import {
  actor,
  commandByApi,
  createMowerByApi,
  loginByUi,
  registerByApi,
  waitForMap,
} from './support';

const OUT = fileURLToPath(new URL('../../../apps/web/screenshots/', import.meta.url));
const SIZES = {
  phone: { width: 390, height: 844 },
  desktop: { width: 1280, height: 800 },
} as const;

async function both(page: Page, name: string): Promise<void> {
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.waitForTimeout(400); // fonts and the last state flash
    await page.screenshot({ path: `${OUT}${name}-${scheme}.png`, animations: 'disabled' });
  }
  await page.emulateMedia({ colorScheme: 'light' });
}

for (const size of Object.keys(SIZES) as (keyof typeof SIZES)[]) {
  test(`mower state screenshots at ${size} width`, async ({ browser, request }) => {
    const owner = await registerByApi(request, actor(`shot-state-${size}`));
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
    await commandByApi(request, owner, mowerId, { kind: 'start_mow', area_ids: ['front', 'back'] });
    await page.goto(`/mowers/${mowerId}`);
    const card = page.getByTestId('job-card');
    await expect(card.getByTestId('job-area')).toHaveText('Front lawn', { timeout: 60_000 });
    await expect
      .poll(
        async () =>
          Number((await card.getByTestId('job-progress').getAttribute('aria-valuenow')) ?? 0),
        { timeout: 90_000 },
      )
      .toBeGreaterThan(20);

    if (size === 'phone') {
      await both(page, 'mower-state-job-phone');
      await page.getByTestId('reception').scrollIntoViewIfNeeded();
      await both(page, 'mower-state-reception-phone');
    } else {
      await page.getByTestId('job-card').scrollIntoViewIfNeeded();
      await both(page, 'mower-state-desktop');
      await page.getByTestId('reception').scrollIntoViewIfNeeded();
      await both(page, 'mower-state-reception-desktop');
    }
    await context.close();
  });
}
