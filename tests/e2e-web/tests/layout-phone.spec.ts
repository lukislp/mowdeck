/**
 * Phone layout guard (360 and 390 px wide, touch): no page scrolls sideways, wide tables scroll
 * inside their own box, the health page with a mower's own lifetime counters (long readout
 * labels) and the other mower subpages fit 320 to 430 px, and the top bar stays pinned under the status bar while the page
 * scrolls (the document does not rubber-band, so a drag cannot pull the header out of view).
 */
import { expect, test, type Page } from '@playwright/test';

import { RUN, actor, createMowerByApi, loginByUi, registerByApi, watchCsp } from './support';

const owner = actor('layout');

/** Elements reaching past the right edge of the viewport (for a readable failure). */
async function overflowCulprits(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const width = document.documentElement.clientWidth;
    const found: string[] = [];
    for (const element of Array.from(document.body.querySelectorAll<HTMLElement>('*'))) {
      const box = element.getBoundingClientRect();
      if (box.width === 0 || box.right <= width + 1) continue;
      // Fixed bars only follow a wider page; content inside a scroll box of its own does not
      // widen the page.
      let parent: HTMLElement | null = element;
      let skip = false;
      while (parent && parent !== document.body) {
        const style = getComputedStyle(parent);
        if (style.position === 'fixed') {
          skip = true;
          break;
        }
        if (parent !== element && style.overflowX !== 'visible') {
          skip = parent.getBoundingClientRect().right <= width + 1;
          break;
        }
        parent = parent.parentElement;
      }
      if (skip) continue;
      const name = element.tagName.toLowerCase();
      const cls = typeof element.className === 'string' ? element.className.split(' ')[0] : '';
      found.push(`${name}${cls ? '.' + cls : ''} right=${Math.round(box.right)}`);
    }
    return found.slice(0, 8);
  });
}

async function expectNoSidewaysScroll(page: Page, path: string): Promise<void> {
  const size = await page.evaluate(() => {
    const root = document.scrollingElement ?? document.documentElement;
    return { scroll: root.scrollWidth, client: root.clientWidth };
  });
  const culprits = size.scroll > size.client ? await overflowCulprits(page) : [];
  expect(size.scroll, `${path} scrolls sideways: ${culprits.join(', ')}`).toBeLessThanOrEqual(
    size.client,
  );
}

/** Wait until the page has rendered its data: the document size holds still for a moment. */
async function settle(page: Page): Promise<void> {
  await expect(page.locator('header.md-top').first()).toBeVisible();
  let last = '';
  await expect
    .poll(
      async () => {
        const now = await page.evaluate(() => {
          const root = document.scrollingElement ?? document.documentElement;
          return `${root.scrollWidth}x${root.scrollHeight}`;
        });
        const still = now === last;
        last = now;
        return still;
      },
      { intervals: [400] },
    )
    .toBe(true);
}

/** The page's top bar sits at the top edge (below the safe area, 0 in a browser). */
async function headerTop(page: Page): Promise<number> {
  const box = await page.locator('header.md-top').first().boundingBox();
  expect(box).not.toBeNull();
  return Math.round(box!.y);
}

let mowerId = '';

test.beforeAll(async ({ request }) => {
  const who = await registerByApi(request, owner);
  mowerId = await createMowerByApi(request, who, `Layout ${RUN}`);
});

for (const width of [360, 390]) {
  test(`no page scrolls sideways at ${width} px; wide tables scroll in their box`, async ({
    browser,
  }) => {
    const context = await browser.newContext({
      viewport: { width, height: 780 },
      locale: 'de-DE',
      isMobile: true,
      hasTouch: true,
    });
    const page = await context.newPage();
    const csp = watchCsp(page);
    await loginByUi(page, owner);

    const paths = [
      '/mowers',
      `/mowers/${mowerId}`,
      `/mowers/${mowerId}/health`,
      `/mowers/${mowerId}/settings`,
      `/mowers/${mowerId}/drive`,
      '/mowers/new',
      '/reports',
      '/schedules',
      '/schedules/new',
      '/fleet',
      '/fleet/sites',
      '/fleet/groups',
      '/fleet/base-stations',
      '/notifications',
      '/settings',
      '/account',
    ];
    for (const path of paths) {
      await page.goto(path);
      // The report's per-mower table is the widest content of the app: wait until it is there.
      if (path === '/reports')
        await expect(page.getByTestId('report-rows').locator('table')).toBeVisible();
      await settle(page);
      await expectNoSidewaysScroll(page, path);
    }

    // The per-mower report table is wider than the phone: it scrolls inside its own box.
    await page.goto('/reports');
    const box = page.getByTestId('report-rows').locator('.md-table-scroll');
    await expect(box.locator('table')).toBeVisible();
    const sizes = await box.evaluate((element) => ({
      scroll: element.scrollWidth,
      client: element.clientWidth,
      right: element.getBoundingClientRect().right,
    }));
    expect(sizes.scroll).toBeGreaterThan(sizes.client);
    expect(sizes.right).toBeLessThanOrEqual(width);
    await box.evaluate((element) => element.scrollBy({ left: 400 }));
    await expect.poll(() => box.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
    await expectNoSidewaysScroll(page, '/reports (table scrolled)');

    csp.assertNone();
    await context.close();
  });
}

/**
 * A health answer as a mower with its own lifetime counters gives it: every readout then carries
 * its long "(Mäher, gesamt)" / "(seit MowDeck)" label, plus a long error code.
 */
function deviceHealth(mowerId: string): Record<string, unknown> {
  const now = new Date().toISOString();
  return {
    mower_id: mowerId,
    battery_cycles: 41,
    battery_cycles_source: 'device',
    battery_cycles_tracked: 12,
    blade_changed_at: now,
    blade_hours: 18.4,
    blade_hours_since_change: 6.2,
    blade_hours_threshold: 100,
    blade_hours_threshold_custom: false,
    blade_hours_threshold_effective: 100,
    blade_hours_threshold_source: 'device',
    blade_source: 'device',
    charge_sessions: 97,
    device_battery_cycles: 412,
    device_blade_hours: 84.25,
    device_blade_warn_hours: 100,
    device_distance_m: 1_234_567,
    device_mowing_hours: 1234.5,
    distance_m: 45_678,
    errors: { blade_motor_overcurrent_protection_triggered_repeatedly_0123456789: 3, '1005': 1 },
    mowing_hours: 56.7,
    since: now,
    updated_at: now,
  };
}

/** Thirty days with mowing, so the bar strips render. */
function dailyDays(): Record<string, unknown>[] {
  const days: Record<string, unknown>[] = [];
  for (let i = 29; i >= 0; i--) {
    const day = new Date(Date.now() - i * 86_400_000).toISOString().slice(0, 10);
    days.push({ day, blade_hours: (i % 5) * 0.4, distance_m: (i % 7) * 310, mowing_hours: 1 });
  }
  return days;
}

async function expectFits(page: Page, path: string): Promise<void> {
  await expectNoSidewaysScroll(page, path);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
    `${path} is wider than the phone`,
  ).toBe(true);
}

