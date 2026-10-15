/**
 * Spot mowing on a phone (390 x 844, touch), against the stack's simulated mower: "Spot mähen"
 * puts the map full screen, a tap places the spot and the spot sheet opens as a bottom sheet at
 * peek, so the map stays visible above it. Peek shows the summary, the conditions lamp and
 * START; a tap or a drag on the handle goes to half (the settings), Escape back to peek, a tap
 * on the map collapses to peek again, the lamp opens half on the start conditions. The run part
 * is guarded like the desktop spec: skipped when the gateway has no spot runs or a start
 * condition is unmet; with a run, the run bar's STOP sits in the peek and the sheet stays there.
 */
import { expect, test, type Locator, type Page } from '@playwright/test';

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

/** Tap the map 24 px right of the mower marker (inside its area on the simulator's map). */
async function tapNextToMower(page: Page): Promise<void> {
  const marker = (await page.getByTestId('mower-marker').boundingBox())!;
  await page.touchscreen.tap(marker.x + marker.width / 2 + 24, marker.y + marker.height / 2);
}

/** The visible part of the map above the sheet, in px. */
async function mapAboveSheet(map: Locator, sheet: Locator): Promise<number> {
  const m = (await map.boundingBox())!;
  const s = (await sheet.boundingBox())!;
  return s.y - m.y;
}

test('spot mowing on a phone: a bottom sheet with snap points keeps the map visible', async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('spotphone'));
  const mowerId = await createMowerByApi(request, owner, `Luba spot phone ${RUN}`);
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
  await expect(page.getByTestId('mower-marker')).toBeVisible({ timeout: 60_000 });

  // --- spot mode: the map goes full screen; a tap places the spot ---------------------------
  await page.getByTestId('spot-open').tap();
  await expect(page.getByTestId('spot-chip')).toContainText('Tippe auf die Karte');
  await expect(map).toBeInViewport({ ratio: 0.95 });
  await tapNextToMower(page);
  await expect(map).toHaveAttribute('data-spot', 'circle');

  // --- peek: summary, conditions lamp and START; the map stays visible above ----------------
  const sheet = page.getByTestId('spot-sheet');
  await expect(sheet).toHaveAttribute('data-snap', 'peek');
  await expect(map).toBeVisible();
  expect(await mapAboveSheet(map, sheet)).toBeGreaterThan(400);
  await expect(map).toHaveAttribute('data-inset-bottom', /^\d+$/);
  await expect(sheet.getByTestId('spot-peek-summary')).toHaveText('Spirale · 1,5 m');
  await expect(sheet.getByTestId('spot-peek-conditions')).toContainText('/8');
  const start = sheet.getByTestId('spot-start');
  await expect(start).toBeInViewport();
  await expect(start).toContainText('Trockenlauf');
  await expect(sheet.getByTestId('spot-radius')).toBeHidden();

  // --- a tap on the handle goes to half: the settings, START still at hand ------------------
  const handle = sheet.getByTestId('spot-sheet-handle');
  await expect(handle).toHaveAttribute('aria-expanded', 'false');
  await handle.tap();
  await expect(sheet).toHaveAttribute('data-snap', 'half');
  await expect(handle).toHaveAttribute('aria-expanded', 'true');
  await expect(sheet.getByTestId('spot-radius')).toBeVisible();
  await expect(sheet.getByTestId('spot-speed')).toHaveAttribute('max', '0.3');
  await expect(start).toBeInViewport();
  expect(await mapAboveSheet(map, sheet)).toBeGreaterThan(300);
  await sheet.getByTestId('spot-radius').fill('2');
  await expect(sheet.getByTestId('spot-radius-value')).toHaveText('2,0 m');
  await expect(sheet.getByTestId('spot-peek-summary')).toHaveText('Spirale · 2,0 m');

  // --- Escape back to peek; a drag up on the handle opens it again --------------------------
  await page.keyboard.press('Escape');
  await expect(sheet).toHaveAttribute('data-snap', 'peek');
  // The snap attribute flips at once while the sheet still slides down: wait until the handle
  // has settled before aiming the drag at it.
  let last = -1;
  await expect
    .poll(
      async () => {
        const top = (await handle.boundingBox())?.y ?? -1;
        const settled = Math.abs(top - last) < 0.5;
        last = top;
        return settled;
      },
      { intervals: [100] },
    )
    .toBe(true);
  const grip = (await handle.boundingBox())!;
  const x = grip.x + grip.width / 2;
  const y = grip.y + grip.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x, y - 260, { steps: 8 });
  await page.mouse.up();
  await expect(sheet).not.toHaveAttribute('data-snap', 'peek');
  await expect(start).toBeInViewport();

  // --- a tap on the map moves the spot and collapses the sheet to peek ----------------------
  await tapNextToMower(page);
  await expect(sheet).toHaveAttribute('data-snap', 'peek');
  await expect(map).toHaveAttribute('data-spot', 'circle');

  // --- the lamp opens half on the start conditions; full shows all of them -----------------
  await sheet.getByTestId('spot-peek-conditions').tap();
  await expect(sheet).toHaveAttribute('data-snap', 'half');
  await expect(sheet.getByTestId('spot-conditions')).toBeInViewport();
  await handle.tap();
  await expect(sheet).toHaveAttribute('data-snap', 'full');
  await expect(sheet.getByTestId('spot-conditions').locator('li')).toHaveCount(8);
  await expect(sheet.getByTestId('spot-conditions')).toContainText('jünger als 60 s');
  expect(await mapAboveSheet(map, sheet)).toBeGreaterThan(40);
  await page.keyboard.press('Escape');
  await expect(sheet).toHaveAttribute('data-snap', 'peek');

  // --- the run, guarded like the desktop spec -------------------------------------------------
  const leave = async () => {
    await page.getByTestId('spot-chip-cancel').tap();
    await expect(sheet).toHaveCount(0);
    await expect(page.getByTestId('spot-chip')).toHaveCount(0);
  };
  const probe = await spotRunsSupported(request, owner, mowerId);
  if (!probe.supported) {
    test.info().annotations.push({ type: 'skip', description: `spot run: ${probe.why}` });
    await leave();
    csp.assertNone();
    return;
  }
  if (await start.isDisabled()) {
    const lamp = sheet.getByTestId('spot-peek-conditions');
    await expect(lamp).toHaveAttribute('data-ok', 'false');
    test.info().annotations.push({
      type: 'skip',
      description: 'spot run: start condition(s) unmet against the simulator',
    });
    await leave();
    csp.assertNone();
    return;
  }

  await start.tap();
  const stop = sheet.getByTestId('spot-stop');
  await expect(stop).toBeInViewport({ timeout: 30_000 });
  await expect(sheet).toHaveAttribute('data-snap', 'peek');
  await handle.tap();
  await expect(sheet).toHaveAttribute('data-snap', 'peek');
  await expect(stop).toBeInViewport();
  await stop.tap();
  const ended = sheet.getByTestId('spot-end');
  await expect(ended).toBeVisible({ timeout: 30_000 });
  await expect(ended.getByTestId('spot-end-reason')).toContainText('per Stopp beendet');
  await sheet.getByTestId('spot-dismiss').tap();
  await expect(sheet).toHaveCount(0);
  csp.assertNone();
});
