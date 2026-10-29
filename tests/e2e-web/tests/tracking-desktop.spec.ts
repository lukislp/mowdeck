/**
 * Theft tracking mode (ADR 0058, desktop): with the simulator, tracking started by hand (the
 * same path an alarm takes) records positions while the mower drives, the owner creates a share
 * link, the link shows the live page WITHOUT a login (and nothing else), the report downloads as
 * a PDF, and "false alarm" ends the session. Both maps lie on the base map Telemetry serves
 * around the track (here the stack's fake aerial photo), never on an empty plane. The view stays
 * where the user put it (a poll never refits it) and never zooms out past the served area; the
 * share page is checked on a 390 px touch phone with a real two finger pinch.
 */
import { devices, expect, test, type Locator, type Page } from '@playwright/test';

import {
  RUN,
  actor,
  commandByApi,
  createMowerByApi,
  loginByUi,
  registerByApi,
  waitForMap,
  watchCsp,
} from './support';

/** The map's current zoom (TrackMap writes it to `data-zoom`). */
async function zoomOf(map: Locator): Promise<number> {
  return Number(await map.getAttribute('data-zoom'));
}

/** Wait until the browser painted twice (Svelte applied a poll answer). */
async function settled(page: Page): Promise<void> {
  await page.evaluate(
    () => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))),
  );
}

/** A two finger pinch (spread < 0: zoom out) in the middle of `target`, as raw touch events. */
async function pinch(page: Page, target: Locator, spread: number): Promise<void> {
  const box = (await target.boundingBox())!;
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  const fingers = (gap: number) => [
    { x: x - gap, y, id: 1 },
    { x: x + gap, y, id: 2 },
  ];
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: fingers(120) });
  for (let step = 1; step <= 10; step += 1) {
    await cdp.send('Input.dispatchTouchEvent', {
      type: 'touchMove',
      touchPoints: fingers(120 + (spread * step) / 10),
    });
  }
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await cdp.detach();
}

/** Base map tiles on the tracking map that arrived as images (blob URLs from MowDeck). */
function loadedTiles(page: Page): Promise<number> {
  return page.evaluate(
    () =>
      [
        ...document.querySelectorAll<HTMLImageElement>(
          '[data-testid="track-map"] .md-map-aerial img',
        ),
      ].filter((img) => img.src.startsWith('blob:') && img.naturalWidth > 0).length,
  );
}

