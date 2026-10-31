/**
 * "Was ist neu": a version bump shows the sheet once, with the changes since the version this
 * device saw, and Settings → Neuigkeiten keeps the history. The build's version and `whatsNew`
 * (/config.json) and notes (/whats-new.json, written by the release build) are simulated with routes, so the
 * test needs no release image. On a mower's page the sheet stops above the emergency stop (phone
 * and desktop) and never covers it. Screenshots (light and dark, phone and desktop) are written to
 * the directory in MOWDECK_E2E_SHOTS when it is set.
 */
import { expect, test, type BrowserContext, type Locator, type Page } from '@playwright/test';

import { RUN, actor, createMowerByApi, loginByUi, registerByApi } from './support';

const SEEN_KEY = 'mowdeck.whats-new-seen';
const notes = {
  format: 1,
  releases: [
    {
      version: '0.31.0',
      date: '2026-11-02',
      entries: [
        {
          id: 'pr-3',
          kind: 'feature',
          en: 'Linked accounts in one place.',
          de: 'Verknüpfte Konten an einem Ort.',
          href: '/settings#linked-accounts',
        },
      ],
    },
    {
      version: '0.30.0',
      date: '2026-10-30',
      entries: [
        { id: 'pr-2', kind: 'feature', en: 'Sign in with a passkey.', de: 'Anmelden mit Passkey.' },
        { id: 'pr-4', kind: 'fix', en: 'Only an English commit subject' },
      ],
    },
    {
      version: '0.28.5',
      date: '2026-10-27',
      entries: [{ id: 'pr-1', kind: 'fix', en: 'Old fix.', de: 'Alter Fix.' }],
    },
  ],
};

test.describe.configure({ mode: 'serial' });

const user = actor('whatsnew');
let build = '0.30.0';
let mowerId = '';

async function simulate(context: BrowserContext): Promise<void> {
  await context.route('**/config.json', async (route) => {
    const response = await route.fetch();
    const config = (await response.json()) as {
      source?: Record<string, unknown>;
      whatsNew?: boolean;
    };
    config.source = { ...config.source, version: build };
    // A release image ships the notes and says so; the test image has none of its own.
    config.whatsNew = true;
    await route.fulfill({ response, json: config });
  });
  await context.route('**/whats-new.json', (route) => route.fulfill({ json: notes }));
  // Seed "last seen" once per tab; a reload keeps what the app stored.
  await context.addInitScript((key) => {
    if (!sessionStorage.getItem('seeded')) {
      localStorage.setItem(key, '0.28.5');
      sessionStorage.setItem('seeded', '1');
    }
  }, SEEN_KEY);
}

test.beforeAll(async ({ request }) => {
  const who = await registerByApi(request, user);
  mowerId = await createMowerByApi(request, who, `What's new ${RUN}`);
});

test('the sheet appears once after a version change', async ({ browser }) => {
  const context = await browser.newContext({
    locale: 'de-DE',
    viewport: { width: 1280, height: 800 },
  });
  await simulate(context);
  const page = await context.newPage();
  await loginByUi(page, user);
  await page.goto('/mowers');

  const sheet = page.getByTestId('whats-new-sheet');
  await expect(sheet).toBeVisible();
  await expect(sheet.getByRole('heading', { name: 'Was ist neu in v0.30.0' })).toBeVisible();
  // 0.31.0 is newer than the running build and 0.28.5 was seen: only 0.30.0 is listed.
  await expect(sheet.getByTestId('whats-new-entry')).toHaveCount(2);
  await expect(sheet).toContainText('Anmelden mit Passkey.');
  await expect(sheet).toContainText('Only an English commit subject');
  await expect(sheet).not.toContainText('Alter Fix.');
  await shots(page, 'whats-new-desktop');

  await sheet.getByRole('button', { name: 'Verstanden' }).click();
  await expect(sheet).toBeHidden();
  expect(await page.evaluate((key) => localStorage.getItem(key), SEEN_KEY)).toBe('0.30.0');

  await page.reload();
  // The user has a mower (for the mower-page check): the list and the main region both match.
  await expect(page.getByTestId('mower-list').or(page.getByRole('main')).first()).toBeVisible();
  await page.waitForTimeout(1000);
  await expect(page.getByTestId('whats-new-sheet')).toBeHidden();
  await context.close();
});

