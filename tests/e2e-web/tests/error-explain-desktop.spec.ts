/**
 * Error codes in plain words (ADR 0067, desktop 1280 x 800). A simulated mower that gets stuck
 * after 20 s of mowing (`stuck_after_seconds`) reports the code `stuck`; the mower page explains
 * it in German from the simulator's error lexicon (registered by mowdeck-control through the
 * entry point group `mowdeck.error_lexicons`, served by the Device-Gateway and Telemetry):
 * severity, category, vendor and code, title, cause and the numbered steps. The same error
 * through the API in English, and an unknown vendor code gets the generic answer. The mower's
 * settings list the simulator's error codes with the current error pinned and counted.
 */
import { expect, test } from '@playwright/test';

import {
  RUN,
  actor,
  commandByApi,
  loginByUi,
  registerByApi,
  waitForMap,
  watchCsp,
} from './support';

test('error lexicon: a stuck simulated mower is explained in German on its page', async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('errx'));
  const auth = { Authorization: `Bearer ${owner.token}` };

  const created = await request.post('/api/v1/fleet/mowers', {
    headers: auth,
    data: {
      name: `Luba errx ${RUN}`,
      connection_kind: 'simulated',
      connection_options: { stuck_after_seconds: 20, time_scale: 1 },
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  const mowerId = ((await created.json()) as { id: string }).id;
  await waitForMap(request, owner, mowerId);
  await commandByApi(request, owner, mowerId, { kind: 'start_mow', area_ids: ['front'] });

  await loginByUi(page, owner);
  await page.goto(`/mowers/${mowerId}`);
  await expect(page.getByTestId('mower-state')).toContainText('Fehler', { timeout: 90_000 });

  const card = page.getByTestId('mower-error').getByTestId('error-explainer');
  await expect(card).toBeVisible({ timeout: 30_000 });
  await expect(card).toHaveAttribute('data-known', 'true');
  await expect(card).toHaveAttribute('data-category', 'trapped');
  await expect(card).toHaveAttribute('lang', 'de');
  await expect(card.getByTestId('error-title')).toHaveText('Steckt an einem Hindernis fest');
  await expect(card.getByTestId('error-severity')).toContainText('Warnung');
  await expect(card.getByTestId('error-severity')).toContainText('Festgefahren');
  await expect(card.getByTestId('error-code')).toContainText('Simulator');
  await expect(card.getByTestId('error-code')).toContainText('stuck');
  await expect(card.getByText('Wahrscheinliche Ursache')).toBeVisible();
  await expect(card.getByTestId('error-steps').getByRole('listitem')).toHaveCount(2);
  await expect(card.getByTestId('error-steps')).toContainText('Joystick');

  // The mower's settings list its vendor's error codes: the current error pinned and marked,
  // the code it reported counted, a search narrows the list and a row opens to the steps.
  await page.goto(`/mowers/${mowerId}/settings`);
  const lexicon = page.getByTestId('error-codes-section').getByTestId('error-lexicon');
  await expect(lexicon.getByTestId('lexicon-summary')).toContainText('2 Codes', {
    timeout: 30_000,
  });
  await expect(lexicon.getByTestId('lexicon-current')).toContainText(
    'Steckt an einem Hindernis fest',
  );
  const stuckRow = lexicon.locator('[data-testid="lexicon-entry"][data-code="stuck"]');
  await expect(stuckRow).toHaveAttribute('data-current', 'true');
  await expect(stuckRow.getByTestId('lexicon-seen')).toContainText('bei diesem Mäher', {
    timeout: 30_000,
  });
  await lexicon.getByTestId('lexicon-search').fill('angehoben');
  await expect(lexicon.getByTestId('lexicon-entry')).toHaveCount(1);
  const liftedRow = lexicon.locator('[data-testid="lexicon-entry"][data-code="lifted"]');
  await liftedRow.locator('summary').click();
  await expect(liftedRow.getByTestId('error-steps')).toBeVisible();
  await lexicon.getByTestId('lexicon-reset').click();
  await expect(lexicon.getByTestId('lexicon-entry')).toHaveCount(2);
  const browsed = await request.get(`/api/v1/telemetry/mowers/${mowerId}/error-lexicon`, {
    headers: auth,
    params: { severity: 'critical', locale: 'en' },
  });
  expect(browsed.status(), await browsed.text()).toBe(200);
  const browsedBody = (await browsed.json()) as { entries: { code: string }[] };
  expect(browsedBody.entries.map((e) => e.code)).toEqual(['lifted']);

  // The API: the same error in English, and a code the vendor's lexicon does not know.
  const english = await request.get(`/api/v1/telemetry/mowers/${mowerId}/error`, {
    headers: auth,
    params: { locale: 'en' },
  });
  expect(english.status(), await english.text()).toBe(200);
  const body = (await english.json()) as {
    error: { title: string; known: boolean; vendor: string; steps: string[] } | null;
  };
  expect(body.error?.title).toBe('Stuck on an obstacle');
  expect(body.error?.vendor).toBe('simulated');
  expect(body.error?.steps.length).toBe(2);
  const unknown = await request.get('/api/v1/telemetry/error-codes/simulated/E42', {
    headers: auth,
    params: { locale: 'de' },
  });
  expect(((await unknown.json()) as { title: string; known: boolean }).known).toBe(false);
  const lexicons = await request.get('/api/v1/telemetry/error-lexicons', { headers: auth });
  const vendors = ((await lexicons.json()) as { lexicons: { vendor: string }[] }).lexicons.map(
    (l) => l.vendor,
  );
  expect(vendors).toEqual(expect.arrayContaining(['mammotion', 'simulated']));
  csp.assertNone();
});
