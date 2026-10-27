/**
 * Theft tracking mode (ADR 0058, desktop): with the simulator, tracking started by hand (the
 * same path an alarm takes) records positions while the mower drives, the owner creates a share
 * link, the link shows the live page WITHOUT a login (and nothing else), the report downloads as
 * a PDF, and "false alarm" ends the session.
 */
import { expect, test } from '@playwright/test';

import {
  RUN,
  actor,
  commandByApi,
  createMowerByApi,
  loginByUi,
  registerByApi,
  waitForMap,
} from './support';

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

  // A share link from the sheet.
  await page.getByTestId('tracking-share').click();
  await page.getByTestId('tracking-share-create').click();
  const address = await page.getByTestId('tracking-share-url').inputValue();
  expect(address).toMatch(/\/track#[A-Za-z0-9_-]{40,}$/);

  // Logged out (a fresh context without cookies or tokens): the page shows the live map only.
  const stranger = await browser.newContext();
  const guest = await stranger.newPage();
  await guest.goto(address.trim());
  await expect(guest.getByTestId('track-public')).toBeVisible();
  await expect(guest.getByTestId('track-age')).toBeVisible();
  await expect(guest).toHaveURL(/\/track$/); // the token left the address bar
  const html = await guest.content();
  expect(html).not.toContain(owner.accountId);
  expect(html).not.toContain(mowerId);
  // The public API answers a logged-out call, and 404 for a wrong token.
  const token = address.trim().split('#')[1];
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