for (const width of [320, 375, 390, 430]) {
  test(`the health page with a mower's own counters and the mower subpages fit ${width} px`, async ({
    browser,
  }) => {
    const context = await browser.newContext({
      viewport: { width, height: 780 },
      locale: 'de-DE',
      isMobile: true,
      hasTouch: true,
    });
    const page = await context.newPage();
    const csp = watchCsp(page);
    await loginByUi(page, owner);

    const json = (body: unknown) => ({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
    await page.route(/\/api\/v1\/telemetry\/mowers\/[^/]+\/health(\?.*)?$/, (route) =>
      route.fulfill(json(deviceHealth(mowerId))),
    );
    await page.route(/\/api\/v1\/telemetry\/mowers\/[^/]+\/health\/daily(\?.*)?$/, (route) =>
      route.fulfill(json({ days: dailyDays() })),
    );
    await page.route(
      /\/api\/v1\/telemetry\/mowers\/[^/]+\/health\/blade-changes(\?.*)?$/,
      (route) =>
        route.fulfill(
          json({
            items: [
              {
                id: '00000000-0000-4000-8000-000000000001',
                changed_at: new Date().toISOString(),
                changed_by: null,
                note: 'Messerwechsel-LB2AWDX4G-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ',
              },
            ],
          }),
        ),
    );

    const health = `/mowers/${mowerId}/health`;
    await page.goto(health);
    // The long "(seit MowDeck)" labels and the bar strips are there before the width is measured.
    await expect(page.getByTestId('health-mowing-hours-mowdeck')).toBeVisible();
    await expect(page.getByTestId('health-blade-since-mowdeck')).toBeVisible();
    await expect(page.getByTestId('daily-blade')).toBeVisible();
    await settle(page);
    await expectFits(page, health);

    for (const path of [
      `/mowers/${mowerId}`,
      `/mowers/${mowerId}/settings`,
      `/mowers/${mowerId}/drive`,
      `/mowers/${mowerId}/map`,
    ]) {
      await page.goto(path);
      await settle(page);
      await expectFits(page, path);
    }

    csp.assertNone();
    await context.close();
  });
}

test('the top bar stays pinned while the page scrolls; the document does not rubber-band', async ({
  browser,
}) => {
  const context = await browser.newContext({
    viewport: { width: 390, height: 640 },
    locale: 'de-DE',
    isMobile: true,
    hasTouch: true,
  });
  const page = await context.newPage();
  await loginByUi(page, owner);

  // No overscroll bounce or pull-to-refresh on the page itself (iOS rubber band, Chrome glow).
  expect(
    await page.evaluate(() => getComputedStyle(document.documentElement).overscrollBehaviorY),
  ).toBe('none');

  for (const path of [`/mowers/${mowerId}/settings`, `/mowers/${mowerId}`, '/settings']) {
    await page.goto(path);
    await settle(page);
    const header = page.locator('header.md-top').first();
    expect(await headerTop(page)).toBe(0);
    for (const where of ['middle', 'end'] as const) {
      const scrolled = await page.evaluate((to) => {
        const height = document.documentElement.scrollHeight;
        window.scrollTo({ top: to === 'end' ? height : height / 2, behavior: 'instant' });
        return window.scrollY;
      }, where);
      expect(scrolled, `${path} scrolls (${where})`).toBeGreaterThan(0);
      expect(await headerTop(page), `${path}: top bar at the ${where}`).toBe(0);
      await expect(header).toBeInViewport({ ratio: 1 });
    }
  }
  await context.close();
});
