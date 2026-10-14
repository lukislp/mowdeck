/**
 * Phase 2 gate (docs/phases.md), steps 1 to 11 of the design, on a phone (390 x 844):
 * an owner registers, adds a simulated mower in the UI, sees it docked, chooses the areas on the
 * map and starts mowing from the detail page, follows the mower live on the map (step 11: the
 * chevron moves, the driven route grows), sets the blade height while it mows, docks it; a
 * viewer sees the mower and the emergency stop but no start control; a foreign account sees
 * nothing; language, theme and logout work. Runs serially: later steps use earlier state.
 */
import { expect, test, type Browser, type Page } from '@playwright/test';

import { PASSWORD, RUN, actor, loginByUi, registerByApi, watchCsp } from './support';

test.describe.configure({ mode: 'serial' });

const owner = actor('lukas');
const viewer = actor('vera');
const foreign = actor('bob');
const mowerName = `Luba ${RUN}`;
let mowerId = '';
let ownerPage: Page;

async function newPage(browser: Browser): Promise<Page> {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    locale: 'de-DE',
    isMobile: true,
    hasTouch: true,
  });
  return context.newPage();
}

test.afterAll(async () => {
  await ownerPage?.context().close();
});

test('1 owner registers and lands on the mower list (German by default)', async ({ browser }) => {
  ownerPage = await newPage(browser);
  const csp = watchCsp(ownerPage);
  await ownerPage.goto('/');
  await expect(ownerPage).toHaveURL(/\/login/);
  await ownerPage.getByRole('link', { name: 'Neues Konto erstellen' }).click();
  await ownerPage.locator('#account_name').fill(`Lukas home ${RUN}`);
  await ownerPage.locator('#username').fill(owner.username);
  await ownerPage.locator('#password').fill(PASSWORD);
  await ownerPage.getByTestId('register-submit').click();
  await ownerPage.waitForURL(/\/mowers$/);
  await expect(ownerPage.getByRole('heading', { name: 'Mäher', exact: true })).toBeVisible();
  await expect(ownerPage.getByTestId('nav-mowers')).toContainText('Mäher');
  await expect(ownerPage.getByTestId('mowers-empty')).toBeVisible();
  csp.assertNone();
});

test('2 owner registers a simulated mower and sees it docked', async () => {
  const page = ownerPage;
  await page.getByTestId('add-mower').first().click();
  await page.locator('#name').fill(mowerName);
  await expect(page.locator('#connection_kind')).toHaveValue('simulated');
  await page.getByTestId('mower-submit').click();
  await page.waitForURL(/\/mowers$/);
  const row = page.getByRole('link', { name: new RegExp(mowerName) });
  await expect(row).toBeVisible();
  mowerId = (await row.getAttribute('href'))?.split('/').pop() ?? '';
  expect(mowerId).not.toBe('');
  await expect(row).toContainText(/Angedockt|Lädt/, { timeout: 30_000 });
});

