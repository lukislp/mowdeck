/**
 * The mower's own settings on the settings page (desktop, 1280 x 800), generated from the
 * simulated mower's declaration (one setting of each type): grouped and translated rows with the
 * reported values; a switch, a stepper, a choice and the quiet hours changed, each shown at once
 * and then "applied by the mower" after the command result, and the mower's state follows. A
 * Viewer sees the settings read only with the reason.
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

async function mowerSettings(
  request: APIRequestContext,
  owner: Actor,
  mowerId: string,
): Promise<Record<string, unknown>> {
  const response = await request.get(`/api/v1/telemetry/mowers/${mowerId}/state`, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  if (!response.ok()) return {};
  return (
    ((await response.json()) as { state: { settings?: Record<string, unknown> } }).state.settings ??
    {}
  );
}

async function createViewer(request: APIRequestContext, owner: Actor): Promise<Actor> {
  const auth = { Authorization: `Bearer ${owner.token}` };
  const roles = (await (
    await request.get(`/api/v1/identity/accounts/${owner.accountId}/roles`, { headers: auth })
  ).json()) as { id: string; key: string | null }[];
  const viewerRole = roles.find((role) => role.key === 'viewer');
  expect(viewerRole, 'viewer role').toBeDefined();
  const viewer = actor('settings-viewer');
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

test('device settings: change a switch, a number, a choice and the quiet hours; viewer read only', async ({
  page,
  request,
  browser,
}) => {
  test.setTimeout(240_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('devset'));
  const mowerId = await createMowerByApi(request, owner, `Luba settings ${RUN}`);
  await waitForMap(request, owner, mowerId);
  await expect
    .poll(async () => Object.keys(await mowerSettings(request, owner, mowerId)).length, {
      timeout: 60_000,
    })
    .toBeGreaterThan(0);

  await loginByUi(page, owner);
  await page.goto(`/mowers/${mowerId}/settings`);
  const section = page.getByTestId('device-settings-section');
  await expect(section).toBeVisible();
  await expect(section.locator('section[data-group]')).toHaveCount(6);
  await expect(section.locator('section[data-group="weather"] h3')).toHaveText('Wetter');
  await expect(page.getByTestId('device-settings')).not.toHaveAttribute('data-blocker', /.+/);

  // A switch: rain detection is on; switched off it shows at once, then the mower confirms.
  const rain = page.getByTestId('setting-rain_detection');
  await expect(rain).toContainText('Regenerkennung');
  const rainSwitch = page.getByTestId('setting-rain_detection-switch');
  await expect(rainSwitch).toHaveAttribute('aria-checked', 'true');
  await rainSwitch.click();
  await expect(rainSwitch).toHaveAttribute('aria-checked', 'false');
  await expect(rain).toHaveAttribute('data-state', 'saved', { timeout: 30_000 });
  await expect(page.getByTestId('setting-rain_detection-note')).toHaveText('Vom Mäher übernommen');
  await expect
    .poll(async () => (await mowerSettings(request, owner, mowerId)).rain_detection)
    .toBe(false);

  // A number on its grid: recharge level 20 % up by one step.
  const recharge = page.getByTestId('setting-recharge_level');
  await recharge.getByRole('button', { name: 'Laden ab erhöhen' }).click();
  await expect(recharge).toHaveAttribute('data-state', 'saved', { timeout: 30_000 });
  await expect(recharge).toContainText('21');
  await expect
    .poll(async () => (await mowerSettings(request, owner, mowerId)).recharge_level)
    .toBe(21);

  // A choice: turning multi-point.
  const turn = page.getByTestId('setting-turn_style-choice');
  await turn.getByRole('radio', { name: 'Mehrpunkt' }).click();
  await expect(page.getByTestId('setting-turn_style')).toHaveAttribute('data-state', 'saved', {
    timeout: 30_000,
  });
  await expect(turn.getByRole('radio', { name: 'Mehrpunkt' })).toHaveAttribute(
    'aria-checked',
    'true',
  );

  // The quiet hours: on with the default window, then a new end applied.
  await page.getByTestId('setting-non_work_hours-switch').click();
  await expect(page.getByTestId('setting-non_work_hours')).toHaveAttribute('data-state', 'saved', {
    timeout: 30_000,
  });
  await expect(page.getByTestId('setting-non_work_hours-start')).toHaveValue('22:00');
  await page.getByTestId('setting-non_work_hours-end').fill('06:30');
  await page.getByTestId('setting-non_work_hours-apply').click();
  await expect
    .poll(async () => (await mowerSettings(request, owner, mowerId)).non_work_hours, {
      timeout: 30_000,
    })
    .toEqual({ start: '22:00', end: '06:30' });
  csp.assertNone();

  // A Viewer sees the values but cannot change them, and is told why.
  const viewer = await createViewer(request, owner);
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const viewerPage = await context.newPage();
  await loginByUi(viewerPage, viewer);
  await viewerPage.goto(`/mowers/${mowerId}/settings`);
  await expect(viewerPage.getByTestId('device-settings-blocker')).toHaveAttribute(
    'data-reason',
    'no_permission',
  );
  await expect(viewerPage.getByTestId('setting-rain_detection-switch')).toBeDisabled();
  await expect(viewerPage.getByTestId('setting-rain_detection-switch')).toHaveAttribute(
    'aria-checked',
    'false',
  );
  await context.close();
});
