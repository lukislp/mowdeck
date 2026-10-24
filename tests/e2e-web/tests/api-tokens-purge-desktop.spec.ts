/**
 * Deleting revoked API tokens (desktop, 1280 x 800): one revoked token is removed from its row,
 * the rest with "Widerrufene entfernen" after a confirmation sheet that names the count. Each
 * step is checked against the API: the deleted tokens are gone for good, the active token is
 * untouched and still exchanges, an active token cannot be purged directly (409), and the audit
 * log keeps the tokens' history next to one `api_token.purged` row per token.
 */
import { expect, test, type APIRequestContext } from '@playwright/test';

import { actor, loginByUi, registerByApi, watchCsp, type Actor } from './support';

interface Created {
  id: string;
  token: string;
}

async function tokenByApi(
  request: APIRequestContext,
  owner: Actor,
  name: string,
  revoke = false,
): Promise<Created> {
  const base = `/api/v1/identity/accounts/${owner.accountId}/api-tokens`;
  const headers = { Authorization: `Bearer ${owner.token}` };
  const created = await request.post(base, {
    headers,
    data: {
      name,
      grants: [{ level: { type: 'account', id: owner.accountId }, permissions: ['mower.view'] }],
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  const body = (await created.json()) as Created;
  if (revoke) {
    const revoked = await request.delete(`${base}/${body.id}`, { headers });
    expect(revoked.ok(), await revoked.text()).toBe(true);
  }
  return body;
}

async function namesByApi(request: APIRequestContext, owner: Actor): Promise<string[]> {
  const listed = await request.get(`/api/v1/identity/accounts/${owner.accountId}/api-tokens`, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  expect(listed.status(), await listed.text()).toBe(200);
  return ((await listed.json()) as { name: string }[]).map((t) => t.name).sort();
}

test('api tokens: revoked ones are removed one by one and all at once', async ({
  page,
  request,
}) => {
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('tokens-purge'));
  const headers = { Authorization: `Bearer ${owner.token}` };
  const base = `/api/v1/identity/accounts/${owner.accountId}/api-tokens`;
  const tablet = await tokenByApi(request, owner, 'Altes Tablet', true);
  await tokenByApi(request, owner, 'Testskript', true);
  await tokenByApi(request, owner, 'Altes Handy', true);
  const active = await tokenByApi(request, owner, 'Home Assistant');

  // An active token is never purged directly: revoke first.
  const refused = await request.delete(`${base}/${active.id}?purge=true`, { headers });
  expect(refused.status()).toBe(409);

  await loginByUi(page, owner);
  await page.goto('/settings#api-tokens');
  const section = page.getByTestId('settings-section-api-tokens');
  const folded = section.getByTestId('token-revoked');
  await expect(folded.locator('summary')).toContainText('Widerrufene Tokens (3)');
  await folded.locator('summary').click();

  // One from its row.
  await folded.getByRole('button', { name: '„Altes Tablet“ endgültig entfernen' }).click();
  await expect(folded.locator('summary')).toContainText('Widerrufene Tokens (2)');
  await expect(section.getByTestId('token-purge-status')).toHaveText('„Altes Tablet“ entfernt.');
  await expect
    .poll(() => namesByApi(request, owner))
    .toEqual(['Altes Handy', 'Home Assistant', 'Testskript']);

  // The rest at once, after the confirmation names the count.
  await folded.getByTestId('token-purge-all').click();
  const sheet = page.getByTestId('token-purge-confirm');
  await expect(sheet).toHaveAttribute('aria-label', '2 widerrufene Tokens entfernen?');
  await expect(sheet.getByTestId('token-purge-names')).toContainText('Testskript');
  await expect(sheet.getByTestId('token-purge-names')).toContainText('Altes Handy');
  await sheet.getByRole('button', { name: 'Abbrechen' }).click();
  await expect(sheet).toHaveCount(0);
  expect(await namesByApi(request, owner)).toHaveLength(3);

  await folded.getByTestId('token-purge-all').click();
  await page.getByTestId('token-purge-go').click();
  await expect(section.getByTestId('token-revoked')).toHaveCount(0);
  await expect(section.getByTestId('token-purge-status')).toHaveText(
    '2 widerrufene Tokens entfernt.',
  );
  await expect.poll(() => namesByApi(request, owner)).toEqual(['Home Assistant']);

  // After a reload: only the active token, nothing revoked.
  await page.reload();
  await expect(
    section.locator('[data-testid="token-row"][data-name="Home Assistant"]'),
  ).toBeVisible();
  await expect(section.getByTestId('token-revoked')).toHaveCount(0);

  // The active token still works.
  const exchanged = await request.post('/api/v1/identity/token', {
    data: { grant_type: 'api_token', token: active.token },
  });
  expect(exchanged.status(), await exchanged.text()).toBe(200);
  const gone = await request.post('/api/v1/identity/token', {
    data: { grant_type: 'api_token', token: tablet.token },
  });
  expect(gone.status()).toBe(401);

  // The audit log keeps the history and has one purge row per token, without the secret.
  const audit = await request.get(`/api/v1/identity/accounts/${owner.accountId}/audit`, {
    headers,
    params: { target_id: tablet.id },
  });
  const entries = (await audit.json()) as { action: string; details: Record<string, unknown> }[];
  expect(entries.map((e) => e.action)).toEqual(
    expect.arrayContaining(['api_token.created', 'api_token.revoked', 'api_token.purged']),
  );
  expect(JSON.stringify(entries)).not.toContain(tablet.token);
  const purged = await request.get(`/api/v1/identity/accounts/${owner.accountId}/audit`, {
    headers,
    params: { action: 'api_token.purged' },
  });
  expect(((await purged.json()) as unknown[]).length).toBe(3);
  csp.assertNone();
});
