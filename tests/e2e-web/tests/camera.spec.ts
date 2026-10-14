/**
 * Phase 5, Task 25: the live camera (ADR 0032) with the simulated mower, whose sessions have the
 * provider `fake`: the camera panel sits inline in the detail page's side column (after the status block, before the areas) and
 * is open by default, shows the generated test pattern (a canvas that keeps drawing frames),
 * collapses (no session; remembered across a reload) and expands again, the full-screen page
 * shows it too, the light switch follows the state the mower reports, and a Viewer (`can_view`
 * only) sees the camera but cannot switch the light. Stereo cameras: the simulated mower has a
 * front left and a front right camera (like a LUBA 2 AWD X); the picker switches between them
 * and offers "Beide", which shows both side by side (stacked on a phone), each tile live with
 * its own name, the right pattern mirrored so the two pictures differ, and no "same picture"
 * notice. "Panorama" blends both into one wide canvas (both test patterns, the right one
 * mirrored and labelled), calibrated with the overlap and offset sliders and the mirror switch
 * and once with the local feature calibration (ADR 0037: result, apply, reset; OpenCV is not
 * involved, the matcher is plain TypeScript); the raw pattern canvases stay in the page (hidden)
 * with their capture marks.
 * Writes apps/web/screenshots/camera-phone.png and camera-both-phone.png.
 */
import { fileURLToPath } from 'node:url';

import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import {
  RUN,
  actor,
  createMowerByApi,
  loginByUi,
  registerByApi,
  waitForMap,
  watchCsp,
  type Actor,
} from './support';

const OUT = fileURLToPath(new URL('../../../apps/web/screenshots/', import.meta.url));

