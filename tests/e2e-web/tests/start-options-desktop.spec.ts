/**
 * Job options of a start (desktop, 1280 x 800; ADR 0030 amendment 2026-10-24): the simulated
 * mower declares obstacle detection, laps around no-go zones, path order and start progress,
 * the start sheet shows them behind "Mehr" with the vendor app's names, a start sends exactly
 * the chosen ones, the simulator starts its route at the chosen progress, and the mow defaults
 * keep the options but not the start progress.
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

type Command = { kind: string; status: string; pattern?: Record<string, unknown> | null };

async function startCommands(
  request: APIRequestContext,
  owner: Actor,
  mowerId: string,
): Promise<Command[]> {
  const response = await request.get(`/api/v1/fleet/mowers/${mowerId}/commands?limit=20`, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  return ((await response.json()) as { items: Command[] }).items.filter(
    (c) => c.kind === 'start_mow',
  );
}

async function jobProgress(
  request: APIRequestContext,
  owner: Actor,
  mowerId: string,
): Promise<number | null> {
  const response = await request.get(`/api/v1/telemetry/mowers/${mowerId}/state`, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  if (!response.ok()) return null;
  const body = (await response.json()) as { state: { job?: { progress_percent?: number } | null } };
  return body.state.job?.progress_percent ?? null;
}

test('start options: shown behind "Mehr", sent with the start, remembered without the progress', async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('startopts'));
  const mowerId = await createMowerByApi(request, owner, `Luba options ${RUN}`);
  await waitForMap(request, owner, mowerId);

  await loginByUi(page, owner);
  await page.goto(`/mowers/${mowerId}`);
  const start = page.getByRole('button', { name: 'Starten' });
  // A fresh account's control grant may reach the authorization store after the page's first
  // permission check: load the page again until the start is offered.
  await expect(async () => {
    if (!(await start.isVisible())) await page.reload();
    await expect(start).toBeEnabled({ timeout: 10_000 });
  }).toPass({ timeout: 90_000 });
  await start.click();
  const picker = page.getByTestId('area-picker');
  await picker.locator('[data-area="front"]').click();
  await expect(picker.locator('[data-area="front"]')).toHaveAttribute('aria-pressed', 'true');

  // The options are folded behind "Mehr" until asked for.
  const more = picker.getByTestId('start-more-toggle');
  await expect(more).toHaveText('Mehr');
  await expect(picker.getByTestId('start-obstacle')).toHaveCount(0);
  await more.click();
  await expect(more).toHaveAttribute('aria-expanded', 'true');

  // Obstacle detection with the app's names; nothing chosen keeps the mower's own setting.
  const obstacle = picker.getByTestId('start-obstacle');
  await expect(obstacle.getByRole('radio')).toHaveText(['Aus', 'Standard', 'Empfindlich']);
  await expect(obstacle).toContainText('Mäher');
  await obstacle.getByRole('radio', { name: 'Empfindlich' }).click();
  await expect(obstacle.getByRole('radio', { name: 'Empfindlich' })).toHaveAttribute(
    'aria-checked',
    'true',
  );

  const order = picker.getByTestId('start-order');
  await expect(order.getByRole('radio')).toHaveText(['Zuerst Rand', 'Zuerst Zickzack']);
  await order.getByRole('radio', { name: 'Zuerst Zickzack' }).click();

  const laps = picker.getByTestId('start-nogo-laps');
  await expect(laps).toContainText('Rundenzahl der Sperrzone');
  await laps.getByRole('button', { name: 'Weniger Runden um Sperrzonen' }).click();
  await expect(laps.locator('output')).toHaveAttribute('data-value', '0');

  const progress = picker.getByTestId('start-progress');
  await expect(progress).toContainText('Fortschritt beginnen');
  const later = progress.getByRole('button', { name: 'Später beginnen' });
  for (let i = 0; i < 8; i += 1) await later.click();
  await expect(progress.locator('output')).toHaveAttribute('data-value', '40');
  await expect(picker.getByTestId('start-summary')).toHaveText(
    'Streifen 0° · 1 Randrunde · ab 40 %',
  );
  await expect(more).toHaveText('Mehr · 4');

  await page.getByTestId('area-start').click();
  await expect(page.getByTestId('mower-state')).toContainText('Mäht', { timeout: 30_000 });

  // The start carried exactly the chosen options and the simulator ran it.
  await expect
    .poll(async () => (await startCommands(request, owner, mowerId))[0]?.status, {
      timeout: 30_000,
    })
    .toBe('executed');
  const [sent] = await startCommands(request, owner, mowerId);
  expect(sent.pattern).toMatchObject({
    name: 'stripes',
    obstacle_detection: 'sensitive',
    obstacle_laps: 0,
    order: 'grid_first',
    start_progress_pct: 40,
  });
  // The simulated route starts at 40 %: the job reports that progress from the start.
  await expect
    .poll(async () => (await jobProgress(request, owner, mowerId)) ?? -1, { timeout: 30_000 })
    .toBeGreaterThanOrEqual(35);

  // "Als Standard merken" was on: the defaults keep the options, not the start progress.
  const mower = await request.get(`/api/v1/fleet/mowers/${mowerId}`, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  const defaults = ((await mower.json()) as { mow_defaults: { pattern: Record<string, unknown> } })
    .mow_defaults.pattern;
  expect(defaults).toMatchObject({
    obstacle_detection: 'sensitive',
    obstacle_laps: 0,
    order: 'grid_first',
    start_progress_pct: null,
  });
  csp.assertNone();
});
