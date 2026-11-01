/**
 * The guided tour (ADR 0073) on a 390 px phone: the card docks to the edge away from the framed
 * element, never covers it and never widens the page; it starts by itself once for a new
 * account; Settings → Setup starts it again; Esc leaves it. Screenshots (light and dark) go to
 * MOWDECK_E2E_SHOTS when it is set.
 */
import { expect, test, type Page } from '@playwright/test';

import { actor, createMowerByApi, loginByUi, registerByApi, waitForMap } from './support';
import { FRESH_BROWSER, expectStep, shots, storedHints } from './tour';

test.use({ storageState: FRESH_BROWSER });

async function boxes(page: Page) {
  const card = await page.getByTestId('tour-card').boundingBox();
  const frame = await page.getByTestId('tour-frame').boundingBox();
  const width = await page.evaluate(() => document.documentElement.scrollWidth);
  return { card: card!, frame: frame!, width };
}

test('starts once for a new account on the phone and leaves with Esc', async ({
  page,
  request,
}) => {
  const owner = await registerByApi(request, actor('tour-phone-new'));
  await loginByUi(page, owner);
  await expectStep(page, 'welcome', false);
  const card = await page.getByTestId('tour-card').boundingBox();
  expect(card!.x).toBeGreaterThanOrEqual(0);
  expect(card!.x + card!.width).toBeLessThanOrEqual(390);
  await shots(page, 'tour-welcome-phone');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('tour')).toHaveCount(0);
  await expect.poll(() => storedHints(request, owner)).toContain('tour-skipped');
  await page.reload();
  await expect(page.getByTestId('mowers-empty')).toBeVisible();
  await page.waitForTimeout(1_500);
  await expect(page.getByTestId('tour')).toHaveCount(0);
});

test('docks the card away from the framed element on the mower screens', async ({
  page,
  request,
}) => {
  const owner = await registerByApi(request, actor('tour-phone'));
  const mowerId = await createMowerByApi(request, owner, `Tour ${owner.username}`);
  await waitForMap(request, owner, mowerId);
  await loginByUi(page, owner);

  await page.goto('/settings#setup');
  await expect(page.getByTestId('tour-status')).toContainText('Noch nicht gesehen');
  await page.getByTestId('tour-start').click();
  await expectStep(page, 'welcome', false);
  await page.getByTestId('tour-next').click();
  await expectStep(page, 'overview');
  await page.getByTestId('tour-next').click();
  await expectStep(page, 'map');
  await expect(page).toHaveURL(new RegExp(`/mowers/${mowerId}$`));
  await page.waitForTimeout(600); // the frame glides into place
  let seen = await boxes(page);
  expect(seen.width).toBeLessThanOrEqual(390);
  // The map is at the top: the card docks to the bottom edge.
  expect(seen.card.y).toBeGreaterThan(seen.frame.y);
  await shots(page, 'tour-map-phone');

  await page.getByTestId('tour-next').click();
  await expectStep(page, 'commands');
  await page.waitForTimeout(600);
  seen = await boxes(page);
  expect(seen.width).toBeLessThanOrEqual(390);
  // The command bar is at the bottom: the card docks to the top and leaves the bar visible.
  expect(seen.card.y + seen.card.height).toBeLessThanOrEqual(seen.frame.y);
  await shots(page, 'tour-commands-phone');

  await page.getByTestId('tour-back').click();
  await expectStep(page, 'map');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('tour')).toHaveCount(0);
  // The mower page is usable again: its emergency stop is there and enabled.
  await expect(page.getByTestId('emergency-stop')).toBeEnabled();
});