async function createViewer(request: APIRequestContext, owner: Actor): Promise<Actor> {
  const auth = { Authorization: `Bearer ${owner.token}` };
  const roles = (await (
    await request.get(`/api/v1/identity/accounts/${owner.accountId}/roles`, { headers: auth })
  ).json()) as { id: string; key: string | null }[];
  const viewerRole = roles.find((role) => role.key === 'viewer');
  expect(viewerRole, 'viewer role').toBeDefined();
  const viewer = actor('camera-viewer');
  const created = await request.post(`/api/v1/identity/accounts/${owner.accountId}/users`, {
    headers: auth,
    data: {
      username: viewer.username,
      password: viewer.password,
      assignments: [{ role_id: viewerRole!.id, level: { type: 'account', id: owner.accountId } }],
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  return viewer;
}

/** The test pattern keeps drawing: its frame counter grows. */
async function expectPatternRunning(page: Page, testid = 'camera-pattern'): Promise<void> {
  const pattern = page.getByTestId(testid);
  await expect(pattern).toBeVisible();
  const frames = async () => Number(await pattern.getAttribute('data-frames'));
  const first = await frames();
  await expect.poll(frames, { timeout: 5_000 }).toBeGreaterThan(first);
  // Not a blank canvas: the bars are drawn.
  const painted = await pattern.evaluate((node) => {
    const canvas = node as HTMLCanvasElement;
    const data = canvas.getContext('2d')!.getImageData(0, 0, canvas.width, canvas.height).data;
    const colours = new Set<string>();
    for (let i = 0; i < data.length; i += 4 * 997) colours.add(`${data[i]},${data[i + 1]}`);
    return colours.size;
  });
  expect(painted).toBeGreaterThan(3);
}

/** A coarse fingerprint of a canvas (every 997th pixel), to tell two pictures apart. */
async function fingerprint(page: Page, testid: string): Promise<string> {
  return page.getByTestId(testid).evaluate((node) => {
    const canvas = node as HTMLCanvasElement;
    const data = canvas.getContext('2d')!.getImageData(0, 0, canvas.width, canvas.height).data;
    const parts: string[] = [];
    for (let i = 0; i < data.length; i += 4 * 997) parts.push(`${data[i]},${data[i + 1]}`);
    return parts.join(';');
  });
}

test('camera: the simulated mower shows the test pattern; a viewer can open it', async ({
  page,
  request,
  browser,
}) => {
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('camera'));
  const mowerId = await createMowerByApi(request, owner, `Luba camera ${RUN}`);
  await waitForMap(request, owner, mowerId);

  // The API: a fake session for the simulated mower, never cached.
  const session = await request.post(`/api/v1/fleet/mowers/${mowerId}/camera/session`, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  expect(session.status(), await session.text()).toBe(200);
  expect(session.headers()['cache-control']).toBe('no-store');
  const body = (await session.json()) as {
    provider: string;
    cameras: { uid: number; placement: string }[];
  };
  expect(body.provider).toBe('fake');
  expect(body.cameras.map((c) => [c.uid, c.placement])).toEqual([
    [1, 'front_left'],
    [2, 'front_right'],
  ]);

  await loginByUi(page, owner);
  await page.goto(`/mowers/${mowerId}`);
  // No header button any more: the panel is inline and open by default.
  await expect(page.getByTestId('mower-camera')).toHaveCount(0);
  const panel = page.getByTestId('camera-panel');
  await expect(panel).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId('camera').getByTestId('camera-panel')).toBeVisible();
  await expect(panel).toHaveAttribute('data-provider', 'fake');
  await expect(panel).toHaveAttribute('data-phase', 'live');
  await expectPatternRunning(page);
  await expect(page.getByTestId('camera-status')).toContainText('Testbild');
  await expect(page.getByTestId('camera-data-note')).toContainText('Agora');
  await expect(page.getByTestId('camera-expiry')).toBeVisible();
  await expect(page.getByTestId('camera-active-uid')).toContainText('1');
  // Two front cameras: the picker switches between them (labels from the placement).
  const picker = page.getByTestId('camera-picker');
  await picker.getByText('Front rechts').click();
  await expect(picker.getByText('Front rechts')).toHaveAttribute('aria-checked', 'true');
  await expect(page.getByTestId('camera-active-uid')).toContainText('2');
  await expect(page.getByTestId('camera-tile')).toHaveAttribute('data-side', 'right');

  // "Beide": left and right side by side, each live with its own name and its own picture.
  await picker.getByText('Beide').click();
  await expect(picker.getByText('Beide')).toHaveAttribute('aria-checked', 'true');
  const stage = panel.locator('.stage');
  await expect(stage).toHaveAttribute('data-layout', 'both');
  const leftTile = page.getByTestId('camera-tile');
  const rightTile = page.getByTestId('camera-tile-right');
  await expect(leftTile).toHaveAttribute('data-status', 'live');
  await expect(rightTile).toHaveAttribute('data-status', 'live');
  await expect(leftTile).toHaveAttribute('data-side', 'left');
  await expect(rightTile).toHaveAttribute('data-side', 'right');
  await expect(leftTile.getByTestId('camera-tile-caption')).toContainText('Front links');
  await expect(rightTile.getByTestId('camera-tile-caption')).toContainText('Front rechts');
  await expectPatternRunning(page, 'camera-pattern');
  await expectPatternRunning(page, 'camera-pattern-right');
  // The right camera is drawn mirrored: the two pictures are not the same stream.
  expect(await fingerprint(page, 'camera-pattern')).not.toBe(
    await fingerprint(page, 'camera-pattern-right'),
  );
  // Both tiles carry the capture marks AI assist reads.
  await expect(page.getByTestId('camera-pattern')).toHaveAttribute('data-camera-side', 'left');
  await expect(page.getByTestId('camera-pattern-right')).toHaveAttribute(
    'data-camera-name',
    'Front right',
  );
  // The duplicate guard compares the tiles every 2 s; different pictures never trip it.
  await page.waitForTimeout(7_000);
  await expect(page.getByTestId('camera-same-picture')).toHaveCount(0);
  await expect(page.getByTestId('camera-no-data')).toHaveCount(0);

  // "Panorama": one wide canvas drawn from both patterns; the raw canvases stay as its sources.
  await page.getByTestId('camera-layout-panorama').click();
  await expect(page.getByTestId('camera-layout-panorama')).toHaveAttribute('aria-checked', 'true');
  await expect(stage).toHaveAttribute('data-layout', 'panorama');
  const panorama = page.getByTestId('camera-panorama');
  await expect(panorama).toBeVisible();
  const panoramaFrames = async () => Number(await panorama.getAttribute('data-frames'));
  const firstFrame = await panoramaFrames();
  await expect.poll(panoramaFrames, { timeout: 5_000 }).toBeGreaterThan(firstFrame);
  const wide = await panorama.evaluate((node) => {
    const canvas = node as HTMLCanvasElement;
    const data = canvas.getContext('2d')!.getImageData(0, 0, canvas.width, canvas.height).data;
    const colours = new Set<string>();
    for (let i = 0; i < data.length; i += 4 * 997) colours.add(`${data[i]},${data[i + 1]}`);
    return { ratio: canvas.width / canvas.height, colours: colours.size };
  });
  // Two 16:9 patterns with 12 % overlap: much wider than one.
  expect(wide.ratio).toBeGreaterThan(2.5);
  expect(wide.colours).toBeGreaterThan(3);
  const panoramaCaption = page.getByTestId('camera-panorama-caption');
  await expect(panoramaCaption).toContainText('Front links');
  await expect(panoramaCaption).toContainText('Front rechts');
  await expect(page.getByTestId('camera-pattern')).toBeHidden();
  await expect(page.getByTestId('camera-pattern')).toHaveAttribute('data-camera-video', '');
  await expect(page.getByTestId('camera-pattern-right')).toHaveAttribute(
    'data-camera-side',
    'right',
  );
  // Calibration: overlap, offset and mirror (the simulator's right pattern is mirrored).
  await page.getByTestId('camera-panorama-calibrate').click();
  await page.getByTestId('camera-panorama-overlap').fill('30');
  await expect(panorama).toHaveAttribute('data-overlap', '30');
  await page.getByTestId('camera-panorama-offset').fill('-5');
  await expect(panorama).toHaveAttribute('data-offset', '-5');
  await page.getByTestId('camera-panorama-mirror').click();
  await expect(panorama).toHaveAttribute('data-mirror', 'true');
  await page.getByTestId('camera-panorama-auto').click();
  await expect(page.getByTestId('camera-panorama-auto-result')).toBeVisible();
  await page.getByTestId('camera-panorama-reset').click();
  await expect(panorama).toHaveAttribute('data-overlap', '12');
  // The local calibration (features in the browser, falling back to the correlation) on the
  // simulator's patterns. Colour bars and captions are no stereo scene: the run must finish with
  // a result or an explained failure, never hang; a result is applied and then reset. The
  // AI-assisted method needs a provider and is covered by the unit tests.
  await page.getByTestId('camera-calibration-method-features').click();
  await expect(page.getByTestId('camera-calibration-method-features')).toHaveAttribute(
    'aria-checked',
    'true',
  );
  await page.getByTestId('camera-panorama-mirror').click();
  await page.getByTestId('camera-calibration-run').click();
  const calibrationResult = page.getByTestId('camera-calibration-result');
  const calibrationError = page.getByTestId('camera-calibration-error');
  await expect(calibrationResult.or(calibrationError)).toBeVisible({ timeout: 15_000 });
  if (await calibrationResult.isVisible()) {
    await expect(calibrationResult).toHaveAttribute('data-method', /^(features|correlation)$/);
    await expect(page.getByTestId('camera-calibration-after')).toBeVisible();
    await page.getByTestId('camera-calibration-apply').click();
    await expect(calibrationResult).toHaveCount(0);
    await expect(panorama).toHaveAttribute('data-method', /^(features|correlation)$/);
    await expect(page.getByTestId('camera-calibration-active')).toBeVisible();
  } else {
    await expect(calibrationError).toHaveAttribute('data-reason', 'no_match');
  }
  await page.getByTestId('camera-panorama-reset').click();
  await expect(panorama).toHaveAttribute('data-method', 'manual');
  await expect(panorama).toHaveAttribute('data-overlap', '12');
  await expect(page.getByTestId('camera-same-picture')).toHaveCount(0);

  // Back to one camera.
  await picker.getByText('Front links').click();
  await expect(stage).toHaveAttribute('data-layout', 'single');
  await expect(rightTile).toBeHidden();

  // The light in the camera panel: the simulator reports it off; switching follows the state.
  const light = page.getByTestId('light-control');
  await expect(light).toHaveAttribute('data-state', 'off', { timeout: 30_000 });
  await page.getByTestId('light-toggle').click();
  await expect(light).toHaveAttribute('data-state', 'on', { timeout: 15_000 });
  await expect(light).toHaveAttribute('data-pending', 'false', { timeout: 15_000 });
  await page.getByTestId('light-toggle').click();
  await expect(light).toHaveAttribute('data-state', 'off', { timeout: 15_000 });

  // Collapsing removes the panel (and its session); the choice survives a reload.
  const toggle = page.getByTestId('camera-toggle');
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await toggle.click();
  await expect(page.getByTestId('camera-panel')).toHaveCount(0);
  await expect(page.getByTestId('camera-collapsed')).toBeVisible();
  await page.reload();
  await expect(page.getByTestId('camera-collapsed')).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId('camera-panel')).toHaveCount(0);
  await page.getByTestId('camera-toggle').click();
  await expect(page.getByTestId('camera-panel')).toHaveAttribute('data-phase', 'live');

  // The full-screen page, with the emergency stop in view.
  await page.getByTestId('camera-fullscreen').click();
  await page.waitForURL(new RegExp(`/mowers/${mowerId}/camera$`));
  await expect(page.getByTestId('camera-page')).toBeVisible();
  await expectPatternRunning(page);
  await expect(page.getByRole('button', { name: 'Not-Stopp' })).toBeVisible();
  await page.getByTestId('camera-close').click();
  await page.waitForURL(new RegExp(`/mowers/${mowerId}$`));
  // Back on the detail page the inline panel is open again.
  await expect(page.getByTestId('camera-panel')).toHaveAttribute('data-phase', 'live');
  // The phone screenshot of the full-screen camera (same login: Identity's login is rate limited).
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/mowers/${mowerId}/camera`);
  await expect(page.getByTestId('camera-panel')).toHaveAttribute('data-phase', 'live');
  await expectPatternRunning(page);
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${OUT}camera-phone.png`, animations: 'disabled' });
  // Both cameras on a phone: stacked, left above right.
  await page.getByTestId('camera-picker').getByText('Beide').click();
  await expect(page.getByTestId('camera-tile-right')).toHaveAttribute('data-status', 'live');
  const top = await page.getByTestId('camera-tile').boundingBox();
  const below = await page.getByTestId('camera-tile-right').boundingBox();
  expect(top && below && below.y >= top.y + top.height - 1).toBe(true);
  await expectPatternRunning(page, 'camera-pattern-right');
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${OUT}camera-both-phone.png`, animations: 'disabled' });
  await page.getByTestId('camera-picker').getByText('Front links').click();
  csp.assertNone();

  // A Viewer (can_view only) opens the camera too.
  const viewer = await createViewer(request, owner);
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const viewerPage = await context.newPage();
  await loginByUi(viewerPage, viewer);
  await viewerPage.goto(`/mowers/${mowerId}`);
  await expect(viewerPage.getByTestId('camera-panel')).toHaveAttribute('data-phase', 'live');
  await expectPatternRunning(viewerPage);
  await expect(viewerPage.getByTestId('light-toggle')).toBeDisabled();
  await context.close();
});
