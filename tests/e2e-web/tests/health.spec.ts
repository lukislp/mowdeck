/**
 * Phase 3 gate item 5, the health history (desktop, 1280 x 800): after the simulated mower mowed
 * for a while, the health page shows blade and mowing hours above zero, today's bar in the
 * 30-day strip and the battery trace; the owner records a blade change with a note; a Viewer
 * sees the page without the "blades replaced" action.
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

async function createViewer(request: APIRequestContext, owner: Actor): Promise<Actor> {
  const auth = { Authorization: `Bearer ${owner.token}` };
  const roles = (await (
    await request.get(`/api/v1/identity/accounts/${owner.accountId}/roles`, { headers: auth })
  ).json()) as { id: string; key: string | null }[];
  const viewerRole = roles.find((role) => role.key === 'viewer');
  expect(viewerRole, 'viewer role').toBeDefined();
  const viewer = actor('health-viewer');
  const created = await request.post(`/api/v1/identity/accounts/${owner.accountId}/users`, {
    headers: auth,
    data: {
      username: viewer.username,
      password: viewer.password,
      assignments: [{ role_id: viewerRole!.id, level: { type: 'account', id: owner.accountId } }],
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  return viewer;
}

test('health: blade and mowing hours grow after a job; viewer without blade reset', async ({
  page,
  request,
  browser,
}) => {
  test.setTimeout(300_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('health'));
  const mowerId = await createMowerByApi(request, owner, `Luba health ${RUN}`);
  await waitForMap(request, owner, mowerId);
  const auth = { Authorization: `Bearer ${owner.token}` };

  // A mowing job until the blade hours show at two decimals (36 s of blade time).
  await commandByApi(request, owner, mowerId, { kind: 'start_mow', area_ids: ['front', 'back'] });
  await expect
    .poll(
      async () => {
        const response = await request.get(`/api/v1/telemetry/mowers/${mowerId}/health`, {
          headers: auth,
        });
        return response.ok() ? ((await response.json()) as { blade_hours: number }).blade_hours : 0;
      },
      { timeout: 180_000, intervals: [2_000] },
    )
    .toBeGreaterThan(0.012);

  await loginByUi(page, owner);
  await page.goto(`/mowers/${mowerId}`);
  await page.getByTestId('mower-health').click();
  await page.waitForURL(new RegExp(`/mowers/${mowerId}/health$`));

  const bladeHours = page.getByTestId('health-blade-hours');
  await expect(bladeHours).not.toContainText('keine Daten');
  const value = async (testid: string) =>
    Number(
      ((await page.getByTestId(testid).textContent()) ?? '')
        .replace(/[^\d,]/g, '')
        .replace(',', '.'),
    );
  expect(await value('health-blade-hours')).toBeGreaterThan(0);
  expect(await value('health-mowing-hours')).toBeGreaterThan(0);
  expect(await value('health-distance')).toBeGreaterThan(0);
  expect(await value('health-blade-since')).toBeGreaterThan(0);
  await expect(page.getByTestId('health-threshold')).toHaveAttribute('data-level', 'ok');

  // Today's bar in the 30-day strips, the battery trace of the last 24 hours.
  await expect(page.getByTestId('daily-blade').locator('svg')).toHaveAttribute('data-bars', '1');
  expect(Number(await page.getByTestId('daily-blade').getAttribute('data-total'))).toBeGreaterThan(
    0,
  );
  await expect(page.getByTestId('battery-trace')).toBeVisible();

  // The owner records a blade change.
  await page.getByTestId('blade-reset').click();
  await page.getByTestId('blade-note').fill(`Ersatzmesser ${RUN}`);
  await page.getByTestId('blade-reset-save').click();
  await expect(page.getByText('Messerwechsel gespeichert')).toBeVisible();
  await expect(page.getByTestId('health-blade-changes')).toContainText(`Ersatzmesser ${RUN}`);
  expect(await value('health-blade-since')).toBeLessThan(0.01);
  csp.assertNone();

  // A Viewer sees the health page but no blade reset.
  const viewer = await createViewer(request, owner);
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const viewerPage = await context.newPage();
  await loginByUi(viewerPage, viewer);
  await viewerPage.goto(`/mowers/${mowerId}/health`);
  await expect(viewerPage.getByTestId('health-blade-hours')).not.toContainText('keine Daten');
  await expect(viewerPage.getByTestId('health-blade-changes')).toContainText(`Ersatzmesser ${RUN}`);
  await expect(viewerPage.getByTestId('blade-reset')).toHaveCount(0);
  await expect(viewerPage.getByRole('button', { name: 'Not-Stopp' })).toBeVisible();
  // And no drive entry on the detail page (a Viewer has no can_drive).
  await viewerPage.goto(`/mowers/${mowerId}`);
  await expect(viewerPage.getByTestId('mower-health')).toBeVisible();
  await expect(viewerPage.getByTestId('mower-drive')).toHaveCount(0);
  await context.close();

  await commandByApi(request, owner, mowerId, { kind: 'return_to_dock' });
});
