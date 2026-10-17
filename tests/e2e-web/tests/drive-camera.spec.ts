/**
 * The drive view with the camera (ADR 0044) against the stack's simulated mower, whose camera
 * sessions are `fake` (the test pattern): on the desktop (1280 x 800), a phone upright (390 x 844)
 * and a phone held sideways (844 x 390) the drive page shows the camera, the emergency stop and
 * the session Start/Stop lie fully inside the viewport and nothing overflows sideways (sideways
 * nothing scrolls at all). The joystick drives with the camera shown. A frozen picture (the
 * simulator hook `data-freeze` on the pattern canvas) shows "Bild steht", the mower is held (only
 * zero frames go out while the stick is held forward) and the stick drives again only after it
 * was let go and touched again. Leaving the page ends the drive session (`client_stop`) and the
 * camera: exactly one camera session request per visit. A simulated mower without a camera
 * (`connection_options.camera: false`) gets the map and no camera session.
 * The camera light: in every layout the light chip lies fully inside the viewport over the camera,
 * clear of the emergency stop, the session Start/Stop, the readout and the stick; it
 * switches the simulated mower's light (`data-state` follows what the mower reports, as on the
 * mower page) while the drive session stays open and the stick keeps driving, stays usable while
 * the picture stands, and is absent in the map view and for a mower without the `light`
 * capability (`connection_options.light: false`).
 * Writes drive-camera-desktop.png, drive-camera-frozen-desktop.png, drive-camera-phone.png and
 * drive-camera-sideways.png to apps/web/screenshots (or MOWDECK_E2E_SHOTS).
 */
import { fileURLToPath } from 'node:url';

import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test';

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

const OUT =
  process.env.MOWDECK_E2E_SHOTS ??
  fileURLToPath(new URL('../../../apps/web/screenshots/', import.meta.url));

const DESKTOP = { width: 1280, height: 800 };
const UPRIGHT = { width: 390, height: 844 };
const SIDEWAYS = { width: 844, height: 390 };

interface LiveState {
  speed_mps: number | null;
  position: { lat: number; lon: number } | null;
}

interface SentFrame {
  at: number;
  linear: number;
  angular: number;
}

async function mowerState(
  request: APIRequestContext,
  who: Actor,
  mowerId: string,
): Promise<LiveState | null> {
  const response = await request.get(`/api/v1/telemetry/mowers/${mowerId}/state`, {
    headers: { Authorization: `Bearer ${who.token}` },
  });
  return response.ok() ? ((await response.json()) as { state: LiveState }).state : null;
}

function metres(a: { lat: number; lon: number }, b: { lat: number; lon: number }): number {
  const k = 111_320;
  const dx = (b.lon - a.lon) * k * Math.cos((a.lat * Math.PI) / 180);
  return Math.hypot(dx, (b.lat - a.lat) * k);
}

async function createSimulatedMower(
  request: APIRequestContext,
  owner: Actor,
  name: string,
  options: Record<string, unknown>,
): Promise<string> {
  const response = await request.post('/api/v1/fleet/mowers', {
    headers: { Authorization: `Bearer ${owner.token}` },
    data: { name, connection_kind: 'simulated', connection_options: options },
  });
  expect(response.status(), await response.text()).toBe(201);
  return ((await response.json()) as { id: string }).id;
}

async function waitForCaps(request: APIRequestContext, owner: Actor, mowerId: string) {
  await expect
    .poll(
      async () => {
        const response = await request.get(`/api/v1/telemetry/mowers/${mowerId}/capabilities`, {
          headers: { Authorization: `Bearer ${owner.token}` },
        });
        return response.ok() ? (await response.json()).capabilities : null;
      },
      { timeout: 60_000 },
    )
    .not.toBeNull();
}

/** The element lies completely inside the viewport (and is not covered at its centre). */
async function expectInViewport(page: Page, locator: Locator, what: string): Promise<void> {
  await expect(locator, what).toBeVisible();
  const box = (await locator.boundingBox())!;
  const view = page.viewportSize()!;
  expect(box.x, `${what}: left`).toBeGreaterThanOrEqual(0);
  expect(box.y, `${what}: top`).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width, `${what}: right`).toBeLessThanOrEqual(view.width + 0.5);
  expect(box.y + box.height, `${what}: bottom`).toBeLessThanOrEqual(view.height + 0.5);
  const reachable = await locator.evaluate((node) => {
    const rect = node.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
    return hit !== null && (node === hit || node.contains(hit));
  });
  expect(reachable, `${what}: not covered`).toBe(true);
}

