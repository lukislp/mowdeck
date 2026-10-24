/**
 * Review screenshots of the maintenance section (run: pnpm --filter e2e-web screens): the list of
 * the simulated mower's actions while it mows (the dock-bound ones disabled with their reason,
 * a map sync done) and the confirmation of a restart at the dock, light and dark, at phone
 * (390 x 844) and desktop (1280 x 800) width, written to apps/web/screenshots/maintenance-*.png.
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
    await page.waitForTimeout(400); // fonts
    await page.screenshot({ path: `${OUT}${name}-${scheme}.png`, animations: 'disabled' });
  }
  await page.emulateMedia({ colorScheme: 'light' });
}

for (const size of Object.keys(SIZES) as (keyof typeof SIZES)[]) {
  test(`maintenance screenshots at ${size} width`, async ({ browser, request }) => {
    const owner = await registerByApi(request, actor(`shot-maint-${size}`));
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
    await page.goto(`/mowers/${mowerId}/settings`);
    const section = page.getByTestId('maintenance-section');
    await expect(section.getByTestId('maint-restart_mower')).toHaveAttribute(
      'data-blocked',
      'none',
      { timeout: 60_000 },
    );

    // The confirmation of a restart at the dock.
    await page.getByTestId('maint-restart_mower-open').click();
    await expect(page.getByTestId('maint-confirm-body')).toBeVisible();
    await both(page, `maintenance-confirm-${size}`);
    await page.getByTestId('maint-confirm').getByRole('button', { name: 'Abbrechen' }).click();

    // The list while it mows, after a map sync.
    await page.getByTestId('maint-sync_map-open').click();
    await page.getByTestId('maint-confirm-go').click();
    await expect(page.getByTestId('maint-sync_map-result')).toHaveAttribute(
      'data-status',
      'executed',
      { timeout: 60_000 },
    );
    await commandByApi(request, owner, mowerId, { kind: 'start_mow', area_ids: ['front'] });
    await expect(page.getByTestId('maint-restart_mower-reason')).toBeVisible({ timeout: 60_000 });
    await page.waitForTimeout(6_000); // the result toast is gone
    await section.evaluate((element) => element.scrollIntoView({ block: 'start' }));
    await both(page, `maintenance-${size}`);
    await context.close();
  });
}
