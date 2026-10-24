/**
 * Start settings of a schedule (desktop, 1280 x 800; ADR 0024 amendment 2026-10-24): the
 * editor offers exactly what the simulated mower declares, with the start sheet's own controls
 * (pattern, angle, blade height, speed, and behind "Mehr" obstacle detection, path order and
 * laps around no-go zones, never the start progress); the list shows them compactly, and a run
 * sends the blade height and speed first, then `start_mow` with the schedule's settings over the
 * mow defaults, which the simulator executes.
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

const SCHEDULER = '/api/v1/scheduler';

type Command = { kind: string; status: string; pattern?: Record<string, unknown> | null };

async function commands(
  request: APIRequestContext,
  owner: Actor,
  mowerId: string,
): Promise<Command[]> {
  const response = await request.get(`/api/v1/fleet/mowers/${mowerId}/commands?limit=20`, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  return ((await response.json()) as { items: Command[] }).items;
}

async function mowerState(request: APIRequestContext, owner: Actor, mowerId: string) {
  const response = await request.get(`/api/v1/telemetry/mowers/${mowerId}/state`, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  if (!response.ok()) return null;
  return (
    (await response.json()) as {
      state: { blade_height_mm?: number | null; speed_mps?: number | null };
    }
  ).state;
}

test('schedule options: offered like the start sheet, saved, sent at the run', async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('schedopts'));
  const mowerId = await createMowerByApi(request, owner, `Luba sched ${RUN}`);
  await waitForMap(request, owner, mowerId);
  const auth = { Authorization: `Bearer ${owner.token}` };
  const weather = await request.put(`${SCHEDULER}/dev/weather`, {
    headers: auth,
    data: { mode: 'dry' },
  });
  expect(weather.status(), await weather.text()).toBe(200);
  // Mow defaults the schedule overrides in part.
  const defaults = await request.put(`/api/v1/fleet/mowers/${mowerId}/mow-defaults`, {
    headers: auth,
    data: { pattern: { name: 'stripes', angle_deg: 0, border_laps: 2, order: 'border_first' } },
  });
  expect(defaults.status(), await defaults.text()).toBe(200);

  await loginByUi(page, owner);
  await page.goto(`/schedules/new?mower=${mowerId}`);
  await expect(page.locator('#sched-target')).toHaveValue(mowerId);
  const panel = page.getByTestId('sched-mow');
  // The mower and its model; the capabilities may arrive a moment after the mower connected.
  await expect(async () => {
    if (!(await panel.getByTestId('sched-mow-pattern').isVisible())) await page.reload();
    await expect(panel.getByTestId('sched-mow-pattern')).toBeVisible({ timeout: 5_000 });
  }).toPass({ timeout: 60_000 });
  await expect(panel.getByTestId('sched-mow-members')).toContainText(`Luba sched ${RUN}`);
  await expect(panel.getByTestId('sched-mow-summary')).toHaveText('Standard des Mähers');

  // Only the simulator's patterns, nothing set: the mower's defaults.
  const tiles = panel.getByTestId('sched-mow-pattern').getByRole('radio');
  await expect(tiles).toHaveText(['Streifen', 'Diagonal', 'Schachbrett']);
  await expect(panel.getByTestId('sched-mow-angle')).toContainText('Mäher');
  await expect(panel.getByTestId('sched-mow-spacing')).toHaveCount(0); // no spacing range
  await panel.getByTestId('sched-mow-pattern-diagonal').click();
  const raise = panel.getByRole('button', { name: 'Winkel vergrößern' });
  await raise.click();
  await raise.click();
  await expect(panel.getByTestId('sched-mow-angle').locator('output')).toHaveAttribute(
    'data-value',
    '30',
  );

  // Blade height and speed within the model's ranges.
  const bladeUp = panel.getByRole('button', { name: 'Schnitthöhe erhöhen' });
  for (let i = 0; i < 4; i += 1) await bladeUp.click();
  await expect(panel.getByTestId('sched-mow-blade-stepper-value')).toHaveAttribute(
    'data-value',
    '40',
  );
  const faster = panel.getByRole('button', { name: 'Schneller' });
  for (let i = 0; i < 5; i += 1) await faster.click();
  await expect(panel.getByTestId('sched-mow-speed-stepper-value')).toHaveAttribute(
    'data-value',
    '0.6',
  );

  // Behind "Mehr": the declared job options with the app's names, no start progress.
  await panel.getByTestId('sched-mow-more-toggle').click();
  const obstacle = panel.getByTestId('sched-mow-obstacle');
  await expect(obstacle.getByRole('radio')).toHaveText(['Aus', 'Standard', 'Empfindlich']);
  await obstacle.getByRole('radio', { name: 'Empfindlich' }).click();
  await panel
    .getByTestId('sched-mow-order')
    .getByRole('radio', { name: 'Zuerst Zickzack' })
    .click();
  await panel.getByRole('button', { name: 'Weniger Runden um Sperrzonen' }).click();
  await expect(panel.getByTestId('sched-mow-progress')).toHaveCount(0);
  await expect(panel.getByTestId('sched-mow-summary')).toHaveText(
    'Diagonal · 30° · Empfindlich · Zuerst Zickzack · Sperrzone 0× · 40 mm · 0,60 m/s',
  );

  await page.locator('#sched-name').fill('Optionen');
  const areas = page.getByTestId('sched-areas');
  await expect(page.getByTestId('sched-map')).toHaveAttribute('data-ready', 'true');
  await areas.locator('button[data-area="front"]').click();
  const created = page.waitForResponse(
    (r) => r.url().endsWith(`${SCHEDULER}/schedules`) && r.request().method() === 'POST',
  );
  await page.getByTestId('sched-save').click();
  const createdResponse = await created;
  expect(createdResponse.status(), await createdResponse.text()).toBe(201);
  const schedule = (await createdResponse.json()) as {
    id: string;
    pattern: Record<string, unknown>;
    blade_height_mm: number;
    speed_mps: number;
  };
  expect(schedule.pattern).toMatchObject({
    name: 'diagonal',
    angle_deg: 30,
    border_laps: null,
    obstacle_detection: 'sensitive',
    obstacle_laps: 0,
    order: 'grid_first',
  });
  expect(schedule.blade_height_mm).toBe(40);
  expect(schedule.speed_mps).toBe(0.6);
  await page.waitForURL(/\/schedules$/);

  // The list shows what the schedule sets.
  const row = page.locator(`[data-testid="schedule-row"][data-id="${schedule.id}"]`);
  await expect(row.getByTestId('schedule-mow')).toContainText('Diagonal');
  await expect(row.getByTestId('schedule-mow')).toContainText('Empfindlich');
  await expect(row.getByTestId('schedule-mow')).toContainText('40 mm');

  // Run it now: the simulator gets the setpoints, then the start with the merged settings.
  await row.getByTestId('schedule-run').click();
  const sheet = page.getByTestId('run-sheet');
  await sheet.getByTestId('run-confirm').click();
  await expect(sheet.getByTestId('run-result').locator('[data-value="started"]')).toBeVisible({
    timeout: 30_000,
  });
  await sheet.getByTestId('run-close').click();

  await expect
    .poll(
      async () =>
        (await commands(request, owner, mowerId))
          .filter((c) => ['start_mow', 'set_blade_height', 'set_speed'].includes(c.kind))
          .map((c) => `${c.kind}:${c.status}`)
          .sort(),
      { timeout: 60_000 },
    )
    .toEqual(['set_blade_height:executed', 'set_speed:executed', 'start_mow:executed']);
  const start = (await commands(request, owner, mowerId)).find((c) => c.kind === 'start_mow');
  // Schedule over the mow defaults (the border laps and nothing else come from them).
  expect(start?.pattern).toEqual({
    name: 'diagonal',
    angle_deg: 30,
    border_laps: 2,
    obstacle_detection: 'sensitive',
    obstacle_laps: 0,
    order: 'grid_first',
  });
  await expect
    .poll(async () => (await mowerState(request, owner, mowerId))?.blade_height_mm, {
      timeout: 30_000,
    })
    .toBe(40);
  csp.assertNone();
});
