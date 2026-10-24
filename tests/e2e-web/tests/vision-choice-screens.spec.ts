/**
 * Review screenshots of the AI assist choice (run: pnpm --filter e2e-web screens): a mower stuck
 * in a two-area job, the fake model `fake/unsure_resume` 55 % sure that the job can continue
 * (below the 60 % threshold), so the sheet offers the dock fallback (preselected) and the
 * model's low-confidence resume; light and dark, phone (390 x 844) and desktop (1280 x 800),
 * written to apps/web/screenshots/assist-choice-*.png.
 */
import { fileURLToPath } from 'node:url';

import { expect, test, type Page } from '@playwright/test';

import { RUN, actor, commandByApi, loginByUi, registerByApi, waitForMap } from './support';

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
  test(`assist choice screenshots at ${size} width`, async ({ browser, request }) => {
    test.setTimeout(240_000);
    const owner = await registerByApi(request, actor(`shot-choice-${size}`));
    const auth = { Authorization: `Bearer ${owner.token}` };
    const saved = await request.put('/api/v1/vision/me/provider', {
      headers: auth,
      data: { model: 'fake/unsure_resume' },
    });
    expect(saved.status(), await saved.text()).toBe(200);
    const created = await request.post('/api/v1/fleet/mowers', {
      headers: auth,
      data: {
        name: `Luba 2 AWD ${RUN}`.slice(0, 40),
        connection_kind: 'simulated',
        connection_options: { stuck_after_seconds: 20, time_scale: 1 },
      },
    });
    expect(created.status(), await created.text()).toBe(201);
    const mowerId = ((await created.json()) as { id: string }).id;
    await waitForMap(request, owner, mowerId);
    await commandByApi(request, owner, mowerId, { kind: 'start_mow', area_ids: ['front', 'back'] });
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
    await expect(page.getByTestId('mower-state')).toContainText('Fehler', { timeout: 60_000 });
    await page.getByRole('button', { name: 'Mit KI-Hilfe befreien' }).click();
    const sheet = page.getByRole('dialog', { name: 'KI-Hilfe' });
    await sheet.getByTestId('assist-start').click();
    await expect(sheet.getByTestId('assist-choice')).toBeVisible({ timeout: 30_000 });
    await sheet
      .getByTestId('assist-choice')
      .evaluate((element) => element.scrollIntoView({ block: 'start' }));
    await both(page, `assist-choice-${size}`);
    await sheet.getByTestId('assist-option-model').getByRole('radio').check();
    await expect(sheet.getByTestId('assist-resume-plan')).toBeVisible();
    await sheet
      .getByTestId('assist-option-model')
      .evaluate((element) => element.scrollIntoView({ block: 'start' }));
    await both(page, `assist-choice-model-${size}`);
    await context.close();
  });
}
