/**
 * Spot mowing (ADR 0039) on the mower page, desktop, against the stack's simulated mower: "Spot
 * mähen" puts the map into spot mode, a tap places a 1.5 m circle and opens the sheet (radius
 * slider, pattern, spacing, speed capped at 0.3 m/s, blades off as a dry run, the start
 * conditions as rows). Whether the gateway runs spots is probed with a `spot_stop` over the API:
 * a gateway without spot runs rejects it as unsupported and the run part is skipped cleanly.
 * With spot runs, a start with all conditions green shows the run bar; STOP ends the run with
 * its reason in words. Unmet conditions (the simulator starts docked) are recorded, not failed.
 */
import { expect, test } from '@playwright/test';

import {
  RUN,
  actor,
  capabilities,
  createMowerByApi,
  loginByUi,
  registerByApi,
  spotRunsSupported,
  waitForMap,
  watchCsp,
  type SpotCaps,
} from './support';

test('spot mowing: spot mode, the sheet, and a run when the gateway offers spot runs', async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('spot'));
  const mowerId = await createMowerByApi(request, owner, `Luba spot ${RUN}`);
  await waitForMap(request, owner, mowerId);
  let caps: SpotCaps | null = null;
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
