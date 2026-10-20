/**
 * One vertical scroller per page (AppShell): on the desktop (from 1024 px) the frame is bound
 * to the viewport, the document never scrolls and the content area `main#main` is the only
 * scroll container; on a phone the document scrolls under the pinned top bar and the content
 * area does not scroll on its own. On both the document ends where the shell ends: nothing
 * (such as a visually hidden label positioned against the viewport, PR #66) reaches below it
 * and leaves a second scrollbar with an empty strip under the app. Runs on a phone, the
 * desktop, in WebKit and in Firefox.
 */
import { expect, test, type Page } from '@playwright/test';

import { RUN, actor, createMowerByApi, loginByUi, registerByApi, type Actor } from './support';

interface Scroll {
  desktop: boolean;
  viewport: number;
  document: number;
  shellBottom: number;
  main: { overflowY: string; scroll: number; client: number };
  culprits: string[];
}

/** The scroll sizes of the document and the content area, and what reaches below the shell. */
async function measure(page: Page): Promise<Scroll> {
  return page.evaluate(() => {
    const root = document.scrollingElement ?? document.documentElement;
    const shell = document.querySelector<HTMLElement>('.md-shell')!;
    const main = document.querySelector<HTMLElement>('main#main')!;
    const shellBottom = Math.ceil(shell.getBoundingClientRect().bottom + window.scrollY);
    // For a readable failure: absolutely positioned boxes below the shell.
    const culprits: string[] = [];
    for (const element of Array.from(document.body.querySelectorAll<HTMLElement>('*'))) {
      const bottom = element.getBoundingClientRect().bottom + window.scrollY;
      if (bottom <= shellBottom + 1 || getComputedStyle(element).position !== 'absolute') continue;
      const cls = typeof element.className === 'string' ? element.className.split(' ')[0] : '';
      culprits.push(
        `${element.tagName.toLowerCase()}${cls ? '.' + cls : ''}@${Math.round(bottom)}`,
      );
    }
    const style = getComputedStyle(main);
    return {
      desktop: window.innerWidth >= 1024,
      viewport: window.innerHeight,
      document: root.scrollHeight,
      shellBottom,
      main: { overflowY: style.overflowY, scroll: main.scrollHeight, client: main.clientHeight },
      culprits: culprits.slice(0, 8),
    };
  });
}

/** Wait until the page has rendered its data: the content size holds still for a moment. */
async function settle(page: Page): Promise<void> {
  await expect(page.locator('header.md-top').first()).toBeVisible();
  let last = '';
  await expect
    .poll(
      async () => {
        const now = await page.evaluate(() => {
          const root = document.scrollingElement ?? document.documentElement;
          const main = document.querySelector('main#main')!;
          return `${root.scrollHeight}/${main.scrollHeight}`;
        });
        const still = now === last;
        last = now;
        return still;
      },
      { intervals: [400] },
    )
    .toBe(true);
}

async function expectOneScroller(page: Page, path: string): Promise<Scroll> {
  const size = await measure(page);
  const why = `${path}: ${JSON.stringify(size)}`;
  // The document ends with the shell: no strip under the app, no stray second scrollbar.
  expect(size.document, why).toBeLessThanOrEqual(size.shellBottom + 1);
  if (size.desktop) {
    // The document never scrolls; only the content area does.
    expect(size.document, why).toBeLessThanOrEqual(size.viewport);
    expect(size.main.overflowY, why).toBe('auto');
  } else {
    // The document scrolls; the content area is no scroll box of its own.
    expect(size.main.overflowY, why).toBe('visible');
  }
  return size;
}

let owner: Actor;
let mowerId = '';

test.beforeAll(async ({ request }, testInfo) => {
  // One user per project: the projects share the stack and the run id.
  owner = await registerByApi(request, actor(`scroll-${testInfo.project.name}`));
  mowerId = await createMowerByApi(request, owner, `Scroll ${RUN} ${testInfo.project.name}`);
});

test('every main page has exactly one vertical scroller', async ({ page }) => {
  await loginByUi(page, owner);
  const paths = [
    '/mowers',
    `/mowers/${mowerId}`,
    `/mowers/${mowerId}/settings`,
    '/reports',
    '/schedules',
    '/fleet',
    '/notifications',
    '/account',
    '/settings',
  ];
  for (const path of paths) {
    await page.goto(path);
    await settle(page);
    await expectOneScroller(page, path);
  }
});

test('Settings, About: the content scrolls to the source code, the page does not', async ({
  page,
}) => {
  await loginByUi(page, owner);
  await page.goto('/settings#about');
  const about = page.getByTestId('settings-section-about');
  const link = about.getByTestId('source-code-link');
  await expect(link).toBeVisible();
  await settle(page);
  await expectOneScroller(page, '/settings#about');
  // To About by its entry in the section index (the hash may land before the sections above
  // have loaded): the content area scrolls there, the document stays where it is.
  await page.getByTestId('settings-index-about').click();
  await expect(link).toBeInViewport();
  const size = await expectOneScroller(page, '/settings#about (scrolled)');
  if (size.desktop) {
    expect(size.main.scroll).toBeGreaterThan(size.main.client);
    expect(
      await page.evaluate(() => document.querySelector('main#main')!.scrollTop),
    ).toBeGreaterThan(0);
    expect(await page.evaluate(() => window.scrollY)).toBe(0);
  }
});

test('the sign-in and registration pages do not scroll past their frame', async ({ page }) => {
  for (const path of ['/login', '/register']) {
    await page.goto(path);
    await expect(page.getByTestId('source-code-link')).toBeVisible();
    const size = await page.evaluate(() => {
      const root = document.scrollingElement ?? document.documentElement;
      const frame = document.querySelector<HTMLElement>('main.column')!.parentElement!;
      return {
        document: root.scrollHeight,
        frameBottom: Math.ceil(frame.getBoundingClientRect().bottom + window.scrollY),
      };
    });
    expect(size.document, `${path}: ${JSON.stringify(size)}`).toBeLessThanOrEqual(
      size.frameBottom + 1,
    );
  }
});
