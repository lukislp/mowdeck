/**
 * Phone layout guard (360 and 390 px wide, touch): no page scrolls sideways, wide tables scroll
 * inside their own box, and the top bar stays pinned under the status bar while the page
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
