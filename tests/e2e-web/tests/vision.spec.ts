/**
 * Task 27, AI assist ("KI-Hilfe", desktop 1280 x 800) against the stack's rule based fake vision
 * provider (the stack runs with `VISION_ALLOW_FAKE_PROVIDER=true`, tests/e2e-web/stack.env):
 * (a) the owner opens the AI provider settings ("KI-Anbieter"), picks the fake family, saves the
 * model `fake/recover` and a test call answers ok; (b) a simulated mower that gets stuck after
 * 20 s of mowing (`stuck_after_seconds`, a development option of the simulator that Fleet passes
 * through) shows the error state, the AI assist sheet proposes the fake maneuver (back, turn,
 * forward), the owner approves it (blades off), the outcome is "recovered" with the mower back in
 * idle or mowing, and Fleet lists the drive session with `origin: "vision"`. Stereo: the settings
 * page has "Stereo-Bilder, wenn verfügbar" switched on by default; with both front cameras of the
 * simulator side by side in the camera panel the request carries both frames, and the proposal
 * shows "Stereo" with the two images (left and right) and `stereo: true` in the API.
 */
import { expect, test, type APIRequestContext } from '@playwright/test';

import {
  RUN,
  actor,
  commandByApi,
  loginByUi,
  registerByApi,
  waitForMap,
  watchCsp,
  type Actor,
} from './support';

async function createStuckMower(
  request: APIRequestContext,
  owner: Actor,
  name: string,
): Promise<string> {
  const response = await request.post('/api/v1/fleet/mowers', {
    headers: { Authorization: `Bearer ${owner.token}` },
    data: {
      name,
      connection_kind: 'simulated',
      connection_options: { stuck_after_seconds: 20, time_scale: 1 },
    },
  });
  expect(response.status(), await response.text()).toBe(201);
  return ((await response.json()) as { id: string }).id;
}

