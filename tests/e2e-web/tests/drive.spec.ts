/**
 * Phase 3 gate item 4, manual drive with the joystick (desktop, 1280 x 800), against the stack's
 * simulated mower: the detail page offers "drive manually"; the drive page opens a session over
 * the Realtime socket; holding the pad forward for 2 s (real pointer events) moves the mower;
 * after letting go it stands still (speed 0) within 2 s; "Stop" ends the session and Fleet
 * records it (`GET /mowers/{id}/drive-sessions`, reason `client_stop`).
 */
import { expect, test, type APIRequestContext } from '@playwright/test';

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

interface LiveState {
  activity: string;
  speed_mps: number | null;
  position: { lat: number; lon: number } | null;
  observed_at: string;
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

/** Distance in metres between two WGS84 points (small distances). */
function metres(a: { lat: number; lon: number }, b: { lat: number; lon: number }): number {
  const k = 111_320;
  const dx = (b.lon - a.lon) * k * Math.cos((a.lat * Math.PI) / 180);
  const dy = (b.lat - a.lat) * k;
  return Math.hypot(dx, dy);
}

test('drive: a session moves the mower and it stops after the stick is released', async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('drive'));
  const mowerId = await createMowerByApi(request, owner, `Luba drive ${RUN}`);
  await waitForMap(request, owner, mowerId);
  const auth = { Authorization: `Bearer ${owner.token}` };
  await expect
    .poll(
      async () =>
        (
          await request.get(`/api/v1/telemetry/mowers/${mowerId}/capabilities`, { headers: auth })
        ).status(),
      { timeout: 60_000 },
    )
    .toBe(200);
  await expect
    .poll(async () => (await mowerState(request, owner, mowerId))?.position ?? null, {
      timeout: 60_000,
    })
    .not.toBeNull();

  await loginByUi(page, owner);
  await page.goto(`/mowers/${mowerId}`);
  await page.getByTestId('mower-drive').click();
  await page.waitForURL(new RegExp(`/mowers/${mowerId}/drive$`));
  await expect(page.getByTestId('drive-state')).toHaveText('Startklar');
  await expect(page.getByTestId('joystick')).toHaveAttribute('aria-disabled', 'true');

  // Start the session.
  await page.getByTestId('drive-start').click();
  await expect(page.getByTestId('drive-state')).toHaveText('Fahrt aktiv');
  await expect(page.getByTestId('drive-session-id')).not.toHaveText('—');
  const pad = page.getByTestId('joystick');
  await expect(pad).toHaveAttribute('aria-disabled', 'false');

  const before = (await mowerState(request, owner, mowerId))!;
  expect(before.position).not.toBeNull();

  // Hold the pad forward for 2 s with real pointer events.
  await pad.scrollIntoViewIfNeeded();
  const box = (await pad.boundingBox())!;
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  await page.mouse.move(cx, cy);
  await page.mouse.down();
  await page.mouse.move(cx, cy - box.height * 0.45, { steps: 5 });
  await expect(pad).toHaveAttribute('data-engaged', 'true');
  await expect(page.getByTestId('drive-rate-sent')).not.toHaveText(/^0/, { timeout: 2_000 });
  await page.waitForTimeout(2_000);
  await expect
    .poll(
      async () => {
        const now = await mowerState(request, owner, mowerId);
        return now?.position ? metres(before.position!, now.position) : 0;
      },
      { timeout: 5_000, intervals: [250] },
    )
    .toBeGreaterThan(0.5);
  await expect(page.getByTestId('drive-rate-acked')).not.toHaveText(/^0/);

  // Let go: one zero frame, then nothing; the mower stands still within 2 s.
  await page.mouse.up();
  const released = Date.now();
  await expect(pad).toHaveAttribute('data-engaged', 'false');
  await expect
    .poll(async () => (await mowerState(request, owner, mowerId))?.speed_mps ?? null, {
      timeout: 2_000,
      intervals: [100],
    })
    .toBe(0);
  expect(Date.now() - released).toBeLessThan(2_500);
  await expect(page.getByTestId('drive-speed')).toContainText('0,00');
  // It stays put: two readings a second apart agree.
  const still = (await mowerState(request, owner, mowerId))!;
  await page.waitForTimeout(1_200);
  const later = (await mowerState(request, owner, mowerId))!;
  expect(metres(still.position!, later.position!)).toBeLessThan(0.05);

  // Stop the session; Fleet records it.
  await page.getByTestId('drive-stop').click();
  await expect(page.getByTestId('drive-state')).toHaveText('Startklar');
  await expect(page.getByText('Fahrt beendet', { exact: true })).toBeVisible();
  await expect
    .poll(
      async () => {
        const response = await request.get(`/api/v1/fleet/mowers/${mowerId}/drive-sessions`, {
          headers: auth,
        });
        expect(response.status()).toBe(200);
        const body = (await response.json()) as {
          items: { end_reason?: string | null; frames?: number | null }[];
        };
        return body.items[0]?.end_reason ?? null;
      },
      { timeout: 10_000 },
    )
    .toBe('client_stop');
  const sessions = (await (
    await request.get(`/api/v1/fleet/mowers/${mowerId}/drive-sessions`, { headers: auth })
  ).json()) as { items: { frames?: number | null; blade_on_allowed: boolean }[] };
  expect(sessions.items).toHaveLength(1);
  expect(sessions.items[0]!.frames ?? 0).toBeGreaterThan(5);

  // The emergency stop is on the drive page as everywhere else.
  await expect(page.getByRole('button', { name: 'Not-Stopp' })).toBeVisible();
  csp.assertNone();
});
