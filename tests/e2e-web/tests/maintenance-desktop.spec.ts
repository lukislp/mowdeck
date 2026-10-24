/**
 * Maintenance on the mower settings page (desktop, 1280 x 800): the simulated mower's five
 * actions in page order; each asks first in a sheet that explains it, and cancelling sends
 * nothing; a map sync is executed, a restart handed to the mower; while the mower mows the
 * dock-bound actions are disabled with their reason; leaving the dock drives it out, after which
 * the actions that need the dock say so.
 */
import { expect, test, type APIRequestContext } from '@playwright/test';

import {
  RUN,
  actor,
  commandByApi,
  createMowerByApi,
  loginByUi,
  registerByApi,
  waitForMap,
  watchCsp,
  type Actor,
} from './support';

async function commandKinds(
  request: APIRequestContext,
  owner: Actor,
  mowerId: string,
): Promise<string[]> {
  const response = await request.get(`/api/v1/fleet/mowers/${mowerId}/commands?limit=50`, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  return ((await response.json()) as { items: { kind: string }[] }).items.map((c) => c.kind);
}

async function activity(request: APIRequestContext, owner: Actor, mowerId: string) {
  const response = await request.get(`/api/v1/telemetry/mowers/${mowerId}/state`, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  return response.ok()
    ? ((await response.json()) as { state: { activity: string } }).state.activity
    : null;
}

test('maintenance: confirm, run, results and the safety rules', async ({ page, request }) => {
  test.setTimeout(240_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('maint'));
  const mowerId = await createMowerByApi(request, owner, `Luba maintenance ${RUN}`);
  await waitForMap(request, owner, mowerId);

  await loginByUi(page, owner);
  await page.goto(`/mowers/${mowerId}/settings`);
  const section = page.getByTestId('maintenance-section');
  await expect(section.locator('li[data-testid^="maint-"]')).toHaveCount(5, { timeout: 60_000 });
  const order = await section
    .locator('li[data-testid^="maint-"]')
    .evaluateAll((items) => items.map((item) => item.getAttribute('data-testid')));
  expect(order).toEqual([
    'maint-leave_dock',
    'maint-sync_map',
    'maint-resync_rtk',
    'maint-relocate_dock',
    'maint-restart_mower',
  ]);
  // Docked: everything may run.
  for (const id of order)
    await expect(page.getByTestId(id!)).toHaveAttribute('data-blocked', 'none');

  // The confirmation explains; cancelling sends nothing.
  await page.getByTestId('maint-restart_mower-open').click();
  const sheet = page.getByTestId('maint-confirm');
  await expect(sheet).toContainText('Mäher neu starten?');
  await expect(page.getByTestId('maint-confirm-body')).toContainText('startet neu');
  await expect(sheet).toContainText('Voraussetzung');
  await sheet.getByRole('button', { name: 'Abbrechen' }).click();
  await expect(sheet).toBeHidden();
  expect(await commandKinds(request, owner, mowerId)).not.toContain('restart_mower');

  // A map sync: executed.
  await page.getByTestId('maint-sync_map-open').click();
  await expect(page.getByTestId('maint-confirm-body')).toContainText('Karte');
  await page.getByTestId('maint-confirm-go').click();
  await expect(page.getByTestId('maint-sync_map-result')).toHaveAttribute(
    'data-status',
    'executed',
    { timeout: 60_000 },
  );
  await expect(page.getByTestId('maint-sync_map-result')).toContainText('ausgeführt');

  // A restart: the mower does not confirm it; "accepted" is its final answer.
  await page.getByTestId('maint-restart_mower-open').click();
  await page.getByTestId('maint-confirm-go').click();
  await expect(page.getByTestId('maint-restart_mower-result')).toHaveAttribute(
    'data-status',
    'accepted',
    { timeout: 60_000 },
  );
  await expect(page.getByTestId('maint-restart_mower-result')).toContainText(
    'an den Mäher übergeben',
  );
  expect(await commandKinds(request, owner, mowerId)).toContain('restart_mower');
  // Back after the simulated restart: docked again, everything enabled.
  await expect(page.getByTestId('maint-restart_mower')).toHaveAttribute('data-blocked', 'none', {
    timeout: 60_000,
  });

  // While it mows: the dock-bound actions say why they cannot run, the RTK resync can.
  await commandByApi(request, owner, mowerId, { kind: 'start_mow', area_ids: ['front'] });
  await expect(page.getByTestId('maint-restart_mower-reason')).toHaveAttribute(
    'data-reason',
    'not_at_rest',
    { timeout: 60_000 },
  );
  await expect(page.getByTestId('maint-restart_mower-open')).toBeDisabled();
  await expect(page.getByTestId('maint-relocate_dock-reason')).toHaveAttribute(
    'data-reason',
    'not_docked',
  );
  await expect(page.getByTestId('maint-leave_dock-reason')).toContainText(
    'Nur wenn der Mäher in der Station steht.',
  );
  await expect(page.getByTestId('maint-resync_rtk')).toHaveAttribute('data-blocked', 'none');
  await expect(page.getByTestId('maint-sync_map')).toHaveAttribute('data-blocked', 'none');

  await commandByApi(request, owner, mowerId, { kind: 'return_to_dock', end_job: true });
  await expect
    .poll(() => activity(request, owner, mowerId), { timeout: 120_000 })
    .toMatch(/docked|charging/);

  // Leaving the dock drives it out; then the dock-bound actions are disabled again.
  await expect(page.getByTestId('maint-leave_dock')).toHaveAttribute('data-blocked', 'none', {
    timeout: 30_000,
  });
  await page.getByTestId('maint-leave_dock-open').click();
  await expect(page.getByTestId('maint-confirm-body')).toContainText('Station');
  await page.getByTestId('maint-confirm-go').click();
  await expect(page.getByTestId('maint-leave_dock-result')).toHaveAttribute(
    'data-status',
    'accepted',
    { timeout: 60_000 },
  );
  await expect.poll(() => activity(request, owner, mowerId), { timeout: 60_000 }).toBe('idle');
  await expect(page.getByTestId('maint-leave_dock-reason')).toHaveAttribute(
    'data-reason',
    'not_docked',
    { timeout: 30_000 },
  );
  await expect(page.getByTestId('maint-restart_mower')).toHaveAttribute('data-blocked', 'none');
  csp.assertNone();
});
