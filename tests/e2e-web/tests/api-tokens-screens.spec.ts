/**
 * Review screenshots of the API tokens section (run: pnpm --filter e2e-web screens): the list
 * with a view-only and a full Home Assistant token and three revoked ones folded away, the
 * revoked ones opened (with "Widerrufene entfernen" and a remove button per row), the
 * confirmation sheet before removing them, the presets with the full one chosen and its warning, the custom
 * selection, and the one-time secret right after creating a token (as the page lands, no
 * scrolling by the test), light and dark, at phone (390 x 844) and desktop (1280 x 800) width,
 * written to apps/web/screenshots/api-tokens-*.png.
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
  revoke = false,
): Promise<void> {
  const base = `/api/v1/identity/accounts/${owner.accountId}/api-tokens`;
  const headers = { Authorization: `Bearer ${owner.token}` };
  const response = await request.post(base, {
    headers,
    data: { name, grants: [{ level: { type: 'account', id: owner.accountId }, permissions }] },
  });
  expect(response.status(), await response.text()).toBe(201);
  if (!revoke) return;
  const { id } = (await response.json()) as { id: string };
  const revoked = await request.delete(`${base}/${id}`, { headers });
  expect(revoked.ok(), await revoked.text()).toBe(true);
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
    for (const name of ['Testskript', 'Altes Handy', 'Home Assistant (alt)']) {
      await tokenByApi(request, owner, name, ['mower.view', 'mower.control'], true);
    }
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

    // The revoked tokens opened.
    const folded = section.getByTestId('token-revoked');
    await expect(folded.locator('summary')).toContainText('(3)');
    await folded.locator('summary').click();
    await expect(folded.getByTestId('token-revoked-row')).toHaveCount(3);
    await top(folded);
    await both(page, `api-tokens-revoked-${size}`);

    // The confirmation before every revoked token is deleted for good.
    await folded.getByTestId('token-purge-all').click();
    const confirm = page.getByTestId('token-purge-confirm');
    await expect(confirm).toHaveAttribute('aria-label', '3 widerrufene Tokens entfernen?');
    await both(page, `api-tokens-purge-${size}`);
    await confirm.getByRole('button', { name: 'Abbrechen' }).click();
    await folded.locator('summary').click();

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

    // Right after creating a token from the bottom of the form: the secret as the page lands.
    await section.getByTestId('token-preset-basic').check();
    await section.locator('#token-name').fill('Home Assistant Garten');
    await section.getByTestId('token-create').scrollIntoViewIfNeeded();
    await section.getByTestId('token-create').click();
    const secret = section.getByTestId('token-secret');
    await expect(secret).toBeFocused();
    await expect(secret.getByTestId('token-secret-value')).toBeInViewport({ ratio: 1 });
    await page.waitForTimeout(600); // smooth scroll settles
    await both(page, `api-tokens-secret-${size}`);
    await context.close();
  });
}