test('vision: fake provider set up, a stuck mower recovered by an approved AI maneuver', async ({
  page,
  request,
}) => {
  test.setTimeout(300_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('vision'));
  const auth = { Authorization: `Bearer ${owner.token}` };

  // (a) AI provider settings: the fake family, model fake/recover, save, test ok.
  await loginByUi(page, owner);
  // The AI provider is a section of the one settings page; the old route forwards to it.
  await page.goto('/settings/ai-provider');
  await page.waitForURL(/\/settings#ai-provider$/);
  await expect(page.getByTestId('settings-section-ai-provider')).toHaveCount(1);
  await expect(page.getByRole('heading', { name: 'KI-Anbieter' }).first()).toBeVisible();
  await page.getByRole('radio', { name: /Fake/ }).check();
  const model = page.getByLabel('Modell', { exact: true });
  await expect(model).toHaveValue(/^fake\//);
  await model.fill('fake/recover');
  await page.getByTestId('ai-save').click();
  await expect(page.getByText('KI-Anbieter gespeichert')).toBeVisible();
  await expect(page.getByTestId('ai-provider-effective')).toHaveAttribute('data-effective', 'own');
  await page.getByRole('button', { name: 'Testen' }).click();
  await expect(page.getByTestId('ai-test-verdict')).toHaveText('Das Modell hat geantwortet.');
  await expect(page.getByTestId('ai-test-result')).toContainText('fake/recover');
  // The stereo preference: on by default.
  const stereoSwitch = page.getByTestId('ai-preferences').getByRole('switch');
  await expect(stereoSwitch).toHaveAttribute('aria-checked', 'true');

  // (b) A simulated mower that gets stuck after 20 s of mowing.
  const mowerId = await createStuckMower(request, owner, `Luba vision ${RUN}`);
  await waitForMap(request, owner, mowerId);
  await expect
    .poll(
      async () =>
        (
          await request.get(`/api/v1/telemetry/mowers/${mowerId}/capabilities`, { headers: auth })
        ).status(),
      { timeout: 60_000 },
    )
    .toBe(200);
  await commandByApi(request, owner, mowerId, { kind: 'start_mow', area_ids: ['front'] });

  await page.goto(`/mowers/${mowerId}`);
  await expect(page.getByTestId('mower-state')).toContainText('Fehler', { timeout: 60_000 });
  // Both front cameras side by side: AI assist sends both frames of the same moment.
  await page.getByTestId('camera-picker').getByText('Beide').click();
  await expect(page.getByTestId('camera-tile-right')).toHaveAttribute('data-status', 'live');

  // Open AI assist: it names the provider, analyses and proposes the fake maneuver.
  await page.getByRole('button', { name: 'Mit KI-Hilfe befreien' }).click();
  const sheet = page.getByRole('dialog', { name: 'KI-Hilfe' });
  await expect(sheet).toBeVisible();
  await expect(sheet.getByTestId('assist-provider')).toContainText('fake/recover');
  await expect(
    sheet.getByText('Die Messer bleiben bei jedem Manöver der KI-Hilfe aus.'),
  ).toBeVisible();
  await expect(sheet.getByTestId('assist-image-source')).toContainText('Beide Frontkamera-Bilder');
  await sheet.getByTestId('assist-start').click();
  await expect(sheet.getByTestId('assist-state')).toHaveAttribute('data-value', 'proposed', {
    timeout: 30_000,
  });
  await expect(sheet.getByTestId('assist-stereo')).toContainText('Stereo');
  await expect(sheet.getByTestId('assist-images').getByRole('img')).toHaveCount(2);
  await expect(sheet.getByTestId('assist-images')).toContainText('Front links');
  await expect(sheet.getByTestId('assist-images')).toContainText('Front rechts');
  const listed = await request.get(`/api/v1/vision/mowers/${mowerId}/proposals?limit=1`, {
    headers: auth,
  });
  const newest = (
    (await listed.json()) as {
      items: { stereo: boolean; image_count: number; frames: { side: string | null }[] }[];
    }
  ).items[0];
  expect(newest?.stereo).toBe(true);
  expect(newest?.image_count).toBe(2);
  expect(newest?.frames.map((f) => f.side)).toEqual(['left', 'right']);
  const steps = sheet.getByTestId('assist-steps').getByRole('listitem');
  await expect(steps).toHaveCount(3);
  await expect(steps.nth(0)).toContainText('Zurück');
  await expect(steps.nth(1)).toContainText(/drehen/);
  await expect(steps.nth(2)).toContainText('Vorwärts');
  await expect(sheet.getByTestId('assist-confidence')).toContainText('%');
  await expect(sheet.getByTestId('assist-expiry')).toBeVisible();
  // The emergency stop stays visible while the sheet is open.
  await expect(page.getByRole('button', { name: 'Not-Stopp' })).toBeVisible();

  // Approve: the mower drives the steps with the blades off and is free again.
  await sheet.getByRole('button', { name: 'Zustimmen und fahren (Messer aus)' }).click();
  await expect(sheet.getByTestId('assist-state')).toHaveAttribute('data-value', 'recovered', {
    timeout: 30_000,
  });
  await expect(sheet.getByTestId('assist-outcome')).toContainText('Der Mäher ist wieder frei.');
  await expect(page.getByTestId('mower-state')).toContainText(/Bereit|Mäht/, { timeout: 30_000 });

  // Fleet records the maneuver as a drive session on behalf of the Vision service.
  await expect
    .poll(
      async () => {
        const response = await request.get(`/api/v1/fleet/mowers/${mowerId}/drive-sessions`, {
          headers: auth,
        });
        expect(response.status()).toBe(200);
        const body = (await response.json()) as { items: { origin?: string | null }[] };
        return body.items.some((item) => item.origin === 'vision');
      },
      { timeout: 15_000 },
    )
    .toBe(true);

  await sheet.getByTestId('assist-done').click();
  await expect(sheet).toBeHidden();
  csp.assertNone();
});
