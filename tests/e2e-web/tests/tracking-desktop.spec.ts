/**
 * Theft tracking mode (ADR 0058, desktop): with the simulator, tracking started by hand (the
 * same path an alarm takes) records positions while the mower drives, the owner creates a share
 * link, the link shows the live page WITHOUT a login (and nothing else), the report downloads as
 * a PDF, and "false alarm" ends the session. Both maps lie on the base map Telemetry serves
 * around the track (here the stack's fake aerial photo), never on an empty plane.
 */
import { expect, test, type Page } from '@playwright/test';

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
  const stranger = await browser.newContext();
  const guest = await stranger.newPage();
  await guest.goto(address.trim());
  await expect(guest.getByTestId('track-public')).toBeVisible();
  await expect(guest.getByTestId('track-age')).toBeVisible();
  await expect(guest).toHaveURL(/\/track$/); // the token left the address bar
  // The guest sees the same base map, its tiles fetched with the token in the body.
  await expect(guest.getByTestId('track-map')).toHaveAttribute('data-layer', 'aerial');
  await expect.poll(() => loadedTiles(guest), { timeout: 30_000 }).toBeGreaterThan(0);
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
  await page.getByTestId('tracking-stop-false-alarm').click();
  await expect(page.getByTestId('tracking-idle'))
    .toBeHidden({ timeout: 5_000 })
    .catch(() => {});
  const ended = await request.get(`/api/v1/telemetry/mowers/${mowerId}/tracking`, {
    headers: auth,
  });
  const body = (await ended.json()) as { session: { active: boolean; end_reason: string } };
  expect(body.session.active).toBe(false);
  expect(body.session.end_reason).toBe('false_alarm');
});
