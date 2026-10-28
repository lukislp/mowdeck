/**
 * Setup tour (ADR 0062): a new account sees the setup card on the mower list and the tour
 * starts by itself; a step can be skipped, the progress follows the user to another browser
 * (it is stored at Identity) and a skipped step can be resumed; the tour completes once every
 * required step is done (from the real state: a mower, its map) or skipped. The first-time hints
 * show to users of the tour until dismissed; an account that was set up before the tour gets one
 * dismissible line only. Screenshots (light and dark, desktop and phone) go to the directory in
 * MOWDECK_E2E_SHOTS when it is set.
 */
import {
  expect,
  test,
  type APIRequestContext,
  type Browser,
  type BrowserContext,
  type Page,
} from '@playwright/test';

import {
  actor,
  createMowerByApi,
  loginByUi,
  registerByApi,
  waitForMap,
  type Actor,
} from './support';

test.describe.configure({ mode: 'serial' });

const ONBOARDING = '/api/v1/identity/me/onboarding';
let owner: Actor = actor('tour');

async function progress(
  request: APIRequestContext,
  who: Actor,
): Promise<{ status: string; skipped_steps: string[]; dismissed_hints: string[] }> {
  const response = await request.get(ONBOARDING, {
    headers: { Authorization: `Bearer ${who.token}` },
  });
  expect(response.status(), await response.text()).toBe(200);
  return (await response.json()) as {
    status: string;
    skipped_steps: string[];
    dismissed_hints: string[];
  };
}

async function signedIn(
  browser: Browser,
  who: Actor,
  viewport = { width: 1280, height: 800 },
): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({
    locale: 'de-DE',
    viewport,
    ...(viewport.width < 600 ? { isMobile: true, hasTouch: true } : {}),
  });
  const page = await context.newPage();
  await loginByUi(page, who);
  return { context, page };
}

test.beforeAll(async ({ request }) => {
  owner = await registerByApi(request, owner);
});

test('a new account sees the tour on the mower list', async ({ browser, request }) => {
  expect((await progress(request, owner)).status).toBe('new');
  const { context, page } = await signedIn(browser, owner);

  const card = page.getByTestId('setup-card');
  await expect(card).toBeVisible();
  await expect(card.getByRole('heading', { name: 'Mammotion-Konto verbinden' })).toBeVisible();
  await expect(card).toContainText('Willkommen bei MowDeck');
  await expect(card.getByTestId('setup-card-meter')).toHaveAttribute('data-done', '0');
  await expect(card.getByTestId('setup-card-action')).toHaveAttribute(
    'href',
    '/settings#linked-accounts',
  );
  // The list stays usable underneath: the card is part of the page, not an overlay.
  await expect(page.getByTestId('mowers-empty')).toBeVisible();
  await expect(page.getByTestId('add-mower').first()).toBeEnabled();
  // Started by itself, and stored at Identity.
  await expect.poll(async () => (await progress(request, owner)).status).toBe('active');
  await shots(page, 'onboarding-card-desktop');

  await card.getByTestId('setup-card-open').click();
  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByTestId('setup-page')).toHaveAttribute('data-status', 'active');
  await expect(page.getByTestId('setup-step-cloud-account-state')).toHaveText('Offen');
  await shots(page, 'onboarding-tour-desktop');
  await context.close();
});

test('a step can be skipped', async ({ browser, request }) => {
  const { context, page } = await signedIn(browser, owner);
  await page.goto('/setup');

  const first = page.getByTestId('setup-step-cloud-account');
  await expect(first.getByRole('button', { expanded: true })).toBeVisible();
  await first.getByTestId('setup-step-cloud-account-skip').click();
  await expect(page.getByTestId('setup-step-cloud-account-state')).toHaveText('Übersprungen');
  // The next open step opens and takes the focus.
  const toggle = page.locator('#setup-step-mower-toggle');
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(toggle).toBeFocused();
  await expect(page.getByTestId('setup-meter')).toHaveAttribute('data-done', '1');
  await expect
    .poll(async () => (await progress(request, owner)).skipped_steps)
    .toEqual(['cloud-account']);
  await context.close();
});