test('a later version shows only its own changes, the link opens the page and closes the sheet', async ({
  browser,
}) => {
  build = '0.31.0';
  const context = await browser.newContext({
    locale: 'de-DE',
    viewport: { width: 1280, height: 800 },
  });
  await simulate(context);
  const page = await context.newPage();
  await loginByUi(page, user);
  await page.evaluate((key) => localStorage.setItem(key, '0.30.0'), SEEN_KEY);
  await page.goto('/mowers');

  const sheet = page.getByTestId('whats-new-sheet');
  await expect(sheet.getByRole('heading', { name: 'Was ist neu in v0.31.0' })).toBeVisible();
  await expect(sheet.getByTestId('whats-new-entry')).toHaveCount(1);
  await sheet.getByRole('link', { name: /Jetzt ausprobieren/ }).click();
  await expect(page).toHaveURL(/\/settings#linked-accounts$/);
  await expect(sheet).toBeHidden();
  expect(await page.evaluate((key) => localStorage.getItem(key), SEEN_KEY)).toBe('0.31.0');
  await context.close();
});

test('Settings keeps the history', async ({ browser }) => {
  const context = await browser.newContext({
    locale: 'de-DE',
    viewport: { width: 1280, height: 800 },
  });
  await simulate(context);
  const page = await context.newPage();
  await loginByUi(page, user);
  await page.goto('/settings#whats-new');

  const history = page.getByTestId('whats-new-history');
  await expect(history).toBeVisible();
  await expect(history.getByTestId('whats-new-entry')).toHaveCount(4);
  await expect(history).toContainText('Alter Fix.');
  await expect(page.getByTestId('settings-index-whats-new')).toContainText('Neuigkeiten');
  await shots(page, 'whats-new-settings-desktop');
  await context.close();
});

test('phone width', async ({ browser }) => {
  build = '0.31.0';
  const context = await browser.newContext({
    locale: 'de-DE',
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
  });
  await simulate(context);
  const page = await context.newPage();
  await loginByUi(page, user);
  await page.evaluate((key) => localStorage.setItem(key, '0.28.5'), SEEN_KEY);
  await page.goto('/mowers');
  const sheet = page.getByTestId('whats-new-sheet');
  await expect(sheet).toBeVisible();
  const box = await sheet.boundingBox();
  expect(box?.x ?? 1).toBeGreaterThanOrEqual(0);
  expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(390);
  await shots(page, 'whats-new-phone');
  await context.close();
});

/** The two boxes share no pixel (a one-pixel touch of the edges is allowed). */
async function expectApart(a: Locator, b: Locator): Promise<void> {
  const one = await a.boundingBox();
  const two = await b.boundingBox();
  expect(one).not.toBeNull();
  expect(two).not.toBeNull();
  const overlapX = Math.min(one!.x + one!.width, two!.x + two!.width) - Math.max(one!.x, two!.x);
  const overlapY = Math.min(one!.y + one!.height, two!.y + two!.height) - Math.max(one!.y, two!.y);
  expect(
    overlapX <= 1 || overlapY <= 1,
    `overlap ${JSON.stringify(one)} / ${JSON.stringify(two)}`,
  ).toBe(true);
}

/** The emergency stop is the topmost element at its centre: nothing floats over it. */
async function expectStopOnTop(page: Page): Promise<void> {
  const stop = page.getByTestId('emergency-stop').first();
  await expect(stop).toBeVisible();
  const box = (await stop.boundingBox())!;
  const onTop = await page.evaluate(
    ({ x, y }) => {
      const hit = document.elementFromPoint(x, y);
      return Boolean(hit?.closest('[data-testid="emergency-stop"]'));
    },
    { x: box.x + box.width / 2, y: box.y + box.height / 2 },
  );
  expect(onTop).toBe(true);
}

for (const [label, viewport] of [
  ['phone', { width: 390, height: 844 }],
  ['desktop', { width: 1280, height: 800 }],
] as const) {
  test(`on a mower's page the sheet stays clear of the emergency stop (${label})`, async ({
    browser,
  }) => {
    build = '0.31.0';
    const phone = label === 'phone';
    const context = await browser.newContext({
      locale: 'de-DE',
      viewport,
      isMobile: phone,
      hasTouch: phone,
    });
    await simulate(context);
    const page = await context.newPage();
    await loginByUi(page, user);
    await page.evaluate((key) => localStorage.setItem(key, '0.28.5'), SEEN_KEY);
    await page.goto(`/mowers/${mowerId}`);

    const sheet = page.getByTestId('whats-new-sheet');
    await expect(sheet).toBeVisible();
    await expect(page.getByTestId('emergency-stop').first()).toBeVisible();
    // The sheet's rise animation ends before the boxes are measured.
    await page.waitForTimeout(400);
    await expectApart(sheet, page.locator('.md-command-bar').first());
    await expectStopOnTop(page);
    // The whole sheet is on screen: its footer ("Verstanden") can be reached.
    await expect(sheet.getByRole('button', { name: 'Verstanden' })).toBeInViewport();
    await shots(page, `whats-new-mower-${label}`);

    await sheet.getByRole('button', { name: 'Verstanden' }).click();
    await expect(sheet).toBeHidden();
    await context.close();
  });
}

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
