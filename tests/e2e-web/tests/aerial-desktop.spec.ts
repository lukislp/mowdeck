/**
 * The aerial photo under the mower map (ADR 0053), desktop and phone, against the stack's fake
 * WMS (tests/e2e-web/fake-wms), which paints a synthetic garden at the simulator map's
 * coordinates: the lawn where the front area is, the flower bed where the no-go zone is.
 *
 * Checks the toggle (off by default, remembered across a reload), that tiles arrive through
 * MowDeck's own route as blobs without a single request to a third party or a CSP violation, the
 * credit line, the opacity, that a second view is answered from the cache, that the route is no
 * open proxy, and the georeferencing: the pixel of the photo under the middle of the drawn lawn
 * is lawn green and under the drawn flower bed it is the bed's colour.
 *
 * Review screenshots go to `MOWDECK_AERIAL_SHOTS` (a directory) when it is set.
 */
import { expect, test, type Page } from '@playwright/test';

import {
  RUN,
  actor,
  createMowerByApi,
  loginByUi,
  registerByApi,
  waitForMap,
  watchCsp,
} from './support';

const SHOTS = process.env.MOWDECK_AERIAL_SHOTS;

async function shot(page: Page, name: string): Promise<void> {
  if (!SHOTS) return;
  await page.screenshot({
    path: `${SHOTS}/${name}-${test.info().project.name}.png`,
    animations: 'disabled',
  });
}

/** The colour of the aerial photo at the middle of an element drawn on the map. */
async function photoColourUnder(page: Page, selector: string): Promise<[number, number, number]> {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) throw new Error(`no ${sel}`);
    const box = el.getBoundingClientRect();
    const x = box.left + box.width / 2;
    const y = box.top + box.height / 2;
    // Leaflet's tile images ignore pointer events, so look at their boxes instead.
    const img = [...document.querySelectorAll<HTMLImageElement>('.md-map-aerial img')].find((e) => {
      const r = e.getBoundingClientRect();
      return r.left <= x && x < r.right && r.top <= y && y < r.bottom && e.naturalWidth > 0;
    });
    if (!img) throw new Error('no aerial tile under the element');
    const rect = img.getBoundingClientRect();
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext('2d')!;
    ctx.drawImage(img, 0, 0);
    const px = Math.floor(((x - rect.left) / rect.width) * img.naturalWidth);
    const py = Math.floor(((y - rect.top) / rect.height) * img.naturalHeight);
    const [r, g, b] = ctx.getImageData(px, py, 1, 1).data;
    return [r!, g!, b!] as [number, number, number];
  }, selector);
}

async function wmsRequests(page: Page): Promise<number> {
  const stats = await page.request.get('/fake-wms/stats');
  return ((await stats.json()) as { get_map: number }).get_map;
}

test('aerial photo: toggle, georeferenced tiles through MowDeck, cache and no open proxy', async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const csp = watchCsp(page);
  const outside: string[] = [];
  page.on('request', (r) => {
    const url = new URL(r.url());
    if (!['localhost', '127.0.0.1'].includes(url.hostname) && !r.url().startsWith('blob:'))
      outside.push(r.url());
  });
  const owner = await registerByApi(request, actor('aerial'));
  const mowerId = await createMowerByApi(request, owner, `Luba aerial ${RUN}`);
  await waitForMap(request, owner, mowerId);
  const auth = { Authorization: `Bearer ${owner.token}` };

  await loginByUi(page, owner);
  const tileRequests: string[] = [];
  page.on('request', (r) => {
    if (r.url().includes('/aerial/')) tileRequests.push(r.url());
  });
  await page.goto(`/mowers/${mowerId}`);
  const map = page.getByTestId('mower-map');
  await expect(map).toBeVisible();
  await expect(map).toHaveAttribute('data-ready', 'true');

  // --- off by default: the grid, no tile request ---------------------------------------------
  const toggle = page.getByTestId('aerial-toggle');
  await expect(toggle).toBeVisible();
  await expect(toggle).toHaveAttribute('aria-pressed', 'false');
  await expect(map).toHaveAttribute('data-mode', 'local');
  expect(tileRequests).toHaveLength(0);

  // --- on: geographic mode, tiles arrive as blobs, the credit line is shown ----------------------
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-pressed', 'true');
  await expect(map).toHaveAttribute('data-mode', 'geo');
  const tiles = page.locator('.md-map-aerial img.leaflet-tile-loaded');
  await expect(tiles.first()).toBeVisible({ timeout: 60_000 });
  expect(await tiles.first().getAttribute('src')).toMatch(/^blob:/);
  expect(tileRequests.length).toBeGreaterThan(0);
  expect(tileRequests.every((u) => u.includes(`/api/v1/telemetry/mowers/${mowerId}/aerial/`))).toBe(
    true,
  );
  await expect(page.getByTestId('map-attribution')).toContainText('Fake aerial photo');
  await expect(page.locator('.md-map path.md-area').first()).toBeVisible();
  await page.waitForTimeout(1_500);

  // --- georeferencing: the photo lines up with the outlines drawn on top ------------------------
  const lawn = await photoColourUnder(page, '.md-map path.md-area');
  expect(lawn[1], `lawn pixel ${lawn}`).toBeGreaterThan(lawn[0] + 25);
  expect(lawn[1]).toBeGreaterThan(lawn[2] + 40);
  const bed = await photoColourUnder(page, '.md-map path.md-nogo');
  expect(bed[0], `flower bed pixel ${bed}`).toBeGreaterThan(bed[1] + 40);
  await shot(page, 'map-aerial');

  // --- opacity ---------------------------------------------------------------------------------
  const slider = page.getByTestId('aerial-opacity');
  await slider.fill('0.4');
  await expect(page.locator('.md-map-aerial').first()).toHaveCSS('opacity', '0.4');
  await shot(page, 'map-aerial-faint');
  await slider.fill('0.85');

  // --- remembered, and the second view comes from the server cache ------------------------------
  const before = await wmsRequests(page);
  expect(before).toBeGreaterThan(0);
  await page.reload();
  await expect(page.getByTestId('aerial-toggle')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.md-map-aerial img.leaflet-tile-loaded').first()).toBeVisible({
    timeout: 60_000,
  });
  expect(await wmsRequests(page)).toBe(before);

  // --- off again: back to the grid -------------------------------------------------------------
  await page.getByTestId('aerial-toggle').click();
  await expect(page.getByTestId('mower-map')).toHaveAttribute('data-mode', 'local');

  // --- no open proxy, no anonymous access ----------------------------------------------------
  const info = await request.get(`/api/v1/telemetry/mowers/${mowerId}/aerial`, { headers: auth });
  const body = (await info.json()) as { tile_url: string; bounds: { south: number } };
  expect(body.tile_url).toContain(`/mowers/${mowerId}/aerial/{z}/{x}/{y}`);
  const far = await request.get(`/api/v1/telemetry/mowers/${mowerId}/aerial/17/1/1`, {
    headers: auth,
  });
  expect(far.status()).toBe(404);
  expect((await far.json()).type).toBe('urn:mowdeck:problem:tile-outside-map');
  const anonymous = await request.get(`/api/v1/telemetry/mowers/${mowerId}/aerial/17/1/1`);
  expect(anonymous.status()).toBe(401);
  expect(await wmsRequests(page)).toBe(before);

  // --- nothing went to a third party, the CSP stayed as it is --------------------------------
  expect(outside).toEqual([]);
  csp.assertNone();
});
