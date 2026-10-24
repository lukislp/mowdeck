/**
 * Review screenshots of the API tokens section (run: pnpm --filter e2e-web screens): the list
 * with a view-only and a full Home Assistant token, the presets with the full one chosen and its
 * warning, and the custom selection, light and dark, at phone (390 x 844) and desktop
 * (1280 x 800) width, written to apps/web/screenshots/api-tokens-*.png.
 */
import { fileURLToPath } from 'node:url';

import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test';

import { actor, loginByUi, registerByApi, type Actor } from './support';

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

async function tokenByApi(
  request: APIRequestContext,
  owner: Actor,
  name: string,
  permissions: string[],
): Promise<void> {
  const response = await request.post(`/api/v1/identity/accounts/${owner.accountId}/api-tokens`, {
    headers: { Authorization: `Bearer ${owner.token}` },
    data: { name, grants: [{ level: { type: 'account', id: owner.accountId }, permissions }] },
  });
  expect(response.status(), await response.text()).toBe(201);
}

/** Scroll `element` to the top, clear of the sticky top bar and section strip on phones. */
async function top(element: Locator): Promise<void> {
  await element.evaluate((node) => {
    node.scrollIntoView({ block: 'start' });
    let box = node.parentElement;
    while (box && box.scrollHeight <= box.clientHeight) box = box.parentElement;
    // On phones the section index is a strip under the top bar; on desktops a side column.
    const strip = document.querySelector('[data-testid="settings-index"]')?.getBoundingClientRect();
    const below = strip && strip.width > window.innerWidth / 2 ? Math.max(0, strip.bottom) : 0;
    box?.scrollBy(0, -(below + 8));
  });
}

for (const size of Object.keys(SIZES) as (keyof typeof SIZES)[]) {
  test(`api token screenshots at ${size} width`, async ({ browser, request }) => {
    const owner = await registerByApi(request, actor(`shot-tokens-${size}`));
    await tokenByApi(request, owner, 'Wohnzimmer-Tablet', ['mower.view']);
    await tokenByApi(request, owner, 'Home Assistant', [
      'mower.view',
      'mower.control',
      'mower.drive',
      'fleet.manage',
      'schedule.edit',
    ]);
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
    await page.goto('/settings#api-tokens');
    const section = page.getByTestId('settings-section-api-tokens');
    await expect(section.getByTestId('token-row')).toHaveCount(2);
    await expect(section.getByTestId('token-included').first()).toBeVisible();

    // The list with the rights of each token.
    await top(section);
    await both(page, `api-tokens-${size}`);

    // The presets with the full one chosen and its warning.
    await section.getByTestId('token-preset-full').check();
    await section.locator('#token-name').fill('Home Assistant Garten');
    await expect(section.getByTestId('token-warning')).toBeVisible();
    await top(section.getByTestId('token-presets'));
    await both(page, `api-tokens-full-${size}`);

    // The custom selection from the catalogue.
    await section.getByTestId('token-preset-custom').check();
    const custom = section.getByTestId('token-custom');
    await expect(custom.getByTestId('token-perm-mower.drive')).toBeVisible();
    await top(custom);
    await both(page, `api-tokens-custom-${size}`);
    await context.close();
  });
}
