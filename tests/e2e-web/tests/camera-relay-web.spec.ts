/**
 * The web app plays the camera relay (ADR 0045 step 6) against the test stack, which runs
 * camera-relay and MediaMTX with the simulator's `fake` source (a test pattern per camera, H.264
 * after the relay's transcoder). Runs in Chromium (desktop), Firefox (firefox) and WebKit
 * (webkit); a browser without WebRTC (Playwright's WebKit on Windows) is skipped with a reason.
 *
 * - The camera page asks for `transport: auto` and plays the relay over WHEP: the tile's
 *   `<video>` presents frames of the test pattern; the debug overlay shows the relay rows.
 *   "Beide" plays both front cameras on two connections with two different pictures.
 * - A hidden tab detaches the lease at once (`DELETE .../camera/session/{lease}`), the relay
 *   stops the source after its 15 s grace (Fleet's audit row `mower.camera_stream_stopped`),
 *   and the visible tab requests a new session and plays again.
 * - Strict leases: another user of the same account can neither renew nor end the owner's
 *   lease (404 both); the owner's picture keeps playing and its heartbeat keeps renewing.
 * - The drive view over the relay: a frozen source (the relay's test hook pauses the fake
 *   sources) shows "Bild steht", holds the mower and latches the stick until it is let go; the
 *   light chip still switches the simulated mower's light.
 */
import { expect, test, type Locator, type Page } from '@playwright/test';

import {
  RUN,
  actor,
  createAccountViewer,
  createMowerByApi,
  fleetAuditCount,
  freezeRelaySources,
  loginByUi,
  registerByApi,
  setPageVisibility,
  waitForMap,
  watchCsp,
} from './support';

const SESSION = /\/api\/v1\/fleet\/mowers\/[^/]+\/camera\/session$/;
const LEASE = /\/api\/v1\/fleet\/mowers\/[^/]+\/camera\/session\/[0-9a-f-]{36}$/;

interface SessionBody {
  provider: string;
  relay: { lease_id: string; heartbeat_seconds: number } | null;
}

/** Skip in a browser without WebRTC (never fake a picture there). */
async function requireWebRtc(page: Page, browserName: string): Promise<void> {
  await page.goto('/login');
  const available = await page.evaluate(() => typeof RTCPeerConnection === 'function');
  test.skip(
    !available,
    `${browserName} in this Playwright build has no WebRTC (RTCPeerConnection is missing); ` +
      'the relay player cannot run here',
  );
}

/** The `<video>` presents new frames (requestVideoFrameCallback, else decoded frames grow). */
async function presentsFrames(video: Locator): Promise<number> {
  return video.evaluate(async (node) => {
    const element = node as HTMLVideoElement;
    if (typeof element.requestVideoFrameCallback !== 'function') {
      const before = element.getVideoPlaybackQuality?.().totalVideoFrames ?? 0;
      await new Promise((resolve) => setTimeout(resolve, 1_500));
      return (element.getVideoPlaybackQuality?.().totalVideoFrames ?? 0) - before;
    }
    let count = 0;
    await new Promise<void>((resolve) => {
      const tick = (): void => {
        count += 1;
        if (count >= 3) resolve();
        else element.requestVideoFrameCallback(tick);
      };
      element.requestVideoFrameCallback(tick);
      setTimeout(resolve, 4_000);
    });
    return count;
  });
}

/** A coarse fingerprint of the current video frame (every 997th pixel of a small copy). */
async function videoFingerprint(video: Locator): Promise<{ colours: number; print: string }> {
  return video.evaluate((node) => {
    const element = node as HTMLVideoElement;
    const canvas = document.createElement('canvas');
    canvas.width = 160;
    canvas.height = 120;
    const ctx = canvas.getContext('2d')!;
    ctx.drawImage(element, 0, 0, canvas.width, canvas.height);
    const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    const colours = new Set<string>();
    const parts: string[] = [];
    for (let i = 0; i < data.length; i += 4 * 97) {
      colours.add(`${data[i]! >> 4},${data[i + 1]! >> 4},${data[i + 2]! >> 4}`);
      parts.push(`${data[i]! >> 5},${data[i + 1]! >> 5}`);
    }
    return { colours: colours.size, print: parts.join(';') };
  });
}