test('3 detail shows the readouts and the map; tapping areas selects them for Start', async () => {
  const page = ownerPage;
  await page.getByRole('link', { name: new RegExp(mowerName) }).click();
  await page.waitForURL(new RegExp(`/mowers/${mowerId}$`));
  await expect(page.getByTestId('readout-battery')).toContainText(/\d+\s*%/);
  await expect(page.getByTestId('readout-rtk')).toContainText(/RTK/);
  await expect(page.getByTestId('readout-connection')).toContainText('Simuliert');
  const start = page.getByRole('button', { name: 'Starten' });
  await expect(start).toBeEnabled({ timeout: 60_000 }); // needs the map snapshot

  // The Leaflet map: both areas with their labels and the dock.
  const map = page.getByTestId('mower-map');
  await expect(map).toHaveAttribute('data-ready', 'true');
  await expect(map).toHaveAttribute('data-mode', 'local');
  await expect(map.locator('path[data-area]')).toHaveCount(2);
  await expect(map.getByTestId('map-area-label')).toHaveText(['Front lawn', 'Back lawn']);
  await expect(map.getByRole('img', { name: 'Ladestation' })).toBeVisible();

  // Tap both areas on the map: they are chosen in tap order.
  const front = map.locator('path[data-area="front"]');
  const back = map.locator('path[data-area="back"]');
  await front.click();
  await expect(front).toHaveAttribute('aria-pressed', 'true');
  await back.click();
  await expect(back).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('map-selection')).toContainText('2');

  // Start opens the picker with the map selection already chosen; the stop stays in view.
  await start.click();
  const picker = page.getByTestId('area-picker');
  await expect(picker).toContainText('Front lawn');
  await expect(picker).toContainText('Back lawn');
  await expect(picker.locator('[data-area="front"]')).toHaveAttribute('aria-pressed', 'true');
  await expect(picker.locator('[data-area="front"] .order')).toHaveText('1');
  await expect(picker.locator('[data-area="back"] .order')).toHaveText('2');
  await expect(page.getByTestId('emergency-stop')).toBeInViewport({ ratio: 1 });

  // Rename an area in MowDeck from the picker, then reset it: the mower keeps its own name.
  await picker.locator('[data-rename-area="front"]').click();
  const rename = picker.getByTestId('area-rename-form');
  await expect(rename.getByTestId('area-rename-hint')).toContainText('Front lawn');
  await rename.getByTestId('area-rename-input').fill('Vorgarten');
  await rename.getByTestId('area-rename-save').click();
  // The labels carry the selection order badge (1, 2) while the areas are picked.
  await expect(map.getByTestId('map-area-label')).toHaveText([/^Vorgarten/, /^Back lawn/]);
  await expect(picker.locator('[data-area="front"]')).toContainText('Vorgarten');
  await picker.locator('[data-rename-area="front"]').click();
  await picker.getByTestId('area-rename-reset').click();
  await expect(map.getByTestId('map-area-label')).toHaveText([/^Front lawn/, /^Back lawn/]);
  await expect(picker.locator('[data-area="front"]')).toHaveAttribute('aria-pressed', 'true');
});

test('4 Start uses the areas chosen on the map: executed, then mowing live', async () => {
  const page = ownerPage;
  // The sheet sends exactly the areas chosen on the map, in tap order (checked in step 3).
  const startButton = page.getByTestId('area-start');
  await expect(startButton).toHaveText('Starten (2)');
  await startButton.click();
  await expect(
    page
      .getByRole('status')
      .filter({ hasText: /ausgeführt/ })
      .first(),
  ).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByTestId('mower-state')).toContainText('Mäht', { timeout: 30_000 });
});

test('11 the map follows the mowing mower: the chevron moves, the route grows', async () => {
  const page = ownerPage;
  const map = page.getByTestId('mower-map');
  const marker = map.getByTestId('mower-marker');
  await expect(marker).toBeVisible();
  await expect(marker).toHaveAttribute('data-tone', 'mowing', { timeout: 15_000 });
  const where = async () => {
    const box = await marker.boundingBox();
    return box ? `${Math.round(box.x)},${Math.round(box.y)}` : 'none';
  };
  const first = await where();
  await expect.poll(where, { timeout: 30_000, intervals: [1_000] }).not.toBe(first);
  await expect
    .poll(async () => Number(await map.getAttribute('data-route-points')), { timeout: 30_000 })
    .toBeGreaterThanOrEqual(2);
  await expect(map.locator('path.md-route').first()).toBeAttached();

  // Full-screen map on the phone: the emergency stop stays visible and usable.
  await map.getByTestId('map-expand').click();
  await expect(map.getByTestId('map-expand')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('emergency-stop')).toBeInViewport({ ratio: 1 });
  await expect(marker).toBeInViewport();
  await page.keyboard.press('Escape');
  await expect(map.getByTestId('map-expand')).toHaveAttribute('aria-pressed', 'false');
});

