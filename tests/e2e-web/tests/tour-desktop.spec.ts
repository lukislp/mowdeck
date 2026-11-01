/**
 * The guided tour (ADR 0073) on the desktop: it starts by itself once for a new account and
 * leads it to adding the first mower; Esc leaves it for good (stored per user in Identity); it
 * never starts by itself for an account that already has a mower; Settings → Setup starts it
 * again; the arrow keys walk through the mower screens with the focus kept in the card; a step
 * whose element is missing on the page is passed over instead of blocking the tour.
 * Screenshots (light and dark) go to MOWDECK_E2E_SHOTS when it is set.
 */
import { expect, test } from '@playwright/test';

import {
  actor,
  capabilities,
  createMowerByApi,
  loginByUi,
  registerByApi,
  waitForMap,
  watchCsp,
} from './support';
import { FRESH_BROWSER, expectStep, focusInCard, shots, storedHints, tourOf } from './tour';

test.use({ storageState: FRESH_BROWSER });

test('starts once for a new account and leads to adding the first mower', async ({
  page,
  request,
}) => {
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('tour-new'));
  await loginByUi(page, owner);

  const { card, next } = tourOf(page);
  await expectStep(page, 'welcome', false);
  await expect(card).toHaveAttribute('aria-modal', 'true');
  await expect(card).toHaveAccessibleName('Willkommen bei MowDeck');
  await expect(page.getByTestId('tour-progress')).toContainText('Schritt 1 von 3');
  await expect(next).toBeFocused();
  // The page underneath does not take clicks while the tour runs.
  await expect(page.getByTestId('tour').locator('.catch')).toBeVisible();
  await shots(page, 'tour-welcome-desktop');

  await page.keyboard.press('ArrowRight');
  await expectStep(page, 'restart');
  await expect(page).toHaveURL(/\/settings/);
  await expect(page.getByTestId('tour-start')).toBeInViewport();

  await page.keyboard.press('ArrowRight');
  await expectStep(page, 'add-mower');
  await expect(page).toHaveURL(/\/mowers$/);
  await expect(page.getByTestId('tour-live')).toContainText('Schritt 3 von 3');
  await shots(page, 'tour-add-mower-desktop');
  await expect(next).toHaveText('Mäher hinzufügen');
  await next.click();
  await expect(page).toHaveURL(/\/mowers\/new$/);
  await expect(page.getByTestId('tour')).toHaveCount(0);

  await expect.poll(() => storedHints(request, owner)).toContain('tour-done');
  // Once: never again by itself, not after a reload and not on the next sign-in.
  await page.goto('/mowers');
  await expect(page.getByTestId('mowers-empty')).toBeVisible();
  await page.waitForTimeout(1_500);
  await expect(page.getByTestId('tour')).toHaveCount(0);
  csp.assertNone();
});

test('Esc leaves the tour for good; Settings → Setup starts it again', async ({
  page,
  request,
}) => {
  const owner = await registerByApi(request, actor('tour-esc'));
  await loginByUi(page, owner);
  await expectStep(page, 'welcome', false);
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('tour')).toHaveCount(0);
  await expect.poll(() => storedHints(request, owner)).toContain('tour-skipped');
  // Escape returned the page as it was: the setup card of a new account is still there.
  await expect(page.getByTestId('setup-card')).toBeVisible();

  await page.reload();
  await expect(page.getByTestId('mowers-empty')).toBeVisible();
  await page.waitForTimeout(1_500);
  await expect(page.getByTestId('tour')).toHaveCount(0);

  await page.goto('/settings#setup');
  await expect(page.getByTestId('tour-status')).toContainText('Beendet');
  await page.getByTestId('tour-start').click();
  await expectStep(page, 'welcome', false);
  await page.getByTestId('tour-skip').click();
  await expect(page.getByTestId('tour')).toHaveCount(0);
  await expect(page.getByTestId('tour-start')).toBeFocused();
});

test('arrow keys walk the mower screens; a missing element is passed over', async ({
  page,
  request,
}) => {
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('tour-keys'));
  const mowerId = await createMowerByApi(request, owner, `Tour ${owner.username}`);
  await waitForMap(request, owner, mowerId);
  const canDrive = (await capabilities(request, owner, mowerId))?.manual_drive === true;
  // The schedule list cannot be loaded: its step has nothing to point at.
  await page.route(/\/api\/v1\/scheduler\/schedules(\?.*)?$/, (route) =>
    route.request().method() === 'GET'
      ? route.fulfill({
          status: 503,
          contentType: 'application/problem+json',
          body: JSON.stringify({ type: 'about:blank', title: 'Unavailable', status: 503 }),
        })
      : route.continue(),
  );

  await loginByUi(page, owner);
  // An account that already has a mower is not bombarded: no tour by itself.
  await expect(page.getByTestId(`mower-row-${mowerId}`)).toBeVisible();
  await page.waitForTimeout(1_500);
  await expect(page.getByTestId('tour')).toHaveCount(0);

  await page.goto('/settings#setup');
  await expect(page.getByTestId('tour-status')).toContainText('Noch nicht gesehen');
  await page.getByTestId('tour-start').click();
  await expectStep(page, 'welcome', false);
  const total = canDrive ? 10 : 9;
  await expect(page.getByTestId('tour-progress')).toContainText(`Schritt 1 von ${total}`);
  // Back is not offered on the first step; Tab stays inside the card.
  await expect(page.getByTestId('tour-back')).toHaveCount(0);
  for (let i = 0; i < 4; i++) {
    await page.keyboard.press('Tab');
    expect(await focusInCard(page)).toBe(true);
  }

  await page.keyboard.press('ArrowRight');
  await expectStep(page, 'overview');
  await expect(page).toHaveURL(/\/mowers$/);
  await page.keyboard.press('ArrowRight');
  await expectStep(page, 'map');
  await expect(page).toHaveURL(new RegExp(`/mowers/${mowerId}$`));
  await shots(page, 'tour-map-desktop');
  await page.keyboard.press('ArrowLeft');
  await expectStep(page, 'overview');
  await page.keyboard.press('ArrowRight');
  await expectStep(page, 'map');
  await page.keyboard.press('ArrowRight');
  await expectStep(page, 'commands');
  // The emergency stop is inside the framed command bar.
  await expect(page.locator('[data-tour="commands"]')).toBeInViewport();
  await shots(page, 'tour-commands-desktop');
  if (canDrive) {
    await page.keyboard.press('ArrowRight');
    await expectStep(page, 'spot');
  }
  await page.keyboard.press('ArrowRight');
  await expectStep(page, 'zones');

  // Schedules: the page shows its error instead of the list, the tour moves on by itself.
  await page.keyboard.press('ArrowRight');
  await expectStep(page, 'inbox');
  await expect(page).toHaveURL(/\/notifications$/);
  await expect(page.getByTestId('tour-progress')).toContainText(`von ${total - 1}`);
  // Back skips the missed step as well.
  await page.keyboard.press('ArrowLeft');
  await expectStep(page, 'zones');
  await page.keyboard.press('ArrowRight');
  await expectStep(page, 'inbox');

  await page.keyboard.press('ArrowRight');
  await expectStep(page, 'settings');
  await page.keyboard.press('ArrowRight');
  await expectStep(page, 'restart');
  await expect(tourOf(page).next).toHaveText('Fertig');
  await page.keyboard.press('Enter');
  await expect(page.getByTestId('tour')).toHaveCount(0);
  await expect.poll(() => storedHints(request, owner)).toContain('tour-done');
  await expect(page.getByTestId('tour-status')).toContainText('Abgeschlossen');
  csp.assertNone();
});
