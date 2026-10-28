/**
 * Mowing angle rotation (desktop, 1280 x 800; ADR 0061): a schedule that turns the angle by a
 * step on every run shows the angle of each upcoming run in its preview, starting from the base
 * angle; the editor offers the step as chips with a dial of the next angles, and the mower page's
 * start sheet offers "Mähwinkel drehen" with the angle the start will use.
 */
import { expect, test } from '@playwright/test';

import {
  RUN,
  actor,
  createMowerByApi,
  loginByUi,
  registerByApi,
  waitForMap,
  watchCsp,
} from './support';

const SCHEDULER = '/api/v1/scheduler';
const EVERY_DAY = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

test('angle rotation: a step per run in the preview, the editor and the start sheet', async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('rotation'));
  const mowerId = await createMowerByApi(request, owner, `Luba rotate ${RUN}`);
  await waitForMap(request, owner, mowerId);
  const auth = { Authorization: `Bearer ${owner.token}` };

  const created = await request.post(`${SCHEDULER}/schedules`, {
    headers: auth,
    data: {
      name: `Rotation ${RUN}`,
      target_type: 'mower',
      target_id: mowerId,
      kind: 'weekly',
      days_of_week: EVERY_DAY,
      start_time: '05:00',
      duration_minutes: 60,
      pattern: { angle_deg: 30 },
      rotation_step_deg: 45,
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  const schedule = (await created.json()) as { id: string; rotation_step_deg: number };
  expect(schedule.rotation_step_deg).toBe(45);

  // The API: each firing continues from the one before, modulo 180 degrees.
  const preview = await request.get(`${SCHEDULER}/schedules/${schedule.id}/preview?count=5`, {
    headers: auth,
  });
  expect(preview.status(), await preview.text()).toBe(200);
  const items = ((await preview.json()) as { items: { angle_deg: number | null }[] }).items;
  expect(items.map((i) => i.angle_deg)).toEqual([30, 75, 120, 165, 30]);

  await loginByUi(page, owner);
  await page.goto(`/schedules/${schedule.id}`);
  await expect(page.getByTestId('preview-angle')).toHaveText(['30°', '75°', '120°', '165°', '30°']);
  await expect(page.getByTestId('preview-rotation')).toBeVisible();

  // The editor: rotation by step, +45° chosen, the dial's sequence from the base angle. The
  // rotation section needs the mower's capabilities, which may arrive a moment later.
  const section = page.getByTestId('sched-rotation-section');
  await expect(async () => {
    if (!(await section.isVisible())) await page.reload();
    await expect(section).toBeVisible({ timeout: 5_000 });
  }).toPass({ timeout: 60_000 });
  await expect(section.getByTestId('sched-rotation-step-45')).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  const sequence = section.getByTestId('sched-rotation-sequence');
  await expect(sequence).toContainText('30° → 75° → 120°');
  await section.getByTestId('sched-rotation-step-90').click();
  await expect(sequence).toContainText('30° → 120° → 30°');
  await expect(section.getByTestId('sched-rotation-dial')).toBeVisible();

  // The start sheet: rotation is offered and shows the angle of this start (no history yet:
  // the sheet's own angle).
  await page.goto(`/mowers/${mowerId}`);
  const start = page.getByRole('button', { name: 'Starten', exact: true }).first();
  await expect(start).toBeEnabled({ timeout: 60_000 }); // needs the map snapshot
  await start.click();
  const picker = page.getByTestId('area-picker');
  await picker.locator('[data-area="front"]').click();
  await expect(picker.getByTestId('start-rotate')).toBeVisible();
  const toggle = picker.getByTestId('start-rotate-switch');
  if ((await toggle.getAttribute('aria-checked')) !== 'true') await toggle.click();
  await expect(picker.getByTestId('start-rotate-hint')).toContainText('mäht mit 0°');

  csp.assertNone();
});