/** The two elements do not overlap. */
async function expectApart(a: Locator, b: Locator, what: string): Promise<void> {
  const one = (await a.boundingBox())!;
  const two = (await b.boundingBox())!;
  const overlap =
    one.x < two.x + two.width &&
    two.x < one.x + one.width &&
    one.y < two.y + two.height &&
    two.y < one.y + one.height;
  expect(overlap, `${what}: apart`).toBe(false);
}

/** The light chip over the camera: in view, reachable, clear of the stop controls (and stick). */
async function expectLightPlaced(page: Page, layout: string, stick?: Locator): Promise<Locator> {
  const light = page.getByTestId('drive-light');
  await expectInViewport(page, light, `light (${layout})`);
  await expectApart(
    light,
    page.getByTestId('emergency-stop'),
    `light vs emergency stop (${layout})`,
  );
  const session = page.getByTestId('drive-stop').or(page.getByTestId('drive-start'));
  await expectApart(light, session.first(), `light vs session button (${layout})`);
  await expectApart(light, page.getByTestId('drive-hud'), `light vs readout (${layout})`);
  if (stick) await expectApart(light, stick, `light vs stick (${layout})`);
  return light;
}

/** One tap on the light chip; the state the mower reports follows. */
async function switchLight(page: Page, to: 'on' | 'off'): Promise<void> {
  const light = page.getByTestId('drive-light');
  await expect(light).toHaveAttribute('data-pending', 'false', { timeout: 15_000 });
  await expect(light).toBeEnabled();
  await light.click();
  await expect(light).toHaveAttribute('data-state', to, { timeout: 15_000 });
  await expect(light).toHaveAttribute('data-pending', 'false', { timeout: 15_000 });
  await expect(light).toHaveAttribute('aria-checked', to === 'on' ? 'true' : 'false');
}

