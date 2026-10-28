/**
 * API access (ADR 0064), desktop: a user creates a personal access token in the settings after a
 * step-up, sees the secret once with a quick start for this installation, scripts against the
 * API with it (list mowers, read the state, pause a mower) and revokes it, after which the
 * exchange fails. The token itself can never list or create tokens.
 */
import { expect, test } from '@playwright/test';

import {
  actor,
  ageConfirmations,
  createMowerByApi,
  loginByUi,
  registerByApi,
  watchCsp,
} from './support';

test('api access: create a personal token, script with it, revoke it', async ({
  page,
  request,
}) => {
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('pat'));
  const mowerId = await createMowerByApi(request, owner, 'Skript Mäher');
  await loginByUi(page, owner);
  // The last confirmation is older than the window: creating asks for the password again.
  ageConfirmations(owner.username);
  await page.reload();
  await page.goto('/settings#api-access');
  const section = page.getByTestId('settings-section-api-access');
  await expect(section).toBeVisible();
  await expect(section.getByTestId('pat-empty')).toBeVisible();
  await expect(section.getByTestId('pat-base-url')).toHaveText(/\/api\/v1$/);
  await expect(section.getByTestId('pat-guide')).toHaveAttribute('href', /guides\/scripting\.md$/);
  await expect(section.getByTestId('pat-reference')).toHaveAttribute(
    'href',
    /reference\/api\/index\.md$/,
  );

  // Read is on by default; control turns it on and locks it.
  await expect(section.getByTestId('pat-scope-mower.view')).toBeChecked();
  await section.getByTestId('pat-scope-mower.control').check();
  await expect(section.getByTestId('pat-scope-mower.view')).toBeDisabled();
  await section.getByTestId('pat-lifetime-30').check();
  await section.locator('#pat-name').fill('Regen-Skript');
  await section.getByTestId('pat-create').click();

  const sheet = page.getByTestId('stepup-sheet');
  await expect(sheet).toBeVisible();
  await sheet.locator('#stepup-password').fill(owner.password);
  await sheet.getByTestId('stepup-confirm').click();

  const secret = section.getByTestId('pat-secret');
  await expect(secret).toBeVisible();
  const value = (await section.getByTestId('pat-secret-value').textContent())!.trim();
  expect(value).toMatch(/^mdk_/);
  await expect(section.getByTestId('pat-quickstart')).toContainText('/identity/token');
  await expect(section.getByTestId('pat-quickstart')).not.toContainText(value);

  const row = section.locator('[data-testid="pat-row"][data-name="Regen-Skript"]');
  await expect(row).toBeVisible();
  await expect(row.locator('[data-key]')).toHaveCount(2);
  await expect(row.getByTestId('pat-expiry')).toContainText('Tagen');

  // Scripting: exchange, list mowers, read the state, pause the mower.
  const exchanged = await request.post('/api/v1/identity/token', {
    data: { grant_type: 'api_token', token: value },
  });
  expect(exchanged.status(), await exchanged.text()).toBe(200);
  const access = ((await exchanged.json()) as { access_token: string }).access_token;
  const auth = { Authorization: `Bearer ${access}` };
  const mowers = await request.get('/api/v1/fleet/mowers', { headers: auth });
  expect(mowers.status()).toBe(200);
  const items = ((await mowers.json()) as { items: { id: string }[] }).items;
  expect(items.map((mower) => mower.id)).toContain(mowerId);
  const state = await request.get(`/api/v1/telemetry/mowers/${mowerId}/state`, { headers: auth });
  expect([200, 404]).toContain(state.status()); // 404 until the simulator reported once
  const pause = await request.post(`/api/v1/fleet/mowers/${mowerId}/commands`, {
    headers: auth,
    data: { command: { kind: 'pause_mow' } },
  });
  expect(pause.status(), await pause.text()).toBe(202);

  // A token never manages tokens.
  const ownList = await request.get('/api/v1/identity/me/api-tokens', { headers: auth });
  expect(ownList.status()).toBe(403);
  const another = await request.post('/api/v1/identity/me/api-tokens', {
    headers: auth,
    data: { name: 'evil', scopes: ['mower.view'], expires_in_days: 1 },
  });
  expect(another.status()).toBe(403);

  // Revoke in the UI: gone from the active list, the exchange fails.
  await section.getByTestId('pat-secret-dismiss').click();
  await expect(secret).toHaveCount(0);
  await row.getByTestId('pat-revoke').click();
  await expect(section.getByTestId('pat-no-active')).toBeVisible();
  await expect(section.getByTestId('pat-inactive')).toContainText('1');
  const after = await request.post('/api/v1/identity/token', {
    data: { grant_type: 'api_token', token: value },
  });
  expect(after.status()).toBe(401);
  csp.assertNone();
});
