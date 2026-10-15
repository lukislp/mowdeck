/**
 * Drive calibration (ADR 0042) in the mower settings, desktop (1280 x 800), against the stack's
 * simulated mower: the joystick drives it out of the dock onto the lawn, the settings page shows
 * the start conditions (all green), START runs the
 * calibration and the end notice with the step table appears. With the result on the page there
 * is exactly one vertical scroller, the page's own: no box around the panel (the step table's
 * horizontal scroll box included) scrolls vertically next to it.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import {
  RUN,
  actor,
  capabilities,
  loginByUi,
  registerByApi,
  waitForMap,
  watchCsp,
  type Actor,
  type SpotCaps,
} from './support';

/** A simulated mower in real time (the stack's default time scale would race the steps). */
async function createRealTimeMower(
  request: APIRequestContext,
  owner: Actor,
  name: string,
): Promise<string> {
  const response = await request.post('/api/v1/fleet/mowers', {
    headers: { Authorization: `Bearer ${owner.token}` },
    data: { name, connection_kind: 'simulated', connection_options: { time_scale: 1 } },
  });
  expect(response.status(), await response.text()).toBe(201);
  return ((await response.json()) as { id: string }).id;
}

/** Every element on the page that scrolls vertically right now (content taller than its box). */
async function verticalScrollers(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const found: string[] = [];
    const root = document.scrollingElement;
    if (root && root.scrollHeight > root.clientHeight) found.push('document');
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('body *'))) {
      const overflowY = getComputedStyle(el).overflowY;
      if ((overflowY === 'auto' || overflowY === 'scroll') && el.scrollHeight > el.clientHeight) {
        const id = el.id ? `#${el.id}` : '';
        const testid = el.dataset.testid ? `[data-testid=${el.dataset.testid}]` : '';
        found.push(`${el.tagName.toLowerCase()}${id}${testid}.${el.className}`);
      }
    }
    return found;
  });
}

/** The ancestors of the panel that scroll vertically (overflow-y auto or scroll, and taller). */
async function scrollingAncestors(page: Page, testid: string): Promise<string[]> {
  return page.getByTestId(testid).evaluate((panel) => {
    const found: string[] = [];
    for (let el = panel.parentElement; el; el = el.parentElement) {
      const overflowY = getComputedStyle(el).overflowY;
      if ((overflowY === 'auto' || overflowY === 'scroll') && el.scrollHeight > el.clientHeight) {
        found.push(`${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}`);
      }
    }
    return found;
  });
}

test('drive calibration: a finished run leaves the page with a single vertical scroller', async ({
  page,
  request,
}) => {
  test.setTimeout(300_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('calib'));
  const mowerId = await createRealTimeMower(request, owner, `Luba calib ${RUN}`);
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
  test.skip(!caps!.manual_drive, 'the connection does not drive manually: no calibration');

  // Out of the dock (x = -1 in the simulator's garden): forward until the mower stands about
  // 1.5 m into the front lawn, so the calibration's forward steps (about 7.5 m in all) stay
  // inside it. The simulator reports its local position in `raw.x`.
  const auth = { Authorization: `Bearer ${owner.token}` };
  await loginByUi(page, owner);
  await page.goto(`/mowers/${mowerId}/drive`);
  await expect(page.getByTestId('drive-state')).toHaveText('Startklar');
  await page.getByTestId('drive-start').click();
  await expect(page.getByTestId('drive-state')).toHaveText('Fahrt aktiv');
  const pad = page.getByTestId('joystick');
  await expect(pad).toHaveAttribute('aria-disabled', 'false');
  await pad.scrollIntoViewIfNeeded();
  const box = (await pad.boundingBox())!;
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  await page.mouse.move(cx, cy);
  await page.mouse.down();
  await page.mouse.move(cx, cy - box.height * 0.5, { steps: 5 });
  await expect
    .poll(
      async () => {
        const response = await request.get(`/api/v1/telemetry/mowers/${mowerId}/state`, {
          headers: auth,
        });
        const body = (await response.json()) as { state: { raw: { x?: number } } };
        return body.state.raw.x ?? -1;
      },
      { timeout: 60_000, intervals: [250] },
    )
    .toBeGreaterThan(1.2);
  await page.mouse.up();
  await page.getByTestId('drive-stop').click();
  await expect(page.getByTestId('drive-state')).toHaveText('Startklar');
  // The gateway refuses a calibration while a drive session is open: wait until it ended.
  await expect
    .poll(
      async () => {
        const response = await request.get(`/api/v1/fleet/mowers/${mowerId}/drive-sessions`, {
          headers: auth,
        });
        const body = (await response.json()) as { items: { end_reason?: string | null }[] };
        return body.items[0]?.end_reason ?? null;
      },
      { timeout: 15_000 },
    )
    .toBe('client_stop');
  // The controller releases its drive session with the dead-man timer about a second after the
  // last frame.
  await page.waitForTimeout(3_000);

  // The settings page: every start condition green, the room confirmed, START.
  await page.goto(`/mowers/${mowerId}/settings`);
  const panel = page.getByTestId('calib');
  await expect(panel).toBeVisible();
  const conditions = page.getByTestId('calib-conditions');
  await expect(conditions.locator('li[data-ok="false"]')).toHaveCount(0, { timeout: 60_000 });
  await page.getByTestId('calib-room-check').check();
  await page.getByTestId('calib-start').click();
  await expect(page.getByTestId('calib-live')).toBeVisible();

  // The run ends (done, or aborted when the lawn ahead runs out) with its step table.
  await expect(page.getByTestId('calib-end')).toBeVisible({ timeout: 180_000 });
  await expect(page.getByTestId('calib-steps').locator('tbody tr').first()).toBeVisible();

  // One vertical scroller: the page's (the shell's main area on the desktop), nothing inside.
  const scrollers = await verticalScrollers(page);
  expect(
    scrollers.filter((s) => !s.startsWith('main#main')),
    scrollers.join(', '),
  ).toEqual([]);
  expect(await scrollingAncestors(page, 'calib-steps')).toEqual(
    scrollers.some((s) => s.startsWith('main#main')) ? ['main#main'] : [],
  );
  csp.assertNone();
});
