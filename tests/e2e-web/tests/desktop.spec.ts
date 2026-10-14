/**
 * Desktop layout (1280 x 800): nav rail, the mower detail with the map and the instrument
 * column, the stop always in view, live state on the map; no CSP violation on the way.
 */
import { expect, test } from '@playwright/test';

import {
  actor,
  commandByApi,
  createMowerByApi,
  loginByUi,
  registerByApi,
  waitForMap,
  watchCsp,
} from './support';

test('desktop: list, detail with instruments and the stop, live mowing', async ({
  page,
  request,
}) => {
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('desk'));
  const mowerId = await createMowerByApi(request, owner, 'Garden north');
  await waitForMap(request, owner, mowerId);
  await loginByUi(page, owner);
  await expect(page.getByTestId('nav-mowers')).toBeVisible();
  const row = page.getByTestId(`mower-row-${mowerId}`);
  await expect(row).toContainText(/Angedockt|Lädt/, { timeout: 30_000 });
  await row.click();
  await expect(page.getByTestId('emergency-stop')).toBeInViewport();
  const map = page.getByTestId('mower-map');
  await expect(map).toHaveAttribute('data-ready', 'true');
  await expect(map.locator('path[data-area]')).toHaveCount(2);
  await expect(map.getByRole('img', { name: 'Ladestation' })).toBeVisible();
  // No full-screen mode on the desktop; the map controls are keyboard reachable.
  await expect(map.getByTestId('map-expand')).toHaveCount(0);
  await map.getByRole('button', { name: 'Ganze Karte zeigen' }).focus();
  await page.keyboard.press('Enter');
  await commandByApi(request, owner, mowerId, { kind: 'start_mow', area_ids: ['back'] });
  await expect(page.getByTestId('mower-state')).toContainText('Mäht', { timeout: 30_000 });
  await expect(map.getByTestId('mower-marker')).toHaveAttribute('data-tone', 'mowing');
  await expect(page.getByTestId('recent-commands')).toContainText('Mähen starten');
  csp.assertNone();
});