test('5 blade height 50 mm while mowing is executed and confirmed', async () => {
  const page = ownerPage;
  await expect(page.getByTestId('mower-state')).toContainText('Mäht');
  const raise = page.getByRole('button', { name: 'Schnitthöhe erhöhen' });
  const value = page.getByTestId('blade-value');
  await expect(value).toContainText(/\d/);
  for (let i = 0; i < 20; i += 1) {
    const text = (await value.textContent()) ?? '';
    const current = Number.parseInt(text, 10);
    if (current === 50) break;
    await page
      .getByRole('button', {
        name: current < 50 ? 'Schnitthöhe erhöhen' : 'Schnitthöhe verringern',
      })
      .click();
  }
  await expect(value).toContainText('50');
  await expect(raise).toBeVisible();
  await expect(page.getByTestId('readout-blade')).toContainText('50', { timeout: 30_000 });
});

test('6 return to dock: returning or docked', async () => {
  const page = ownerPage;
  await page.getByRole('button', { name: 'Zur Station' }).click();
  await expect(page.getByTestId('mower-state')).toContainText(/Fährt zurück|Angedockt|Lädt/, {
    timeout: 60_000,
  });
});

test('7 viewer sees the mower and the stop, but no start; the stop is executed', async ({
  browser,
}) => {
  const page = ownerPage;
  await page.goto('/settings/users');
  await page.locator('#new-username').fill(viewer.username);
  await page.locator('#new-password').fill(PASSWORD);
  await page.locator('#new-role').selectOption({ label: 'Betrachter' });
  await page.getByLabel('Passwort beim ersten Anmelden ändern').uncheck();
  await page.getByTestId('user-submit').click();
  await expect(page.getByTestId('members')).toContainText(viewer.username);

  const viewerPage = await newPage(browser);
  const csp = watchCsp(viewerPage);
  await loginByUi(viewerPage, viewer);
  await expect(viewerPage.getByRole('link', { name: new RegExp(mowerName) })).toBeVisible();
  await viewerPage.goto(`/mowers/${mowerId}`);
  await expect(viewerPage.getByTestId('readout-battery')).toContainText(/\d+\s*%/);
  const stop = viewerPage.getByTestId('emergency-stop');
  await expect(stop).toBeVisible();
  await expect(viewerPage.getByRole('button', { name: 'Starten' })).toHaveCount(0);
  await expect(viewerPage.getByTestId('controls')).toHaveCount(0);
  await stop.click();
  await expect(stop).toContainText(/gesendet|ausgeführt/);
  await expect(stop).toContainText('ausgeführt', { timeout: 30_000 });
  csp.assertNone();
  await viewerPage.context().close();
});

test('8 a foreign account sees an empty list and not-found for the mower', async ({
  browser,
  request,
}) => {
  await registerByApi(request, foreign);
  const page = await newPage(browser);
  await loginByUi(page, foreign);
  await expect(page.getByTestId('mowers-empty')).toBeVisible();
  await expect(page.getByText(mowerName)).toHaveCount(0);
  await page.goto(`/mowers/${mowerId}`);
  await expect(page.getByTestId('mower-not-found')).toBeVisible();
  await page.context().close();
});

test('9 language switch to English and dark theme', async () => {
  const page = ownerPage;
  await page.goto('/account');
  await page.getByTestId('theme-toggle').getByRole('radio', { name: 'Dunkel' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.getByTestId('language-switch').getByRole('radio', { name: 'English' }).click();
  await expect(page.getByTestId('nav-mowers')).toContainText('Mowers');
  await expect(page.getByRole('heading', { name: 'Account', exact: true })).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});

test('10 logout returns to the login and a reload stays logged out', async () => {
  const page = ownerPage;
  await page.getByTestId('logout').click();
  await page.waitForURL(/\/login/);
  await page.goto('/mowers');
  await expect(page).toHaveURL(/\/login/);
  await page.reload();
  await expect(page).toHaveURL(/\/login/);
});