async function expectRelayLive(page: Page, testid = 'camera-video'): Promise<void> {
  const stage = page.getByTestId('camera-stage');
  await expect(stage).toHaveAttribute('data-provider', 'relay', { timeout: 30_000 });
  await expect(stage).toHaveAttribute('data-transport', 'relay');
  await expect(stage).toHaveAttribute('data-phase', 'live', { timeout: 30_000 });
  const video = page.getByTestId(testid);
  await expect(video).toBeVisible();
  await expect(video).toHaveAttribute('data-camera-video', '');
  await expect.poll(() => presentsFrames(video), { timeout: 15_000 }).toBeGreaterThanOrEqual(3);
  // Not a blank picture: the pattern's bars arrive.
  await expect
    .poll(async () => (await videoFingerprint(video)).colours, { timeout: 10_000 })
    .toBeGreaterThan(3);
}

test('camera relay in the web app: live test pattern, both cameras, a hidden tab detaches', async ({
  page,
  request,
  browserName,
}) => {
  test.setTimeout(240_000);
  await requireWebRtc(page, browserName);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor(`relayweb-${browserName}`));
  const mowerId = await createMowerByApi(request, owner, `Luba relay web ${browserName} ${RUN}`);
  await waitForMap(request, owner, mowerId);

  let posts = 0;
  const deletes: string[] = [];
  page.on('request', (req) => {
    if (req.method() === 'POST' && SESSION.test(req.url())) posts += 1;
    if (req.method() === 'DELETE' && LEASE.test(req.url())) deletes.push(req.url());
  });

  await loginByUi(page, owner);
  const opened = page.waitForResponse(
    (r) => SESSION.test(r.url()) && r.request().method() === 'POST',
  );
  await page.goto(`/mowers/${mowerId}/camera?camdebug=1`);
  const first = (await (await opened).json()) as SessionBody;
  expect(first.provider).toBe('relay');
  await expectRelayLive(page);
  await expect(page.getByTestId('camera-status')).toContainText('Live');
  // The debug overlay reads the relay path (the option `auto`, the path the session took).
  const overlay = page.getByTestId('camera-debug');
  await expect(overlay).toContainText('transport auto > relay');
  await expect(overlay).toContainText('relay state=');
  await expect(overlay).toContainText(`lease=${first.relay!.lease_id.slice(0, 8)}`);
  await expect(overlay).toContainText('whep status=201');
  await expect(overlay).not.toContainText('agora state=');

  // "Beide": both front cameras, two connections, two different pictures.
  const picker = page.getByTestId('camera-picker');
  await picker.getByText('Beide').click();
  await expect(page.getByTestId('camera-tile')).toHaveAttribute('data-status', 'live', {
    timeout: 20_000,
  });
  await expect(page.getByTestId('camera-tile-right')).toHaveAttribute('data-status', 'live', {
    timeout: 20_000,
  });
  await expectRelayLive(page, 'camera-video-right');
  await expect(page.getByTestId('camera-video-right')).toHaveAttribute('data-camera-side', 'right');
  const left = await videoFingerprint(page.getByTestId('camera-video'));
  const right = await videoFingerprint(page.getByTestId('camera-video-right'));
  expect(left.print).not.toBe(right.print);
  await expect(page.getByTestId('camera-same-picture')).toHaveCount(0);
  // Both readers of the one lease keep playing (the relay binds a reader per camera; one reader
  // per lease kicked the left tile once the right one was confirmed, within a few seconds).
  await page.waitForTimeout(8_000);
  await expect(page.getByTestId('camera-tile')).toHaveAttribute('data-status', 'live');
  await expect(page.getByTestId('camera-tile-right')).toHaveAttribute('data-status', 'live');
  expect(await presentsFrames(page.getByTestId('camera-video'))).toBeGreaterThanOrEqual(3);
  expect(await presentsFrames(page.getByTestId('camera-video-right'))).toBeGreaterThanOrEqual(3);
  // One lease for both tiles: switching the layout requested no new session.
  expect(posts).toBe(1);

  // The tab goes hidden: the lease is detached at once, the relay stops after its grace.
  const stoppedBefore = fleetAuditCount(mowerId, 'mower.camera_stream_stopped');
  await setPageVisibility(page, false);
  await expect.poll(() => deletes.length, { timeout: 5_000 }).toBe(1);
  expect(deletes[0]).toContain(first.relay!.lease_id);
  await expect(page.getByTestId('camera-stage')).toHaveAttribute('data-phase', 'connecting');
  await expect
    .poll(() => fleetAuditCount(mowerId, 'mower.camera_stream_stopped'), {
      timeout: 60_000,
      intervals: [2_000],
    })
    .toBeGreaterThan(stoppedBefore);

  // Visible again: a new session (a new lease) on the same path, and the picture is back.
  const reopened = page.waitForResponse(
    (r) => SESSION.test(r.url()) && r.request().method() === 'POST',
  );
  await setPageVisibility(page, true);
  const second = (await (await reopened).json()) as SessionBody;
  expect(second.relay?.lease_id).toBeTruthy();
  expect(second.relay!.lease_id).not.toBe(first.relay!.lease_id);
  await expectRelayLive(page);

  // Leaving the page ends the lease (DELETE on pagehide, and the reader is gone): the relay stops
  // the stream again after its grace.
  const stoppedAfterHide = fleetAuditCount(mowerId, 'mower.camera_stream_stopped');
  await page.goto('/mowers');
  await expect
    .poll(() => fleetAuditCount(mowerId, 'mower.camera_stream_stopped'), {
      timeout: 60_000,
      intervals: [2_000],
    })
    .toBeGreaterThan(stoppedAfterHide);
  csp.assertNone();
});

