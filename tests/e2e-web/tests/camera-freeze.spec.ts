/**
 * Freeze detection on a real `<video>` (ADR 0044 amendment 2026-10-17) and the camera debug
 * overlay. With `?camdebug=1&camfakevideo=1` the simulator's test pattern plays through the
 * tile's `<video>` as a `MediaStream` (`canvas.captureStream()`), like the WebRTC stream of a
 * real camera: the stage counts presented frames (`requestVideoFrameCallback`), a canvas marked
 * `data-freeze` sends no more frames and the picture must show "Bild steht" within 3 s, and it
 * must run again once frames come back. The overlay shows the element's frame signals and the
 * freeze watch's threshold. Runs in Chromium (desktop) and WebKit (webkit project); a browser
 * without `captureStream` or `requestVideoFrameCallback` (Playwright's WebKit on Windows) keeps
 * the canvas and checks the same freeze on the pattern's own counter. `?camdebug=0` switches
 * everything off again.
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

/** Mark the pattern canvas frozen (the simulator hook) or running. */
async function freeze(page: Page, on: boolean): Promise<void> {
  await page.getByTestId('camera-pattern').evaluate((node, value) => {
    if (value) (node as HTMLElement).dataset.freeze = 'true';
    else delete (node as HTMLElement).dataset.freeze;
  }, on);
}

/** The overlay's number after `label=` (null: not shown). */
async function overlayNumber(page: Page, label: string): Promise<number | null> {
  const text = (await page.getByTestId('camera-debug').textContent()) ?? '';
  const match = new RegExp(`${label}=(\\d+)`).exec(text);
  return match ? Number(match[1]) : null;
}

test('camera freeze: a real video stream that stops shows "Bild steht", the debug overlay reads it', async ({
  page,
  request,
}) => {
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('camfreeze'));
  const mowerId = await createMowerByApi(request, owner, `Luba freeze ${RUN}`);
  await waitForMap(request, owner, mowerId);
  await loginByUi(page, owner);

  const throughVideo = await page.evaluate(
    () =>
      typeof HTMLCanvasElement.prototype.captureStream === 'function' &&
      'requestVideoFrameCallback' in HTMLVideoElement.prototype,
  );
  await page.goto(`/mowers/${mowerId}/camera?camdebug=1&camfakevideo=1`);
  const stage = page.getByTestId('camera-stage');
  await expect(stage).toHaveAttribute('data-phase', 'live');
  const overlay = page.getByTestId('camera-debug');
  await expect(overlay).toBeVisible();
  await expect(overlay).toContainText('phase=live');
  await expect(overlay).toContainText('freeze since=');

  if (throughVideo) {
    // The pattern plays in the <video> (marked for frame capture), the canvas stays hidden.
    const video = page.getByTestId('camera-video');
    await expect(video).toBeVisible();
    await expect(video).toHaveAttribute('data-camera-video', '');
    await expect(page.getByTestId('camera-pattern')).toBeHidden();
    // Presented frames rise (requestVideoFrameCallback on a MediaStream).
    await expect
      .poll(() => overlayNumber(page, 'presented'), { timeout: 5_000 })
      .toBeGreaterThan(0);
    const first = (await overlayNumber(page, 'presented')) ?? 0;
    await expect
      .poll(() => overlayNumber(page, 'presented'), { timeout: 5_000 })
      .toBeGreaterThan(first);
  } else {
    await expect(page.getByTestId('camera-pattern')).toBeVisible();
  }
  // Running steadily: not frozen, the threshold at its floor.
  await page.waitForTimeout(2_500);
  await expect(stage).toHaveAttribute('data-frozen', 'false');
  await expect(overlay).toContainText('limit=1500ms');

  // The stream stops: frozen within 3 s, and it stays frozen.
  await freeze(page, true);
  await expect(page.getByTestId('camera-frozen')).toBeVisible({ timeout: 3_000 });
  await expect(stage).toHaveAttribute('data-frozen', 'true');
  await expect(overlay).toContainText('frozen=true');
  if (throughVideo) {
    const standing = await overlayNumber(page, 'presented');
    await page.waitForTimeout(1_200);
    expect(await overlayNumber(page, 'presented')).toBe(standing);
  }
  await page.waitForTimeout(1_000);
  await expect(page.getByTestId('camera-frozen')).toBeVisible();

  // Frames again: live again.
  await freeze(page, false);
  await expect(page.getByTestId('camera-frozen')).toBeHidden({ timeout: 3_000 });
  await expect(stage).toHaveAttribute('data-frozen', 'false');

  // `?camdebug=0`: no overlay, the pattern back on its canvas.
  await page.goto(`/mowers/${mowerId}/camera?camdebug=0`);
  await expect(stage).toHaveAttribute('data-phase', 'live');
  await expect(page.getByTestId('camera-pattern')).toBeVisible();
  await expect(page.getByTestId('camera-debug')).toHaveCount(0);
  csp.assertNone();
});
