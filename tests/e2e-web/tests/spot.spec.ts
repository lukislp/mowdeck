/**
 * Spot mowing (ADR 0039) on the mower page, desktop, against the stack's simulated mower: "Spot
 * mähen" puts the map into spot mode, a tap places a 1.5 m circle and opens the sheet (radius
 * slider, pattern, spacing, speed capped at 0.3 m/s, blades off as a dry run, the start
 * conditions as rows). Whether the gateway runs spots is probed with a `spot_stop` over the API:
 * a gateway without spot runs rejects it as unsupported and the run part is skipped cleanly.
 * With spot runs, a start with all conditions green shows the run bar; STOP ends the run with
 * its reason in words. Unmet conditions (the simulator starts docked) are recorded, not failed.
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

interface Caps {
  manual_drive?: boolean;
  manual_drive_blades?: boolean;
}

async function capabilities(
  request: APIRequestContext,
  who: Actor,
  mowerId: string,
): Promise<Caps | null> {
  const response = await request.get(`/api/v1/telemetry/mowers/${mowerId}/capabilities`, {
    headers: { Authorization: `Bearer ${who.token}` },
  });
  return response.ok() ? ((await response.json()) as { capabilities: Caps }).capabilities : null;
}

/**
 * Send `spot_stop` and wait for its outcome: false when Fleet refuses the kind or the gateway
 * answers that the connection has no spot runs.
 */
async function spotRunsSupported(
  request: APIRequestContext,
  who: Actor,
  mowerId: string,
): Promise<{ supported: boolean; why: string }> {
  const auth = { Authorization: `Bearer ${who.token}` };
  const sent = await request.post(`/api/v1/fleet/mowers/${mowerId}/commands`, {
    headers: auth,
    data: { command: { kind: 'spot_stop' } },
  });
  if (sent.status() !== 202) return { supported: false, why: `Fleet answered ${sent.status()}` };
  const { command_id: commandId } = (await sent.json()) as { command_id: string };
  let record: { status: string; reason?: string | null } = { status: 'pending' };
  await expect
    .poll(
      async () => {
        const r = await request.get(`/api/v1/fleet/commands/${commandId}`, { headers: auth });
        if (r.ok()) record = (await r.json()) as typeof record;
        return record.status;
      },
      { timeout: 30_000, intervals: [500] },
    )
    .not.toBe('pending');
  const reason = record.reason ?? '';
  if (record.status === 'rejected' && /unsupported|not supported|unknown/i.test(reason)) {
    return { supported: false, why: `gateway: ${reason}` };
  }
  return { supported: true, why: `${record.status} ${reason}`.trim() };
}

test('spot mowing: spot mode, the sheet, and a run when the gateway offers spot runs', async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('spot'));
  const mowerId = await createMowerByApi(request, owner, `Luba spot ${RUN}`);
  await waitForMap(request, owner, mowerId);
  let caps: Caps | null = null;
  await expect
    .poll(
      async () => {
        caps = await capabilities(request, owner, mowerId);
        return caps !== null;
      },
      { timeout: 60_000 },
    )
    .toBe(true);
  test.skip(!caps!.manual_drive, 'the connection does not drive manually: no spot mowing');

  await loginByUi(page, owner);
  await page.goto(`/mowers/${mowerId}`);
  const map = page.getByTestId('mower-map');
  await expect(map).toBeVisible();
  await expect(page.getByTestId('mower-marker')).toBeVisible({ timeout: 60_000 });

  // --- spot mode: the chip says what a tap does; a tap next to the mower places the circle ---
  await page.getByTestId('spot-open').click();
  const chip = page.getByTestId('spot-chip');
  await expect(chip).toContainText('Tippe auf die Karte');
  const marker = (await page.getByTestId('mower-marker').boundingBox())!;
  await page.mouse.click(marker.x + marker.width / 2 + 24, marker.y + marker.height / 2);

  const sheet = page.getByTestId('spot-sheet');
  await expect(sheet).toBeVisible();
  await expect(sheet.getByTestId('spot-radius-value')).toHaveText('1,5 m');
  await expect(sheet.getByTestId('spot-speed')).toHaveAttribute('max', '0.3');
  await expect(sheet.getByTestId('spot-conditions').locator('li')).toHaveCount(8);
  if (!caps!.manual_drive_blades) {
    await expect(sheet).toContainText('nur Trockenlauf');
  }
  await expect(sheet.getByTestId('spot-start')).toContainText('Trockenlauf');

  const probe = await spotRunsSupported(request, owner, mowerId);
  if (!probe.supported) {
    test.info().annotations.push({ type: 'skip', description: `spot run: ${probe.why}` });
    await sheet.getByTestId('spot-cancel').click();
    await expect(chip).toHaveCount(0);
    csp.assertNone();
    return;
  }

  const start = sheet.getByTestId('spot-start');
  if (await start.isDisabled()) {
    // The simulator starts docked and without a drive link of its own: the rows say why.
    const red = await sheet.locator('[data-testid="spot-conditions"] li[data-ok="false"]').count();
    expect(red).toBeGreaterThan(0);
    test.info().annotations.push({
      type: 'skip',
      description: `spot run: ${red} start condition(s) unmet against the simulator`,
    });
    csp.assertNone();
    return;
  }

  // --- the run: run bar with progress, STOP ends it with the reason in words ----------------
  await start.click();
  const runBar = page.getByTestId('spot-run');
  await expect(runBar).toBeVisible({ timeout: 30_000 });
  await expect(runBar.getByTestId('spot-run-blades')).toContainText('Trockenlauf');
  await page.getByTestId('spot-stop').click();
  const ended = page.getByTestId('spot-end');
  await expect(ended).toBeVisible({ timeout: 30_000 });
  await expect(ended.getByTestId('spot-end-reason')).toContainText('per Stopp beendet');
  csp.assertNone();
});