test('the tour resumes where it was, in another browser too', async ({ browser, request }) => {
  // "Später" hides the tour; Settings → Einrichtung brings it back with the same progress.
  const desk = await signedIn(browser, owner);
  await desk.page.getByTestId('setup-card-later').click();
  await expect(desk.page.getByTestId('setup-card')).toBeHidden();
  await expect.poll(async () => (await progress(request, owner)).status).toBe('dismissed');
  await desk.context.close();

  const phone = await signedIn(browser, owner, { width: 390, height: 844 });
  const { page } = phone;
  await expect(page.getByTestId('mowers-empty')).toBeVisible();
  await expect(page.getByTestId('setup-card')).toBeHidden();
  await page.goto('/settings#setup');
  await expect(page.getByTestId('setup-status')).toContainText('Ausgeblendet');
  await page.getByTestId('setup-open').click();
  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByTestId('setup-page')).toHaveAttribute('data-status', 'active');
  await expect(page.getByTestId('setup-step-cloud-account-state')).toHaveText('Übersprungen');
  await expect(page.locator('#setup-step-mower-toggle')).toHaveAttribute('aria-expanded', 'true');
  await shots(page, 'onboarding-tour-phone');

  // A skipped step comes back.
  await page.locator('#setup-step-cloud-account-toggle').click();
  await page.getByTestId('setup-step-cloud-account-resume').click();
  await expect(page.getByTestId('setup-step-cloud-account-state')).toHaveText('Offen');
  await expect.poll(async () => (await progress(request, owner)).skipped_steps).toEqual([]);
  await phone.context.close();
});

test('the tour completes from the real state and the skipped steps', async ({
  browser,
  request,
}) => {
  const mowerId = await createMowerByApi(request, owner, 'Tour mower');
  await waitForMap(request, owner, mowerId);
  const { context, page } = await signedIn(browser, owner);
  await page.goto('/setup');

  // A mower of any kind settles the cloud account step; the map arrived from the gateway.
  for (const key of ['cloud-account', 'mower', 'map']) {
    await expect(page.getByTestId(`setup-step-${key}-state`)).toHaveText('Erledigt');
  }
  await expect(page.getByTestId('setup-complete')).toBeHidden();
  // Skip whatever is still open; optional steps do not hold up the end.
  const steps = page.getByTestId('setup-steps');
  for (let i = 0; i < 10 && !(await page.getByTestId('setup-complete').isVisible()); i += 1) {
    const skip = steps.locator('[data-testid$="-skip"]').first();
    if (!(await skip.isVisible())) break;
    await skip.click();
    await page.waitForTimeout(200);
  }
  const done = page.getByTestId('setup-complete');
  await expect(done).toBeVisible();
  await expect(done.getByRole('heading', { name: 'Alles bereit' })).toBeFocused();
  await shots(page, 'onboarding-complete-desktop');

  await page.getByTestId('setup-finish').click();
  await expect(page).toHaveURL(/\/mowers$/);
  await expect(page.getByTestId(`mower-row-${mowerId}`)).toBeVisible();
  await expect(page.getByTestId('setup-card')).toBeHidden();
  await expect(page.getByTestId('setup-hint')).toBeHidden();
  await expect.poll(async () => (await progress(request, owner)).status).toBe('completed');
  await context.close();
});

test('first-time hints show until dismissed', async ({ browser, request }) => {
  const { context, page } = await signedIn(browser, owner);
  await page.goto('/schedules');
  const hint = page.getByTestId('firsttime-schedules');
  await expect(hint).toBeVisible();
  await expect(hint).toContainText('Zum ersten Mal hier');
  await hint.getByTestId('firsttime-schedules-dismiss').click();
  await expect(hint).toBeHidden();
  await expect
    .poll(async () => (await progress(request, owner)).dismissed_hints)
    .toEqual(['schedules']);
  await page.reload();
  await expect(page.getByTestId('schedule-new')).toBeVisible();
  await page.waitForTimeout(1000);
  await expect(page.getByTestId('firsttime-schedules')).toBeHidden();
  await context.close();
});

test('an account set up before the tour gets one dismissible hint only', async ({
  browser,
  request,
}) => {
  const veteran = await registerByApi(request, actor('veteran'));
  await createMowerByApi(request, veteran, 'Old mower');
  const { context, page } = await signedIn(browser, veteran);

  const hint = page.getByTestId('setup-hint');
  await expect(hint).toBeVisible();
  await expect(page.getByTestId('setup-card')).toBeHidden();
  await shots(page, 'onboarding-hint-desktop');
  await hint.getByTestId('setup-hint-dismiss').click();
  await expect(hint).toBeHidden();
  await expect.poll(async () => (await progress(request, veteran)).status).toBe('dismissed');

  // No first-time hints for an account that never started the tour.
  const mowerId = (
    (await (
      await request.get('/api/v1/fleet/mowers', {
        headers: { Authorization: `Bearer ${veteran.token}` },
      })
    ).json()) as { items: { id: string }[] }
  ).items[0]!.id;
  await page.goto(`/mowers/${mowerId}`);
  await expect(page.getByTestId('mower-health')).toBeVisible();
  await page.waitForTimeout(1000);
  await expect(page.getByTestId('firsttime-mower-map')).toBeHidden();
  await context.close();
});

async function shots(page: Page, name: string): Promise<void> {
  const dir = process.env.MOWDECK_E2E_SHOTS;
  if (!dir) return;
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${dir}/${name}-${scheme}.png`, animations: 'disabled' });
  }
  await page.emulateMedia({ colorScheme: 'light' });
}
