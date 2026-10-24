/**
 * The typed state on the mower page (desktop, 1280 x 800): the reception and health panel shows
 * the simulated mower's RTK fix, satellites, correction age, Wi-Fi signal, uplink, self-check and
 * rain sensor; a job started over the API brings the job card with the area's name, a progress
 * that grows live over the socket, the elapsed and remaining time and the mowed area; pausing
 * marks the card, docking removes it.
 */
import { expect, test } from '@playwright/test';

import {
  RUN,
  actor,
  commandByApi,
  createMowerByApi,
  loginByUi,
  registerByApi,
  waitForMap,
  watchCsp,
} from './support';

test('mower page: reception panel and a live job card', async ({ page, request }) => {
  test.setTimeout(240_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('state'));
  const mowerId = await createMowerByApi(request, owner, `Luba state ${RUN}`);
  await waitForMap(request, owner, mowerId);

  await loginByUi(page, owner);
  await page.goto(`/mowers/${mowerId}`);
  await expect(page.getByTestId('mower-state')).toContainText(/Angedockt|Lädt/);

  // Reception and health of the docked mower (the simulator reports Wi-Fi only).
  const reception = page.getByTestId('reception');
  await expect(reception.getByTestId('rx-solution')).toHaveText('RTK Fix');
  await expect(reception.getByTestId('rx-solution')).toHaveAttribute('data-tone', 'mowing');
  await expect(reception.getByTestId('rx-satellites')).toContainText('29');
  await expect(reception.getByTestId('rx-correction')).toContainText('1,0 s');
  await expect(reception.getByTestId('rx-wifi')).toContainText('-58');
  await expect(reception.getByTestId('rx-wifi').locator('.glyph')).toHaveAttribute(
    'data-bars',
    '3',
  );
  await expect(reception.getByTestId('rx-mobile')).toContainText('—');
  await expect(reception.getByTestId('rx-network')).toContainText('WLAN');
  await expect(reception.getByTestId('rx-self-check')).toHaveAttribute('data-result', 'ok');
  await expect(reception.getByTestId('rx-self-check')).toContainText('Startbereit');
  await expect(reception.getByTestId('rx-rain')).toContainText('Trocken');

  // No job, no card.
  await expect(page.getByTestId('job-card')).toHaveCount(0);

  await commandByApi(request, owner, mowerId, { kind: 'start_mow', area_ids: ['front'] });
  const card = page.getByTestId('job-card');
  await expect(card).toBeVisible({ timeout: 60_000 });
  await expect(card.getByTestId('job-area')).toHaveText('Front lawn', { timeout: 60_000 });
  await expect(card.getByTestId('job-area')).toHaveAttribute('data-kind', 'area');
  await expect(card.getByTestId('job-remaining')).not.toHaveText('—');
  const percent = async () =>
    Number((await card.getByTestId('job-progress').getAttribute('aria-valuenow')) ?? '-1');
  const first = await percent();
  // The socket brings the next states: the progress grows without a reload.
  await expect.poll(percent, { timeout: 90_000, intervals: [1_000] }).toBeGreaterThan(first);
  await expect(card.getByTestId('job-mowed')).toContainText('m²');

  await commandByApi(request, owner, mowerId, { kind: 'pause_mow' });
  await expect(card).toHaveAttribute('data-paused', 'true', { timeout: 30_000 });
  await expect(card.getByTestId('job-paused')).toContainText('angehalten');

  await commandByApi(request, owner, mowerId, { kind: 'return_to_dock', end_job: true });
  await expect(page.getByTestId('job-card')).toHaveCount(0, { timeout: 120_000 });
  csp.assertNone();
});
