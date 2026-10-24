/**
 * Review screenshots of the start sheet's job options (run: pnpm --filter e2e-web screens): the
 * "Mehr" section opened with obstacle detection, path order, laps around no-go zones and the
 * start progress set, light and dark, at phone (390 x 844) and desktop (1280 x 800) width,
 * written to apps/web/screenshots/start-options-*.png.
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
  test(`start options screenshots at ${size} width`, async ({ browser, request }) => {
    const owner = await registerByApi(request, actor(`shot-startopts-${size}`));
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
    await page.goto(`/mowers/${mowerId}`);
    const start = page.getByRole('button', { name: 'Starten' });
    // A fresh account's control grant may reach the authorization store after the page's first
    // permission check: load the page again until the start is offered.
    await expect(async () => {
      if (!(await start.isVisible())) await page.reload();
      await expect(start).toBeEnabled({ timeout: 10_000 });
    }).toPass({ timeout: 90_000 });
    await start.click();
    const picker = page.getByTestId('area-picker');
    await picker.locator('[data-area="front"]').click();
    const settings = picker.getByTestId('start-settings-toggle');
    if (await settings.isVisible()) await settings.click();
    await picker.getByTestId('start-more-toggle').click();
    await picker.getByTestId('start-obstacle-sensitive').click();
    await picker.getByTestId('start-order-grid_first').click();
    await picker.getByRole('button', { name: 'Weniger Runden um Sperrzonen' }).click();
    const later = picker.getByRole('button', { name: 'Später beginnen' });
    for (let i = 0; i < 6; i += 1) await later.click();
    const more = picker.getByTestId('start-more');
    await more.evaluate((element) => element.scrollIntoView({ block: 'start' }));
    await both(page, `start-options-${size}`);
    await context.close();
  });
}
