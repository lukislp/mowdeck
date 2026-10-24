/**
 * Review screenshots of the RTK base station page (run: pnpm --filter e2e-web screens): the
 * simulated base station with an assigned mower, its live state and an offered firmware, the
 * add sheet and the update confirmation, light and dark, at phone (390 x 844) and desktop
 * (1280 x 800) width, written to apps/web/screenshots/base-station-*.png.
 */
import { fileURLToPath } from 'node:url';

import { expect, test, type Page } from '@playwright/test';

import { actor, loginByUi, registerByApi, waitForMap } from './support';

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
  test(`base station screenshots at ${size} width`, async ({ browser, request }) => {
    const owner = await registerByApi(request, actor(`shot-base-${size}`));
    const headers = { Authorization: `Bearer ${owner.token}` };
    const created = await request.post('/api/v1/fleet/mowers', {
      headers,
      data: {
        name: 'Luba 2 AWD',
        connection_kind: 'simulated',
        connection_options: { base_station_firmware_available: 'sim-base-1.1.0' },
      },
    });
    const mowerId = ((await created.json()) as { id: string }).id;
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
    await page.goto('/fleet/base-stations');
    await expect(page.getByTestId('bs-empty')).toBeVisible();
    await both(page, `base-station-empty-${size}`);

    await page.getByTestId('bs-add-empty').click();
    await expect(page.getByTestId('bs-device-simulated')).toBeVisible();
    await both(page, `base-station-add-${size}`);
    await page.getByTestId('bs-simulated-add').click();

    const base = page.locator('li[data-testid^="bs-"][data-name]');
    await base.getByTestId('bs-link').click();
    await expect(base.getByTestId('bs-state')).toBeVisible({ timeout: 60_000 });
    await expect(base.getByTestId('bs-fw-available')).toHaveText('sim-base-1.1.0', {
      timeout: 60_000,
    });
    await page.waitForTimeout(6_000); // the toasts are gone
    await both(page, `base-station-${size}`);
    if (size === 'phone') {
      await base
        .getByTestId('bs-firmware')
        .evaluate((element) => element.scrollIntoView({ block: 'center' }));
      await both(page, `base-station-firmware-${size}`);
    }
    await base.getByTestId('bs-fw-install').click();
    await expect(page.getByTestId('bs-fw-confirm')).toBeVisible();
    await both(page, `base-station-confirm-${size}`);
    await context.close();
  });
}
