/**
 * Review screenshots of the mower's own settings (run: pnpm --filter e2e-web screens): the
 * section "Am Mäher" of the simulated mower right after a change the mower applied, light and
 * dark, at phone (390 x 844) and desktop (1280 x 800) width, written to
 * apps/web/screenshots/device-settings-*.png.
 */
import { fileURLToPath } from 'node:url';

import { expect, test, type Page } from '@playwright/test';

import { actor, createMowerByApi, loginByUi, registerByApi, waitForMap } from './support';

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
  test(`device settings screenshots at ${size} width`, async ({ browser, request }) => {
    const owner = await registerByApi(request, actor(`shot-devset-${size}`));
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
    const section = page.getByTestId('device-settings-section');
    await expect(section.getByTestId('setting-rain_detection-switch')).toHaveAttribute(
      'aria-checked',
      'true',
      { timeout: 60_000 },
    );
    await page.getByTestId('setting-voice_volume').getByRole('button').last().click();
    await expect(page.getByTestId('setting-voice_volume')).toHaveAttribute('data-state', 'saved', {
      timeout: 30_000,
    });
    await section.evaluate((element) => element.scrollIntoView({ block: 'start' }));
    await both(page, `device-settings-${size}`);
    await page
      .getByTestId('setting-voice_volume')
      .evaluate((element) => element.scrollIntoView({ block: 'center' }));
    await both(page, `device-settings-voice-${size}`);
    await context.close();
  });
}