async function expectNoSidewaysOverflow(page: Page): Promise<void> {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

/** The test pattern keeps drawing: its frame counter grows. */
async function expectPatternRunning(page: Page): Promise<void> {
  const pattern = page.getByTestId('camera-pattern');
  await expect(pattern).toBeVisible();
  const frames = async () => Number(await pattern.getAttribute('data-frames'));
  const first = await frames();
  await expect.poll(frames, { timeout: 5_000 }).toBeGreaterThan(first);
}

async function freeze(page: Page, on: boolean): Promise<void> {
  await page.getByTestId('camera-pattern').evaluate((node, value) => {
    if (value) (node as HTMLElement).dataset.freeze = 'true';
    else delete (node as HTMLElement).dataset.freeze;
  }, on);
}

/** Press the pad at its centre and push it forward (real pointer events). */
async function pushForward(page: Page, pad: Locator): Promise<void> {
  const box = (await pad.boundingBox())!;
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  await page.mouse.move(cx, cy);
  await page.mouse.down();
  await page.mouse.move(cx, cy - box.height * 0.45, { steps: 5 });
  await expect(pad).toHaveAttribute('data-engaged', 'true');
}

test('drive view: camera in three layouts, freeze holds the mower, one camera session per visit', async ({
  page,
  request,
  browserName,
}) => {
  test.setTimeout(300_000);
  const csp = watchCsp(page);
  // The screenshots come from Chromium. In WebKit (the webkit project) they would overwrite them,
  // and Playwright's own stylesheet that stops animations is refused by the CSP there.
  const shot = async (name: string): Promise<void> => {
    if (browserName === 'webkit') return;
    await page.screenshot({ path: `${OUT}${name}.png`, animations: 'disabled' });
  };
  const owner = await registerByApi(request, actor('drivecam'));
  const mowerId = await createMowerByApi(request, owner, `Luba drivecam ${RUN}`);
  const blindId = await createSimulatedMower(request, owner, `Luba blind ${RUN}`, {
    camera: false,
  });
  const unlitId = await createSimulatedMower(request, owner, `Luba unlit ${RUN}`, {
    light: false,
  });
  await waitForMap(request, owner, mowerId);
  await waitForCaps(request, owner, mowerId);
  await waitForCaps(request, owner, blindId);
  await waitForCaps(request, owner, unlitId);
  await expect
    .poll(async () => (await mowerState(request, owner, mowerId))?.position ?? null, {
      timeout: 60_000,
    })
    .not.toBeNull();

  // Camera session requests per mower, and every drive frame the page sends.
  const sessions = new Map<string, number>();
  page.on('request', (req) => {
    const match = /\/fleet\/mowers\/([^/]+)\/camera\/session$/.exec(req.url());
    if (match && req.method() === 'POST')
      sessions.set(match[1]!, (sessions.get(match[1]!) ?? 0) + 1);
  });
  const frames: SentFrame[] = [];
  page.on('websocket', (socket) => {
    socket.on('framesent', (event) => {
      try {
        const message = JSON.parse(String(event.payload)) as {
          type?: string;
          linear?: number;
          angular?: number;
        };
        if (message.type === 'drive_frame') {
          frames.push({
            at: Date.now(),
            linear: message.linear ?? 0,
            angular: message.angular ?? 0,
          });
        }
      } catch {
        // not JSON
      }
    });
  });

  await page.setViewportSize(DESKTOP);
  await loginByUi(page, owner);
  await page.goto(`/mowers/${mowerId}`);
  await page.getByTestId('mower-drive').click();
  await page.waitForURL(new RegExp(`/mowers/${mowerId}/drive$`));
  // The mower page's camera panel had its own session; count the drive visit from here.
  await expect(page.getByTestId('camera-panel')).toHaveCount(0);
  const before = sessions.get(mowerId) ?? 0;

  // --- desktop -------------------------------------------------------------------------------
  const drive = page.getByTestId('drive-page');
  await expect(drive).toHaveAttribute('data-view', 'camera', { timeout: 30_000 });
  await expect(drive).toHaveAttribute('data-layout', 'desktop');
  const stage = page.getByTestId('camera-stage');
  await expect(stage).toHaveAttribute('data-phase', 'live', { timeout: 30_000 });
  await expectPatternRunning(page);
  // The front left camera by default.
  await expect(page.getByTestId('camera-tile')).toHaveAttribute('data-side', 'left');
  await expect(page.getByTestId('drive-map-thumb')).toBeVisible();
  await expect(page.getByTestId('drive-view')).toBeVisible();
  // The picture never takes the pointer from what floats above it.
  expect(
    await page
      .getByTestId('camera-pattern')
      .evaluate((node) => getComputedStyle(node).pointerEvents),
  ).toBe('none');
  expect(
    await page.getByTestId('camera-video').evaluate((node) => getComputedStyle(node).pointerEvents),
  ).toBe('none');
  await expectInViewport(page, page.getByTestId('emergency-stop'), 'emergency stop');
  await expectInViewport(page, page.getByTestId('drive-start'), 'session start');
  await expectNoSidewaysOverflow(page);
  // The camera light, as the simulator reports it: off.
  const light = await expectLightPlaced(page, 'desktop');
  await expect(light).toHaveAttribute('data-state', 'off', { timeout: 30_000 });

  // "Both": the stereo pair side by side, and back to the left camera.
  await page.getByTestId('drive-camera-both').click();
  await expect(page.getByTestId('camera-tile-right')).toHaveAttribute('data-status', 'live');
  await page.getByTestId('drive-camera-both').click();
  await expect(page.getByTestId('camera-tile-right')).toBeHidden();
  await expect(page.getByTestId('camera-tile')).toHaveAttribute('data-side', 'left');

  // The map thumbnail hides on a tap and comes back.
  await page.getByTestId('drive-map-thumb-hide').click();
  await expect(page.getByTestId('drive-map-thumb')).toHaveCount(0);
  await page.getByTestId('drive-map-thumb-show').click();
  await expect(page.getByTestId('drive-map-thumb')).toBeVisible();

  // Start and drive with the camera on screen.
  await page.getByTestId('drive-start').click();
  await expect(page.getByTestId('drive-state')).toHaveText('Fahrt aktiv');
  await expectInViewport(page, page.getByTestId('drive-stop'), 'session stop');
  // The light switches while the session is open; the session stays and the stick drives after.
  await expectLightPlaced(page, 'desktop, driving');
  await switchLight(page, 'on');
  await expect(page.getByTestId('drive-state')).toHaveText('Fahrt aktiv');
  const pad = page.getByTestId('joystick');
  const start = (await mowerState(request, owner, mowerId))!;
  await pushForward(page, pad);
  await expect
    .poll(
      async () => {
        const now = await mowerState(request, owner, mowerId);
        return now?.position ? metres(start.position!, now.position) : 0;
      },
      { timeout: 6_000, intervals: [250] },
    )
    .toBeGreaterThan(0.3);
  await expect(stage).toHaveAttribute('data-frozen', 'false');
  await page.waitForTimeout(400);
  await shot('drive-camera-desktop');

  // --- freeze: the picture stands while the stick is held forward ----------------------------
  await freeze(page, true);
  await expect(page.getByTestId('camera-frozen')).toBeVisible({ timeout: 3_000 });
  await expect(stage).toHaveAttribute('data-frozen', 'true');
  await expect(page.getByTestId('camera-status')).toContainText('Bild steht');
  await expect(page.getByTestId('drive-held')).toBeVisible();
  await expect(page.getByTestId('drive-pad')).toHaveAttribute('data-held', 'true');
  // "Bild steht" does not hide the light.
  await expectLightPlaced(page, 'desktop, frozen');
  await expect(light).toBeEnabled();
  const frozenAt = Date.now();
  await shot('drive-camera-frozen-desktop');
  await page.waitForTimeout(1_500);
  const whileFrozen = frames.filter((f) => f.at > frozenAt);
  // The session stays open (zero frames keep it alive), but nothing moves.
  expect(whileFrozen.length).toBeGreaterThan(0);
  expect(whileFrozen.every((f) => f.linear === 0 && f.angular === 0)).toBe(true);
  await expect(page.getByTestId('drive-state')).toHaveText('Fahrt aktiv');
  await expect
    .poll(async () => (await mowerState(request, owner, mowerId))?.speed_mps ?? null, {
      timeout: 3_000,
      intervals: [200],
    })
    .toBe(0);

  // The picture runs again; the stick, still held forward, stays latched at zero.
  await freeze(page, false);
  await expect(page.getByTestId('camera-frozen')).toHaveCount(0, { timeout: 3_000 });
  await expect(page.getByTestId('drive-held')).toContainText('neu ansetzen');
  const thawedAt = Date.now();
  await page.waitForTimeout(1_200);
  expect(frames.filter((f) => f.at > thawedAt).every((f) => f.linear === 0)).toBe(true);
  // Let go and touch again: it drives.
  await page.mouse.up();
  await expect(page.getByTestId('drive-held')).toHaveCount(0);
  const retouchAt = Date.now();
  await pushForward(page, pad);
  await expect
    .poll(() => frames.filter((f) => f.at > retouchAt && f.linear > 0).length, { timeout: 3_000 })
    .toBeGreaterThan(0);
  await page.mouse.up();
  await expect(pad).toHaveAttribute('data-engaged', 'false');

  // --- a phone upright -----------------------------------------------------------------------
  await page.setViewportSize(UPRIGHT);
  await expect(drive).toHaveAttribute('data-layout', 'upright');
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect(stage).toHaveAttribute('data-phase', 'live');
  await expectPatternRunning(page);
  const camBox = (await stage.boundingBox())!;
  expect(Math.abs(camBox.width / camBox.height - 16 / 9)).toBeLessThan(0.05);
  await expect(page.getByTestId('drive-hud')).toBeVisible();
  await expect(page.getByTestId('drive-details')).toBeVisible();
  await expectInViewport(page, page.getByTestId('emergency-stop'), 'emergency stop (upright)');
  await expectInViewport(page, page.getByTestId('drive-stop'), 'session stop (upright)');
  await expectInViewport(page, pad, 'joystick (upright)');
  await expectNoSidewaysOverflow(page);
  await expectLightPlaced(page, 'upright', pad);
  await switchLight(page, 'off');
  await expect(page.getByTestId('drive-state')).toHaveText('Fahrt aktiv');
  await page.waitForTimeout(300);
  await shot('drive-camera-phone');

  // --- a phone held sideways -----------------------------------------------------------------
  await page.setViewportSize(SIDEWAYS);
  await expect(drive).toHaveAttribute('data-layout', 'sideways');
  await expect(page.getByTestId('stop-bar')).toHaveAttribute('data-variant', 'rail');
  await expectPatternRunning(page);
  await expectInViewport(page, page.getByTestId('emergency-stop'), 'emergency stop (sideways)');
  await expectInViewport(page, page.getByTestId('drive-stop'), 'session stop (sideways)');
  await expect(pad).toHaveAttribute('data-variant', 'overlay');
  await expectInViewport(page, pad, 'joystick (sideways)');
  const scroll = await page.evaluate(() => ({
    x: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    y: document.documentElement.scrollHeight - document.documentElement.clientHeight,
  }));
  expect(scroll.x).toBeLessThanOrEqual(0);
  expect(scroll.y).toBeLessThanOrEqual(0);
  // The light in reach of the right thumb, above the overlay stick, clear of the stop rail.
  await expectLightPlaced(page, 'sideways', pad);
  await switchLight(page, 'on');
  await expect(page.getByTestId('drive-state')).toHaveText('Fahrt aktiv');
  // The overlay stick drives over the picture.
  const sidewaysAt = Date.now();
  await pushForward(page, pad);
  await expect
    .poll(() => frames.filter((f) => f.at > sidewaysAt && f.linear > 0).length, { timeout: 3_000 })
    .toBeGreaterThan(0);
  await page.waitForTimeout(300);
  await shot('drive-camera-sideways');
  await page.mouse.up();

  // --- leaving ends the drive session and the camera -----------------------------------------
  await page.setViewportSize(DESKTOP);
  await expect(drive).toHaveAttribute('data-layout', 'desktop');
  // Exactly one camera session for the whole visit, through every layout.
  expect((sessions.get(mowerId) ?? 0) - before).toBe(1);
  // The light belongs to the camera view: none over the map.
  await page.getByTestId('drive-view').getByText('Karte').click();
  await expect(drive).toHaveAttribute('data-view', 'map');
  await expect(page.getByTestId('drive-light')).toHaveCount(0);
  await page.getByTestId('drive-view').getByText('Kamera').click();
  await expect(drive).toHaveAttribute('data-view', 'camera');
  await expect(page.getByTestId('drive-light')).toBeVisible();
  await page.getByRole('link', { name: 'Zurück' }).first().click();
  await page.waitForURL(new RegExp(`/mowers/${mowerId}$`));
  await expect(page.getByTestId('drive-page')).toHaveCount(0);
  // Only the mower page's own panel is left: no second viewer of the same mower.
  await expect(page.getByTestId('camera-stage')).toHaveCount(1);
  const auth = { Authorization: `Bearer ${owner.token}` };
  await expect
    .poll(
      async () => {
        const response = await request.get(`/api/v1/fleet/mowers/${mowerId}/drive-sessions`, {
          headers: auth,
        });
        const body = (await response.json()) as { items: { end_reason?: string | null }[] };
        return body.items[0]?.end_reason ?? null;
      },
      { timeout: 10_000 },
    )
    .toBe('client_stop');

  // --- a mower without a camera gets the map ------------------------------------------------
  await page.goto(`/mowers/${blindId}/drive`);
  const blind = page.getByTestId('drive-page');
  await expect(blind).toHaveAttribute('data-view', 'map', { timeout: 30_000 });
  await expect(page.getByTestId('drive-view')).toHaveCount(0);
  await expect(page.getByTestId('camera-stage')).toHaveCount(0);
  await expectInViewport(page, page.getByTestId('emergency-stop'), 'emergency stop (no camera)');
  await page.waitForTimeout(1_000);
  expect(sessions.get(blindId) ?? 0).toBe(0);
  // The light needs the camera view: the blind mower has a light but shows the map.
  await expect(page.getByTestId('drive-light')).toHaveCount(0);

  // --- a mower without a switchable light: the camera, but no light chip --------------------
  await page.goto(`/mowers/${unlitId}/drive`);
  await expect(page.getByTestId('drive-page')).toHaveAttribute('data-view', 'camera', {
    timeout: 30_000,
  });
  await expect(page.getByTestId('camera-stage')).toHaveAttribute('data-phase', 'live', {
    timeout: 30_000,
  });
  await expect(page.getByTestId('drive-view')).toBeVisible();
  await expect(page.getByTestId('drive-light')).toHaveCount(0);
  csp.assertNone();
});
