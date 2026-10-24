/**
 * API tokens on a phone (390 x 844, touch): with several revoked tokens on the account, creating
 * a token from the bottom of the form brings its one-time secret into view without scrolling
 * (clear of the sticky section strip) and focuses it; copy puts the secret on the clipboard; the
 * revoked tokens sit folded in a closed "Widerrufene Tokens (N)" disclosure under the active ones.
 */
import { expect, test, type APIRequestContext } from '@playwright/test';

import { actor, loginByUi, registerByApi, watchCsp, type Actor } from './support';

test.use({ permissions: ['clipboard-read', 'clipboard-write'] });

async function tokenByApi(
  request: APIRequestContext,
  owner: Actor,
  name: string,
  revoke = false,
): Promise<void> {
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
  if (!revoke) return;
  const { id } = (await created.json()) as { id: string };
  const revoked = await request.delete(`${base}/${id}`, { headers });
  expect(revoked.ok(), await revoked.text()).toBe(true);
}

test('api tokens on a phone: the new secret is in view at once, revoked ones folded', async ({
  page,
  request,
}) => {
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('tokens-phone'));
  await tokenByApi(request, owner, 'Wohnzimmer-Tablet');
  for (const name of ['Alt 1', 'Alt 2', 'Alt 3', 'Alt 4']) {
    await tokenByApi(request, owner, name, true);
  }

  await loginByUi(page, owner);
  await page.goto('/settings#api-tokens');
  const section = page.getByTestId('settings-section-api-tokens');
  await expect(section.getByTestId('token-row')).toHaveCount(1);

  // Revoked tokens: folded, closed, counted.
  const folded = section.getByTestId('token-revoked');
  await expect(folded.locator('summary')).toHaveText(/Widerrufene Tokens \(4\)/);
  await expect(folded).not.toHaveAttribute('open', /.*/);
  await expect(folded.getByTestId('token-revoked-row').first()).toBeHidden();

  // Create from the bottom of the form: the list is out of sight by then.
  await section.locator('#token-name').fill('Home Assistant');
  const create = section.getByTestId('token-create');
  await create.scrollIntoViewIfNeeded();
  await expect(section.getByTestId('token-list')).not.toBeInViewport();
  await create.click();

  // The secret is in view without any scrolling by the test, and has the focus.
  const secret = section.getByTestId('token-secret');
  await expect(secret).toBeFocused();
  const value = secret.getByTestId('token-secret-value');
  await expect(value).toContainText('mdk_');
  await expect(value).toBeInViewport({ ratio: 1 });
  await expect(secret.getByTestId('token-secret-copy')).toBeInViewport({ ratio: 1 });
  await expect(secret.locator('#token-secret-once')).toBeInViewport({ ratio: 1 });
  // Not hidden under the sticky top bar or section strip.
  await expect
    .poll(async () => {
      const box = await secret.boundingBox();
      const strip = await page.getByTestId('settings-index').boundingBox();
      return box && strip ? box.y - (strip.y + strip.height) : -1;
    })
    .toBeGreaterThanOrEqual(0);

  // Copy puts exactly the secret on the clipboard.
  const shown = (await value.textContent())!.trim();
  await secret.getByTestId('token-secret-copy').click();
  await expect(secret.getByTestId('token-secret-copy')).toContainText('Kopiert');
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(shown);

  // The new token heads the active list; the revoked ones open on demand.
  await expect(section.getByTestId('token-row').first()).toHaveAttribute(
    'data-name',
    'Home Assistant',
  );
  await folded.locator('summary').click();
  await expect(folded.getByTestId('token-revoked-row')).toHaveCount(4);
  await expect(folded.getByRole('button')).toHaveCount(0);

  // Stays until dismissed; the focus returns to the form.
  await secret.getByTestId('token-secret-dismiss').click();
  await expect(secret).toHaveCount(0);
  await expect(section.locator('#token-name')).toBeFocused();
  csp.assertNone();
});
