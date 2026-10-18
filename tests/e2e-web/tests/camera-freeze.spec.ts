/**
 * Freeze detection (ADR 0044 amendment 2026-10-17): a standing picture must never look live.
 *
 * - A real `<video>`: the relay's fake source (ADR 0045) plays over WebRTC, the stage counts
 *   presented frames (`requestVideoFrameCallback`). The relay's test hook pauses the source
 *   (`POST /internal/v1/test/freeze`, same WebRTC session, no new frames): the picture must show
 *   "Bild steht" (`camera-frozen`, `data-frozen="true"` on the stage) and stay so, and run again
 *   once the source is resumed. A browser without WebRTC (Playwright's WebKit on Windows) skips
 *   this test with the reason; CI on Linux runs it in Chromium and WebKit.
 * - The test pattern on its canvas (the direct path): a canvas marked `data-freeze` sends no more
 *   frames, "Bild steht" within 3 s, live again once frames come back. Runs in every browser.
 *
 * The drive view's hold and latch over a frozen picture are `drive-camera.spec.ts` (canvas) and
 * `camera-relay-web.spec.ts` (relay).
 */
import { expect, test, type Page } from '@playwright/test';

import {
  RUN,
  actor,
  createMowerByApi,
  expectRelayLive,
  forceCameraTransport,
  freezeRelaySources,
  loginByUi,
  presentsFrames,
  registerByApi,
  requireWebRtc,
  waitForMap,
  watchCsp,
} from './support';

/** Mark the pattern canvas frozen (the simulator hook) or running. */
async function freezePattern(page: Page, on: boolean): Promise<void> {
  await page.getByTestId('camera-pattern').evaluate((node, value) => {
    if (value) (node as HTMLElement).dataset.freeze = 'true';
    else delete (node as HTMLElement).dataset.freeze;
  }, on);
}

test('camera freeze: a real video stream that stops shows "Bild steht" and runs again', async ({
  page,
  request,
  browserName,
}) => {
  test.setTimeout(180_000);
  await requireWebRtc(page, browserName);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor(`camfreeze-${browserName}`));
  const mowerId = await createMowerByApi(request, owner, `Luba freeze ${browserName} ${RUN}`);
  await waitForMap(request, owner, mowerId);
  await loginByUi(page, owner);

  await page.goto(`/mowers/${mowerId}/camera`);
  await expectRelayLive(page);
  const stage = page.getByTestId('camera-stage');
  const video = page.getByTestId('camera-video');
  // Running steadily: not frozen.
  await page.waitForTimeout(2_500);
  await expect(stage).toHaveAttribute('data-frozen', 'false');
  await expect(page.getByTestId('camera-frozen')).toHaveCount(0);

  // The source stands (the relay keeps the WebRTC session, no new frames): frozen, and it stays
  // frozen. A pause longer than MediaMTX's 10 s read timeout would end the publisher, so the
  // source is resumed well before that.
  try {
    expect(freezeRelaySources(true)).toBeGreaterThan(0);
    await expect(page.getByTestId('camera-frozen')).toBeVisible({ timeout: 6_000 });
    await expect(stage).toHaveAttribute('data-frozen', 'true');
    await expect(page.getByTestId('camera-status')).toContainText('Bild steht');
    await expect(page.getByTestId('camera-status')).toHaveAttribute('data-frozen', 'true');
    await page.waitForTimeout(1_000);
    await expect(page.getByTestId('camera-frozen')).toBeVisible();
    await expect(stage).toHaveAttribute('data-frozen', 'true');
  } finally {
    freezeRelaySources(false);
  }

  // Frames again: live again, on the same tile.
  await expect(page.getByTestId('camera-frozen')).toHaveCount(0, { timeout: 20_000 });
  await expect(stage).toHaveAttribute('data-frozen', 'false');
  await expect(stage).toHaveAttribute('data-phase', 'live');
  expect(await presentsFrames(video)).toBeGreaterThanOrEqual(3);
  csp.assertNone();
});

test('camera freeze: the test pattern that stops drawing shows "Bild steht" and runs again', async ({
  page,
  request,
  browserName,
}) => {
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor(`campattern-${browserName}`));
  const mowerId = await createMowerByApi(request, owner, `Luba pattern ${browserName} ${RUN}`);
  await waitForMap(request, owner, mowerId);
  // The simulator's test pattern on its canvas is the direct path.
  await forceCameraTransport(page, 'direct');
  await loginByUi(page, owner);

  await page.goto(`/mowers/${mowerId}/camera`);
  const stage = page.getByTestId('camera-stage');
  await expect(stage).toHaveAttribute('data-phase', 'live');
  await expect(stage).toHaveAttribute('data-provider', 'fake');
  await expect(page.getByTestId('camera-pattern')).toBeVisible();
  // Running steadily: not frozen.
  await page.waitForTimeout(2_500);
  await expect(stage).toHaveAttribute('data-frozen', 'false');

  // The pattern stops: frozen within 3 s, and it stays frozen.
  await freezePattern(page, true);
  await expect(page.getByTestId('camera-frozen')).toBeVisible({ timeout: 3_000 });
  await expect(stage).toHaveAttribute('data-frozen', 'true');
  await expect(page.getByTestId('camera-status')).toContainText('Bild steht');
  await page.waitForTimeout(1_000);
  await expect(page.getByTestId('camera-frozen')).toBeVisible();

  // Frames again: live again.
  await freezePattern(page, false);
  await expect(page.getByTestId('camera-frozen')).toBeHidden({ timeout: 3_000 });
  await expect(stage).toHaveAttribute('data-frozen', 'false');
  csp.assertNone();
});