test('tracking: start, positions arrive, share link works logged out, report, false alarm', async ({
  page,
  request,
  browser,
}) => {
  test.setTimeout(240_000);
  const owner = await registerByApi(request, actor('tracking'));
  const mowerId = await createMowerByApi(request, owner, `Luba tracking ${RUN}`);
  await waitForMap(request, owner, mowerId);
  const auth = { Authorization: `Bearer ${owner.token}` };

  await loginByUi(page, owner);
  const csp = watchCsp(page);
  await page.goto(`/mowers/${mowerId}/tracking`);
  await expect(page.getByTestId('tracking-idle')).toBeVisible();
  await page.getByTestId('tracking-start').click();
  await expect(page.getByTestId('tracking-status')).toBeVisible();

  // The mower drives; its positions become the track.
  await commandByApi(request, owner, mowerId, { kind: 'start_mow', area_ids: ['front'] });
  await expect
    .poll(
      async () => {
        const response = await request.get(`/api/v1/telemetry/mowers/${mowerId}/tracking`, {
          headers: auth,
        });
        return ((await response.json()) as { track: unknown[] }).track.length;
      },
      { timeout: 120_000, intervals: [2_000] },
    )
    .toBeGreaterThan(2);

  // The track lies on a real base map: the stack offers the (fake) aerial photo, served by
  // Telemetry around the track and loaded as blobs, so the map is never an empty black plane.
  const map = page.getByTestId('track-map');
  await expect(map).toHaveAttribute('data-layer', 'aerial');
  await expect.poll(() => loadedTiles(page), { timeout: 30_000 }).toBeGreaterThan(0);
  await expect(map).toContainText('Fake aerial photo');

  // The view is the user's: zoomed out (the small garden track fits at the deepest zoom), it
  // stays so across a poll (it used to refit to the whole track on every poll after a pinch).
  // Zooming out stops where the served area fills the map.
  await expect(map).toHaveAttribute('data-bounds', /^-?[\d.]+(,-?[\d.]+){3}$/);
  const zoomOut = map.locator('.controls button').nth(1);
  const fitted = await zoomOf(map);
  await zoomOut.click();
  await expect.poll(() => zoomOf(map)).toBeLessThan(fitted);
  const zoomed = await zoomOf(map);
  await page.waitForResponse(
    (response) => response.url().includes('/tracking?after=') && response.ok(),
    { timeout: 30_000 },
  );
  await settled(page);
  expect(await zoomOf(map)).toBe(zoomed);
  await expect
    .poll(
      async () => {
        await zoomOut.click();
        return (await zoomOf(map)) - Number(await map.getAttribute('data-min-zoom'));
      },
      { timeout: 30_000 },
    )
    .toBe(0);
  expect(await loadedTiles(page)).toBeGreaterThan(0);
  // The last position as text with a copy button and links to the maps apps.
  await expect(page.getByTestId('tracking-fix-coordinates')).toHaveValue(
    /^-?\d{1,2}\.\d{6}, -?\d{1,3}\.\d{6}$/,
  );
  await expect(page.getByTestId('tracking-fix-copy')).toBeVisible();
  await expect(page.getByTestId('tracking-fix-apple')).toHaveAttribute(
    'href',
    /^https:\/\/maps\.apple\.com\/\?ll=-?[\d.]+,-?[\d.]+&q=/,
  );
  await expect(page.getByTestId('tracking-fix-google')).toHaveAttribute(
    'href',
    /^https:\/\/www\.google\.com\/maps\/search\/\?api=1&query=/,
  );
  csp.assertNone();

  // A share link from the sheet.
  await page.getByTestId('tracking-share').click();
  await page.getByTestId('tracking-share-create').click();
  const address = await page.getByTestId('tracking-share-url').inputValue();
  expect(address).toMatch(/\/track#[A-Za-z0-9_-]{40,}$/);

  // Logged out (a fresh context without cookies or tokens): the page shows the live map only.
  const token = address.trim().split('#')[1]!;
  const stranger = await browser.newContext({
    ...devices['Pixel 7'],
    viewport: { width: 390, height: 844 },
  });
  const guest = await stranger.newPage();
  await guest.goto(address.trim());
  await expect(guest.getByTestId('track-public')).toBeVisible();
  await expect(guest.getByTestId('track-age')).toBeVisible();
  await expect(guest).toHaveURL(/\/track$/); // the token left the address bar
  // The guest sees the same base map, its tiles fetched with the token in the body.
  await expect(guest.getByTestId('track-map')).toHaveAttribute('data-layer', 'aerial');
  await expect.poll(() => loadedTiles(guest), { timeout: 30_000 }).toBeGreaterThan(0);
  // A pinch on the phone zooms the map (not the page) and survives the next poll.
  const guestMap = guest.getByTestId('track-map');
  const before = await zoomOf(guestMap);
  await pinch(guest, guestMap, -80);
  await expect.poll(() => zoomOf(guestMap)).toBeLessThan(before);
  const pinched = await zoomOf(guestMap);
  expect(await guest.evaluate(() => window.visualViewport?.scale ?? 1)).toBe(1);
  await guest.waitForResponse(
    (response) => response.url().endsWith('/public/tracking/view') && response.ok(),
    { timeout: 30_000 },
  );
  await settled(guest);
  expect(await zoomOf(guestMap)).toBe(pinched);
  await expect(guest.getByTestId('track-fix-coordinates')).toHaveValue(
    /^-?\d{1,2}\.\d{6}, -?\d{1,3}\.\d{6}$/,
  );
  for (const id of ['track-fix-apple', 'track-fix-google', 'track-fix-osm']) {
    const link = guest.getByTestId(id);
    await expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(await link.getAttribute('href')).not.toContain(token);
  }
  const html = await guest.content();
  expect(html).not.toContain(owner.accountId);
  expect(html).not.toContain(mowerId);
  expect(html).not.toContain(token);
  // The public API answers a logged-out call, and 404 for a wrong token.
  const open = await stranger.request.post('/api/v1/telemetry/public/tracking/view', {
    data: { token },
  });
  expect(open.status()).toBe(200);
  expect(open.headers()['x-robots-tag']).toContain('noindex');
  const closed = await stranger.request.post('/api/v1/telemetry/public/tracking/view', {
    data: { token: 'x'.repeat(43) },
  });
  expect(closed.status()).toBe(404);
  await stranger.close();

  // The report is a PDF.
  const report = await request.get(`/api/v1/telemetry/mowers/${mowerId}/tracking/report.pdf`, {
    headers: auth,
  });
  expect(report.status()).toBe(200);
  expect((await report.body()).subarray(0, 5).toString()).toBe('%PDF-');

  // False alarm ends it; the revoked or ended session no longer polls.
  await page.keyboard.press('Escape');
  await page.getByTestId('tracking-stop').click();
  // Wait for the stop request itself (the idle check alone raced the next read).
  const stopped = page.waitForResponse(
    (response) => response.url().endsWith('/tracking/stop') && response.ok(),
  );
  await page.getByTestId('tracking-stop-false-alarm').click();
  await stopped;
  const ended = await request.get(`/api/v1/telemetry/mowers/${mowerId}/tracking`, {
    headers: auth,
  });
  const body = (await ended.json()) as { session: { active: boolean; end_reason: string } };
  expect(body.session.active).toBe(false);
  expect(body.session.end_reason).toBe('false_alarm');
});