test('camera relay: another user of the same account can neither renew nor end the lease', async ({
  page,
  request,
  browserName,
}) => {
  test.setTimeout(180_000);
  await requireWebRtc(page, browserName);
  const owner = await registerByApi(request, actor(`relaylease-${browserName}`));
  const housemate = await createAccountViewer(request, owner, `relaylease-mate-${browserName}`);
  const mowerId = await createMowerByApi(request, owner, `Luba lease ${browserName} ${RUN}`);
  await waitForMap(request, owner, mowerId);

  await loginByUi(page, owner);
  const opened = page.waitForResponse(
    (r) => SESSION.test(r.url()) && r.request().method() === 'POST',
  );
  await page.goto(`/mowers/${mowerId}/camera`);
  const session = (await (await opened).json()) as SessionBody;
  const lease = session.relay!.lease_id;
  await expectRelayLive(page);

  // The housemate sees the mower (can_view) but not the owner's lease.
  const base = `/api/v1/fleet/mowers/${mowerId}/camera/session/${lease}`;
  const asMate = { Authorization: `Bearer ${housemate.token}` };
  const renew = await request.post(`${base}/renew`, { headers: asMate });
  expect(renew.status()).toBe(404);
  const end = await request.delete(base, { headers: asMate });
  expect(end.status()).toBe(404);
  // The housemate can open a lease of their own (the same relay session, a second viewer).
  const own = await request.post(`/api/v1/fleet/mowers/${mowerId}/camera/session`, {
    headers: asMate,
    data: { transport: 'relay' },
  });
  expect(own.status(), await own.text()).toBe(200);
  const ownLease = ((await own.json()) as SessionBody).relay!.lease_id;
  expect(
    (
      await request.delete(`/api/v1/fleet/mowers/${mowerId}/camera/session/${ownLease}`, {
        headers: asMate,
      })
    ).status(),
  ).toBe(204);

  // The owner's lease keeps working: the page's heartbeat renews it and the picture plays on.
  const renewed = page.waitForResponse(
    (r) => r.url().endsWith(`/camera/session/${lease}/renew`) && r.status() === 200,
    { timeout: 30_000 },
  );
  await renewed;
  await expect(page.getByTestId('camera-expiry')).toContainText('erneuert');
  await expectRelayLive(page);
  // And the owner may end it.
  const mine = await request.post(`${base}/renew`, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  expect(mine.status()).toBe(200);
});

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

test('drive view over the relay: a frozen source holds the mower and latches the stick; the light still switches', async ({
  page,
  request,
  browserName,
}) => {
  test.setTimeout(240_000);
  await requireWebRtc(page, browserName);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor(`relaydrive-${browserName}`));
  const mowerId = await createMowerByApi(request, owner, `Luba relay drive ${browserName} ${RUN}`);
  await waitForMap(request, owner, mowerId);

  const frames: { at: number; linear: number }[] = [];
  page.on('websocket', (socket) => {
    socket.on('framesent', (event) => {
      try {
        const message = JSON.parse(String(event.payload)) as { type?: string; linear?: number };
        if (message.type === 'drive_frame')
          frames.push({ at: Date.now(), linear: message.linear ?? 0 });
      } catch {
        // not JSON
      }
    });
  });

  await page.setViewportSize({ width: 1280, height: 800 });
  await loginByUi(page, owner);
  await page.goto(`/mowers/${mowerId}/drive`);
  const drive = page.getByTestId('drive-page');
  await expect(drive).toHaveAttribute('data-view', 'camera', { timeout: 30_000 });
  await expectRelayLive(page);
  const stage = page.getByTestId('camera-stage');

  // The light chip switches the simulated mower's light over the relay picture.
  const light = page.getByTestId('drive-light');
  await expect(light).toHaveAttribute('data-state', 'off', { timeout: 30_000 });
  await expect(light).toHaveAttribute('data-pending', 'false', { timeout: 15_000 });
  await light.click();
  await expect(light).toHaveAttribute('data-state', 'on', { timeout: 15_000 });

  await page.getByTestId('drive-start').click();
  await expect(page.getByTestId('drive-state')).toHaveText('Fahrt aktiv');
  const pad = page.getByTestId('joystick');
  await pushForward(page, pad);
  await expect
    .poll(() => frames.filter((f) => f.linear > 0).length, { timeout: 5_000 })
    .toBeGreaterThan(0);
  await expect(stage).toHaveAttribute('data-frozen', 'false');

  try {
    // The relay's source stands: no new frames, the same WebRTC session.
    expect(freezeRelaySources(true)).toBeGreaterThan(0);
    await expect(page.getByTestId('camera-frozen')).toBeVisible({ timeout: 6_000 });
    await expect(page.getByTestId('camera-status')).toContainText('Bild steht');
    await expect(page.getByTestId('drive-held')).toBeVisible();
    await expect(page.getByTestId('drive-pad')).toHaveAttribute('data-held', 'true');
    await expect(light).toBeEnabled();
    const frozenAt = Date.now();
    await page.waitForTimeout(1_000);
    const whileFrozen = frames.filter((f) => f.at > frozenAt);
    expect(whileFrozen.length).toBeGreaterThan(0);
    expect(whileFrozen.every((f) => f.linear === 0)).toBe(true);
  } finally {
    freezeRelaySources(false);
  }

  // The picture runs again; the stick, still held forward, stays latched at zero.
  await expect(page.getByTestId('camera-frozen')).toHaveCount(0, { timeout: 20_000 });
  await expect(page.getByTestId('drive-held')).toContainText('neu ansetzen');
  const thawedAt = Date.now();
  await page.waitForTimeout(800);
  expect(frames.filter((f) => f.at > thawedAt).every((f) => f.linear === 0)).toBe(true);
  await page.mouse.up();
  await expect(page.getByTestId('drive-held')).toHaveCount(0);
  const retouchAt = Date.now();
  await pushForward(page, pad);
  await expect
    .poll(() => frames.filter((f) => f.at > retouchAt && f.linear > 0).length, { timeout: 3_000 })
    .toBeGreaterThan(0);
  await page.mouse.up();
  await expectRelayLive(page);
  await light.click();
  await expect(light).toHaveAttribute('data-state', 'off', { timeout: 15_000 });
  await page.getByTestId('drive-stop').click();
  csp.assertNone();
});
