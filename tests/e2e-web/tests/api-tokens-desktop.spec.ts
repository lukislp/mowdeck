/**
 * API tokens in the settings (desktop, 1280 x 800): a full Home Assistant token is created from
 * its preset, its secret shown once, and the list shows exactly the granted rights as chips
 * (checked against the API); the custom selection offers only what a token may hold, shows the
 * implied account keys as included and refuses an empty selection; the token works for
 * `mower.drive` checks through `POST /me/permissions`.
 */
import { expect, test } from '@playwright/test';

import { actor, loginByUi, registerByApi, watchCsp } from './support';

const FULL = ['fleet.manage', 'mower.control', 'mower.drive', 'mower.view', 'schedule.edit'];

test('api tokens: full Home Assistant preset, rights in the list, custom selection', async ({
  page,
  request,
}) => {
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('tokens'));
  await loginByUi(page, owner);
  await page.goto('/settings#api-tokens');
  const section = page.getByTestId('settings-section-api-tokens');
  await expect(section).toBeVisible();

  // The safer basics are the default; full is explained and flagged.
  await expect(section.getByTestId('token-preset-basic')).toBeChecked();
  await expect(section.getByTestId('token-preset-difference')).toContainText('vollständig');
  await expect(section.getByTestId('token-warning')).toHaveCount(0);
  await section.getByTestId('token-preset-full').check();
  await expect(section.getByTestId('token-warning')).toContainText('mower.drive');
  await expect(section.getByTestId('token-warning')).toContainText('fleet.manage');

  await section.locator('#token-name').fill('Home Assistant');
  await section.getByTestId('token-create').click();
  const secret = section.getByTestId('token-secret');
  await expect(secret).toContainText('mdk_');
  const value = (await secret.locator('code').textContent())!.trim();

  // The list shows the granted rights.
  const row = section.locator('[data-testid="token-row"][data-name="Home Assistant"]');
  await expect(row).toBeVisible();
  const chips = row.getByTestId('token-chips').locator('[data-key]');
  await expect(chips).toHaveCount(FULL.length);
  expect(
    (await chips.evaluateAll((els) => els.map((e) => e.getAttribute('data-key')))).sort(),
  ).toEqual(FULL);
  await expect(row.locator('[data-key="mower.drive"]')).toHaveClass(/powerful/);
  await expect(row.getByTestId('token-included')).toContainText('inbegriffen');

  // The API agrees, and the grant is on the whole account.
  const listed = await request.get(`/api/v1/identity/accounts/${owner.accountId}/api-tokens`, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  const tokens = (await listed.json()) as {
    name: string;
    permissions: string[];
    grants: { level_type: string; level_id: string }[];
  }[];
  const created = tokens.find((t) => t.name === 'Home Assistant')!;
  expect([...created.permissions].sort()).toEqual(FULL);
  expect(created.grants.map((g) => [g.level_type, g.level_id])).toEqual([
    ['account', owner.accountId],
  ]);

  // The token itself holds can_drive on the account.
  const exchanged = await request.post('/api/v1/identity/token', {
    data: { grant_type: 'api_token', token: value },
  });
  expect(exchanged.status(), await exchanged.text()).toBe(200);
  const access = ((await exchanged.json()) as { access_token: string }).access_token;
  const checks = await request.post('/api/v1/identity/me/permissions', {
    headers: { Authorization: `Bearer ${access}` },
    data: {
      checks: ['can_drive', 'can_manage_mowers', 'can_edit_schedule'].map((relation) => ({
        relation,
        object: `account:${owner.accountId}`,
      })),
    },
  });
  expect(checks.status(), await checks.text()).toBe(200);
  expect(await checks.json()).toEqual([true, true, true]);

  // Custom: only what a token may hold, implied keys included, nothing refused.
  await section.getByRole('button', { name: /Schließen/ }).click();
  // Starting from view only: mower.view and what it implies.
  await section.getByTestId('token-preset-view').check();
  await section.getByTestId('token-preset-custom').check();
  const custom = section.getByTestId('token-custom');
  await expect(custom.getByTestId('token-perm-mower.drive')).toBeVisible();
  await expect(custom.getByTestId('token-perm-users.manage')).toHaveCount(0);
  await expect(custom.getByTestId('token-perm-billing.manage')).toHaveCount(0);
  await expect(custom.getByTestId('token-perm-mower.view')).toBeChecked();
  await expect(custom.getByTestId('token-perm-mower.control')).not.toBeChecked();
  await expect(custom.getByTestId('token-perm-telemetry.view')).toBeChecked();
  await expect(custom.getByTestId('token-perm-telemetry.view')).toBeDisabled();
  await expect(custom).toContainText('inbegriffen über mower.view');
  // A mower right needs mower.view, which then stays locked.
  await custom.getByTestId('token-perm-mower.drive').check();
  await expect(custom.getByTestId('token-perm-mower.view')).toBeDisabled();
  await expect(section.getByTestId('token-warning')).toContainText('mower.drive');
  await custom.getByTestId('token-perm-mower.drive').uncheck();
  await custom.getByTestId('token-perm-mower.view').uncheck();
  await expect(custom.getByTestId('token-perm-telemetry.view')).not.toBeChecked();
  await section.locator('#token-name').fill('Nothing');
  await expect(section.getByTestId('token-none')).toBeVisible();
  await expect(section.getByTestId('token-create')).toBeDisabled();
  csp.assertNone();
});
