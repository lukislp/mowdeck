/**
 * Review screenshots of a schedule's start settings (run: pnpm --filter e2e-web screens): the
 * editor's "Mähmuster" panel with a pattern, angle, blade height, speed and the job options
 * behind "Mehr" set, light and dark, at phone (390 x 844) and desktop (1280 x 800) width,
 * written to apps/web/screenshots/schedule-options-*.png, and the schedule list with its compact
 * summary (schedule-options-list-*.png).
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
  test(`schedule options screenshots at ${size} width`, async ({ browser, request }) => {
    const owner = await registerByApi(request, actor(`shot-schedopts-${size}`));
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
    await page.goto(`/schedules/new?mower=${mowerId}`);
    const panel = page.getByTestId('sched-mow');
    await expect(async () => {
      if (!(await panel.getByTestId('sched-mow-pattern').isVisible())) await page.reload();
      await expect(panel.getByTestId('sched-mow-pattern')).toBeVisible({ timeout: 5_000 });
    }).toPass({ timeout: 60_000 });
    await page.locator('#sched-name').fill('Samstag diagonal');
    await panel.getByTestId('sched-mow-pattern-diagonal').click();
    const raise = panel.getByRole('button', { name: 'Winkel vergrößern' });
    for (let i = 0; i < 3; i += 1) await raise.click();
    const bladeUp = panel.getByRole('button', { name: 'Schnitthöhe erhöhen' });
    for (let i = 0; i < 6; i += 1) await bladeUp.click();
    await panel.getByTestId('sched-mow-more-toggle').click();
    await panel.getByTestId('sched-mow-obstacle-sensitive').click();
    await panel.getByTestId('sched-mow-order-border_first').click();
    await panel.evaluate((element) => element.scrollIntoView({ block: 'start' }));
    await both(page, `schedule-options-${size}`);

    const saved = page.waitForResponse(
      (r) => r.url().endsWith('/api/v1/scheduler/schedules') && r.request().method() === 'POST',
    );
    await page.getByTestId('sched-save').click();
    expect((await saved).status()).toBe(201);
    await page.waitForURL(/\/schedules$/);
    await expect(page.getByTestId('schedule-mow')).toContainText('Diagonal');
    await both(page, `schedule-options-list-${size}`);
    await context.close();
  });
}
