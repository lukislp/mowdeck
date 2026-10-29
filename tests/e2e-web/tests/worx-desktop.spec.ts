/**
 * A mower without map areas (ADR 0066 amendment), on the untested Worx provider enabled in
 * stack.env with the fake clouds (`GATEWAY_FAKE_CLOUDS`): any `*@fake.mowdeck.test` address logs
 * in with `fake-cloud-password` at any brand. The test links a fake account in Settings (the
 * provider's own warnings about the unofficial API are shown first, the brand is a choice), adds
 * the online fake mower `Wanda` of the account, and starts it through its start plans: the
 * Start button opens the plan choice instead of the area sheet, and a zone start mows. The fake
 * mower has a fixed serial number, so the test always removes its mower and account.
 */
import { expect, test, type APIRequestContext } from '@playwright/test';

import {
  RUN,
  actor,
  loginByUi,
  pickProvider,
  registerByApi,
  watchCsp,
  type Actor,
} from './support';

const FLEET = '/api/v1/fleet';
const TELEMETRY = '/api/v1/telemetry';
const FAKE_PASSWORD = 'fake-cloud-password';

async function cleanUp(request: APIRequestContext, owner: Actor): Promise<void> {
  const headers = { Authorization: `Bearer ${owner.token}` };
  const mowers = await request.get(`${FLEET}/mowers`, { headers });
  if (mowers.ok()) {
    for (const mower of ((await mowers.json()) as { items: { id: string }[] }).items) {
      await request.delete(`${FLEET}/mowers/${mower.id}`, { headers });
    }
  }
  const accounts = await request.get(`${FLEET}/linked-accounts`, { headers });
  if (accounts.ok()) {
    for (const account of ((await accounts.json()) as { items: { id: string }[] }).items) {
      await request.delete(`${FLEET}/linked-accounts/${account.id}`, { headers });
    }
  }
}

test('worx: link with warnings, start a mower without map areas by its plans', async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('worx'));
  const headers = { Authorization: `Bearer ${owner.token}` };
  const email = `worx-${RUN.replace(/[^A-Za-z0-9-]/g, '').toLowerCase()}@fake.mowdeck.test`;

  const listed = await request.get(`${FLEET}/linked-accounts/providers`, {
    headers,
    params: { locale: 'de' },
  });
  expect(listed.status(), await listed.text()).toBe(200);
  const worx = (
    (await listed.json()) as {
      items: { provider: string; warnings: string[]; tested_on_hardware: boolean }[];
    }
  ).items.find((p) => p.provider === 'worx');
  expect(worx, 'the stack enables the Worx provider (stack.env)').toBeTruthy();
  expect(worx!.tested_on_hardware).toBe(false);
  expect(worx!.warnings).toHaveLength(2);
  expect(worx!.warnings[0]).toContain('Inoffizielle API');

  try {
    await loginByUi(page, owner);
    await page.goto('/settings#linked-accounts');
    await pickProvider(page, 'worx');
    await expect(page.getByTestId('provider-warning')).toHaveCount(2);
    await expect(page.getByTestId('provider-warning').first()).toContainText('Inoffizielle API');
    await expect(page.getByTestId('provider-untested')).toBeVisible();
    await expect(page.getByTestId('provider-dev-account')).toHaveCount(0);
    await page.locator('#linked-brand').selectOption('kress');
    await page.locator('#linked-email').fill(email);
    await page.locator('#linked-password').fill(FAKE_PASSWORD);
    await page.getByTestId('linked-connect-submit').click();
    const card = page.locator(`[data-testid="linked-account"][data-provider="worx"]`);
    await expect(card).toBeVisible({ timeout: 60_000 });
    await expect(card.getByTestId('linked-account-devices')).toHaveText('2');
    await expect(page.locator('body')).not.toContainText(FAKE_PASSWORD);

    const accounts = await request.get(`${FLEET}/linked-accounts`, { headers });
    const [account] = ((await accounts.json()) as { items: { id: string; provider: string }[] })
      .items;
    expect(account?.provider).toBe('worx');
    const added = await request.post(`${FLEET}/linked-accounts/${account!.id}/mowers`, {
      headers,
      data: { mowers: [{ device_name: 'Wanda', name: `Wanda ${RUN}` }] },
    });
    expect(added.status(), await added.text()).toBe(201);
    const mowers = await request.get(`${FLEET}/mowers`, { headers });
    const mower = ((await mowers.json()) as { items: { id: string; name: string }[] }).items.find(
      (m) => m.name === `Wanda ${RUN}`,
    );
    expect(mower).toBeTruthy();

    // The gateway connects it through the fake cloud; its capabilities carry the start plans.
    await expect
      .poll(
        async () => {
          const caps = await request.get(`${TELEMETRY}/mowers/${mower!.id}/capabilities`, {
            headers,
          });
          if (!caps.ok()) return [];
          const body = (await caps.json()) as {
            capabilities?: { start_plans?: { ref: string }[] };
          };
          return (body.capabilities?.start_plans ?? []).map((p) => p.ref);
        },
        { timeout: 90_000 },
      )
      .toEqual(['now', 'zone:1', 'zone:2', 'edge']);

    await page.goto(`/mowers/${mower!.id}`);
    await expect(page.getByTestId('map-empty')).toContainText('keine Karte');
    const start = page.locator('[data-command="start"]').first();
    await expect(start).toBeEnabled({ timeout: 60_000 });
    await start.click();
    await expect(page.getByTestId('start-plan-choice')).toBeVisible();
    await expect(page.getByTestId('start-plan-now')).toBeVisible();
    await expect(page.getByTestId('start-plan-edge')).toBeVisible();
    const sent = page.waitForResponse(
      (r) => /\/mowers\/[^/]+\/commands$/.test(r.url()) && r.request().method() === 'POST',
    );
    await page.getByTestId('start-plan-zone-2').click();
    const command = (await (await sent).request().postDataJSON()) as {
      kind: string;
      plan_ref?: string;
    };
    expect(command).toMatchObject({ kind: 'start_mow', plan_ref: 'zone:2' });
    await expect(page.locator('[data-command="pause"]').first()).toBeEnabled({ timeout: 60_000 });
    csp.assertNone();
  } finally {
    await cleanUp(request, owner);
  }
});
