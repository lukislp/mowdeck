/**
 * Error codes in plain words (ADR 0065, desktop 1280 x 800). A simulated mower that gets stuck
 * after 20 s of mowing (`stuck_after_seconds`) reports the code `stuck`; the mower page explains
 * it in German from the simulator's error lexicon (registered by mowdeck-control through the
 * entry point group `mowdeck.error_lexicons`, served by the Device-Gateway and Telemetry):
 * severity, category, vendor and code, title, cause and the numbered steps. The same error
 * through the API in English, and an unknown vendor code gets the generic answer.
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
