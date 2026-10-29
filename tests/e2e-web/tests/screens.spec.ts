/**
 * Review screenshots of the main pages at phone (390 x 844) and desktop (1280 x 800) width,
 * written to apps/web/screenshots/<page>-<size>.png (run: pnpm --filter e2e-web screens). The
 * map shots: `mower-map-phone` (full screen), `mower-map-desktop` (RTK tint on) and their dark
 * variants, `mower-map-rtk-desktop` (the RTK heat layer of the last 24 h with its legend) and
 * `mower-map-coverage-desktop` (the coverage layer with the missed spots). Notifications:
 * `notifications` (settings), `inbox`; connection forms: `connection-cloud-phone` (the linked
 * account connect form with a missing password) and `connection-ble-desktop` (filled, nothing is
 * saved); schedules: `schedules-phone` (the list),
 * `schedule-edit-desktop` (the form with the preview) and `schedule-runs-phone`. Phase 3:
 * `health-phone` and `health-desktop` (the health page of the mowing mower), `drive-phone` and
 * `drive-desktop-dark` (a live drive session of the docked mower with the stick held).
 */
import { fileURLToPath } from 'node:url';

import { expect, test, type Page } from '@playwright/test';

import {
  actor,
  commandByApi,
  createMowerByApi,
  loginByUi,
  pickProvider,
  registerByApi,
  waitForMap,
} from './support';

const OUT = fileURLToPath(new URL('../../../apps/web/screenshots/', import.meta.url));
const SIZES = {
  phone: { width: 390, height: 844 },
  desktop: { width: 1280, height: 800 },
} as const;

async function shot(page: Page, name: string, size: keyof typeof SIZES): Promise<void> {
  await page.waitForTimeout(400); // fonts and the last state flash
  await page.screenshot({ path: `${OUT}${name}-${size}.png`, animations: 'disabled' });
}

for (const size of Object.keys(SIZES) as (keyof typeof SIZES)[]) {
  test(`screenshots at ${size} width`, async ({ browser, request }) => {
    const owner = await registerByApi(request, actor(`shot-${size}`));
    const mowerId = await createMowerByApi(request, owner, 'Luba 2 AWD');
    const secondId = await createMowerByApi(request, owner, 'Front garden');
    await waitForMap(request, owner, mowerId);
    await waitForMap(request, owner, secondId);

    const context = await browser.newContext({
      viewport: SIZES[size],
      deviceScaleFactor: 1,
      locale: 'de-DE',
      colorScheme: 'light',
      isMobile: size === 'phone',
      hasTouch: size === 'phone',
    });
    const page = await context.newPage();

    await page.goto('/login');
    await page.locator('#username').fill(owner.username);
    await shot(page, 'login', size);

    await loginByUi(page, owner);
    await commandByApi(request, owner, mowerId, { kind: 'start_mow', area_ids: ['front', 'back'] });
    await expect(page.getByTestId(`mower-row-${mowerId}`)).toContainText('Mäht', {
      timeout: 30_000,
    });
    await shot(page, 'mowers', size);

    await page.getByTestId(`mower-row-${mowerId}`).click();
    await expect(page.getByTestId('mower-state')).toContainText('Mäht');
    const map = page.getByTestId('mower-map');
    await expect(map).toHaveAttribute('data-ready', 'true');
    await expect(map.getByTestId('mower-marker')).toBeVisible();
    // Let the mower drive a while, so the driven route shows.
    await expect
      .poll(async () => Number(await map.getAttribute('data-route-points')), { timeout: 60_000 })
      .toBeGreaterThan(15);
    await shot(page, 'mower-detail', size);

    await page.emulateMedia({ colorScheme: 'dark' });
    await shot(page, 'mower-detail-dark', size);
    await page.emulateMedia({ colorScheme: 'light' });

    // The map on its own: full screen on the phone, with the layer panel on the desktop.
    if (size === 'phone') {
      await map.getByTestId('map-expand').click();
      await shot(page, 'mower-map', size);
      await page.emulateMedia({ colorScheme: 'dark' });
      await shot(page, 'mower-map-dark', size);
      await page.emulateMedia({ colorScheme: 'light' });
      await map.getByTestId('map-expand').click();
    } else {
      await map.getByTestId('map-layers').click();
      await map.getByTestId('map-rtk-tint').click();
      await map.getByTestId('map-layers').click();
      await shot(page, 'mower-map', size);
      await page.emulateMedia({ colorScheme: 'dark' });
      await shot(page, 'mower-map-dark', size);
      await page.emulateMedia({ colorScheme: 'light' });

      // The RTK heat layer of the last 24 h (Telemetry's heatmap) with its legend.
      await page.getByTestId('rtk-range').getByText('Heute').click();
      await expect(page.getByTestId('rtk-share-fixed')).toContainText('%');
      await map.getByTestId('map-layers').click();
      await map.getByTestId('map-rtk-tint').click();
      await map.getByTestId('map-layers').click();
      const heat = page.getByTestId('heatmap-controls');
      await heat.getByTestId('heatmap-layer-rtk').click();
      await heat.getByTestId('heatmap-period').selectOption('24h');
      await expect(heat.getByTestId('heatmap-legend')).toHaveAttribute('data-kind', 'fix');
      await expect
        .poll(async () => Number(await map.getAttribute('data-heat-cells')), { timeout: 30_000 })
        .toBeGreaterThan(0);
      await shot(page, 'mower-map-rtk', size);

      // Coverage with the missed spots under the map.
      await heat.getByTestId('heatmap-layer-coverage').click();
      await expect(heat.getByTestId('heatmap-legend')).toHaveAttribute('data-kind', 'ratio');
      await expect(heat.getByTestId('heatmap-status')).toHaveAttribute(
        'data-state',
        /ready|empty/,
        { timeout: 30_000 },
      );
      await expect(page.getByTestId('heatmap-gaps')).toBeVisible();
      await shot(page, 'mower-map-coverage', size);
      await heat.getByTestId('heatmap-layer-off').click();
    }

    await page.goto(`/mowers/${mowerId}/settings`);
    await expect(page.locator('#name')).toHaveValue('Luba 2 AWD');
    await shot(page, 'settings', size);

    // Health of the mowing mower: totals, today's bar, the battery trace.
    await page.goto(`/mowers/${mowerId}/health`);
    await expect(page.getByTestId('health-blade-hours')).not.toContainText('keine Daten');
    await expect(page.getByTestId('battery-trace')).toBeVisible();
    await shot(page, 'health', size);

    // A live drive session of the docked mower with the stick held forward and to the right.
    await page.goto(`/mowers/${secondId}/drive`);
    await expect(page.getByTestId('drive-start')).toBeEnabled({ timeout: 30_000 });
    await page.getByTestId('drive-start').click();
    await expect(page.getByTestId('drive-state')).toHaveText('Fahrt aktiv');
    const pad = page.getByTestId('joystick');
    await pad.scrollIntoViewIfNeeded();
    const box = (await pad.boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.68, box.y + box.height * 0.18, { steps: 4 });
    await page.waitForTimeout(800);
    if (size === 'phone') {
      // The session strip at the top, the held stick below it.
      await page.getByTestId('drive-strip').evaluate((el) => el.scrollIntoView({ block: 'start' }));
      await shot(page, 'drive', size);
    } else {
      await page.emulateMedia({ colorScheme: 'dark' });
      await page.waitForTimeout(400);
      await page.screenshot({ path: `${OUT}drive-desktop-dark.png`, animations: 'disabled' });
      await page.emulateMedia({ colorScheme: 'light' });
    }
    await page.mouse.up();
    await page.getByTestId('drive-stop').click();
    await expect(page.getByTestId('drive-state')).toHaveText('Startklar');

    // Notifications: settings with a saved preference, the inbox with a few entries.
    const auth = { Authorization: `Bearer ${owner.token}` };
    await request.put('/api/v1/notifications/preferences', {
      headers: auth,
      data: {
        enabled_kinds: ['mowing_finished', 'mower_stuck', 'battery_low', 'mower_offline'],
        all_mowers: true,
        quiet_hours: { start: '22:00', end: '07:00' },
        timezone: 'Europe/Berlin',
        language: 'de',
        channels: ['webpush', 'ntfy'],
      },
    });
    await request.post('/api/v1/notifications/test', { headers: auth });
    await page.goto('/notifications/settings');
    await expect(page.getByTestId('kind-mowing_finished')).toHaveAttribute('aria-checked', 'true');
    await shot(page, 'notifications', size);
    await page.goto('/notifications');
    await expect(page.getByTestId('inbox-item').first()).toBeVisible();
    await shot(page, 'inbox', size);

    // Schedules: a daily schedule with areas and an angle rotation (its next firing lies within
    // the static weather's forecast), a manual run (the mower is busy mowing, so it is skipped)
    // and, on the phone, the next firing skipped; plus a disabled every-3-days schedule.
    const created = await request.post('/api/v1/scheduler/schedules', {
      headers: auth,
      data: {
        name: 'Morgens vorne',
        target_type: 'mower',
        target_id: mowerId,
        kind: 'weekly',
        days_of_week: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'],
        start_time: '07:00',
        duration_minutes: 90,
        area_ids: ['front', 'back'],
        rotation_angles: [0, 90],
      },
    });
    expect(created.status(), await created.text()).toBe(201);
    const scheduleId = ((await created.json()) as { id: string }).id;
    await request.post('/api/v1/scheduler/schedules', {
      headers: auth,
      data: {
        name: 'Rasen hinten',
        target_type: 'mower',
        target_id: mowerId,
        kind: 'every_n_days',
        anchor_date: '2026-10-12',
        interval_days: 3,
        start_time: '16:30',
        duration_minutes: 150,
        area_ids: ['back'],
        enabled: false,
      },
    });
    await request.post(`/api/v1/scheduler/schedules/${scheduleId}/run-now`, { headers: auth });
    if (size === 'phone') {
      await request.post(`/api/v1/scheduler/schedules/${scheduleId}/skip-next`, { headers: auth });
      await page.goto('/schedules');
      await expect(page.getByTestId('schedule-last-outcome').first()).toBeVisible();
      await expect(page.getByTestId('schedule-skipped')).toBeVisible();
      await shot(page, 'schedules', size);
      await page.goto(`/schedules/${scheduleId}/runs`);
      await expect(page.getByTestId('run-row').first()).toBeVisible();
      await shot(page, 'schedule-runs', size);
    } else {
      // Rain at the mower (development endpoint of the static provider): the preview holds.
      const rain = await request.put('/api/v1/scheduler/dev/weather', {
        headers: auth,
        data: { mode: 'rain' },
      });
      await page.goto(`/schedules/${scheduleId}`);
      await expect(page.getByTestId('sched-map')).toHaveAttribute('data-ready', 'true');
      await expect(page.getByTestId('preview-firings')).toBeVisible();
      if (rain.ok()) await expect(page.getByTestId('preview-strip')).toBeVisible();
      await shot(page, 'schedule-edit', size);
      if (rain.ok()) {
        await request.put('/api/v1/scheduler/dev/weather', {
          headers: auth,
          data: { mode: 'dry' },
        });
      }
    }

    // Connection forms, generated from Fleet's schemas.
    await page.goto('/mowers/new');
    await page.locator('#name').fill(size === 'phone' ? 'Luba Cloud' : 'Luba BLE');
    if (size === 'phone') {
      // Cloud mowers come from a linked account: the connect form, nothing is sent.
      await page.locator('#connection_kind').selectOption('cloud');
      await pickProvider(page, 'mammotion');
      await page.locator('#linked-email').fill('second-account@example.com');
      await page.getByTestId('linked-connect-submit').click(); // shows the missing password
      await expect(page.locator('#linked-password')).toHaveAttribute('aria-invalid', 'true');
      await shot(page, 'connection-cloud', size);
    } else {
      await page.locator('#connection_kind').selectOption('ble_bridge');
      await page.locator('#opt-host').fill('mowdeck-bridge.local');
      await page.locator('#opt-noise_psk').fill('c2VjcmV0LWtleS1mb3ItdGhlLXNjcmVlbnNob3Q=');
      await page.locator('#opt-ble_address').fill('AA:BB:CC:DD:EE');
      await page.getByTestId('mower-submit').click(); // shows the address format
      await page.getByText('Erweitert').click();
      await shot(page, 'connection-ble', size);
    }
    await context.close();
  });
}

/**
 * Phase 4: `fleet-map-desktop` and `fleet-map-phone` (three simulated mowers at two sites, one
 * mowing, one paused, one docked), `sites-phone` and `reports-desktop` (after the job).
 */
test('fleet screenshots', async ({ browser, request }) => {
  test.setTimeout(240_000);
  const owner = await registerByApi(request, actor('shot-fleet'));
  const auth = { Authorization: `Bearer ${owner.token}` };
  const site = await request.post('/api/v1/fleet/sites', {
    headers: auth,
    data: {
      name: 'Werk Süd',
      timezone: 'Europe/Berlin',
      address: { city: 'München', country: 'DE' },
      latitude: 48.1186,
      longitude: 11.6012,
    },
  });
  expect(site.status(), await site.text()).toBe(201);
  const siteId = ((await site.json()) as { id: string }).id;
  const mower = async (name: string, origin: [number, number], siteOf?: string) => {
    const response = await request.post('/api/v1/fleet/mowers', {
      headers: auth,
      data: {
        name,
        connection_kind: 'simulated',
        connection_options: { origin_lat: origin[0], origin_lon: origin[1] },
        ...(siteOf ? { site_id: siteOf } : {}),
      },
    });
    expect(response.status(), await response.text()).toBe(201);
    return ((await response.json()) as { id: string }).id;
  };
  const nord = await mower('Luba Nord', [48.137154, 11.576124]);
  const park = await mower('Yuka Park', [48.1408, 11.5869]);
  const sued = await mower('Luba Werk', [48.1186, 11.6012], siteId);
  const group = await request.post('/api/v1/fleet/groups', {
    headers: auth,
    data: { name: 'Kolonne Süd', site_id: siteId },
  });
  const groupId = ((await group.json()) as { id: string }).id;
  await request.put(`/api/v1/fleet/mowers/${sued}/groups`, {
    headers: auth,
    data: { group_ids: [groupId] },
  });
  for (const id of [nord, park, sued]) await waitForMap(request, owner, id);
  await commandByApi(request, owner, nord, { kind: 'start_mow', area_ids: ['front', 'back'] });
  await commandByApi(request, owner, sued, { kind: 'start_mow', area_ids: ['back'] });
  await new Promise((resolve) => setTimeout(resolve, 15_000));
  await commandByApi(request, owner, sued, { kind: 'pause_mow' });

  for (const size of ['desktop', 'phone'] as const) {
    const context = await browser.newContext({
      viewport: SIZES[size],
      deviceScaleFactor: 1,
      locale: 'de-DE',
      colorScheme: 'light',
      isMobile: size === 'phone',
      hasTouch: size === 'phone',
    });
    const page = await context.newPage();
    await loginByUi(page, owner);
    await page.goto('/fleet');
    const map = page.getByTestId('fleet-map');
    await expect(map).toHaveAttribute('data-markers', '3');
    await expect(map).not.toHaveAttribute('data-areas', '0');
    await page.waitForTimeout(1_500); // the fit animation and the live states
    await map.getByTestId('fleet-marker').first().click();
    await shot(page, 'fleet-map', size);
    if (size === 'phone') {
      await page.goto('/fleet/sites');
      await expect(page.getByTestId(`site-${siteId}`)).toBeVisible();
      await shot(page, 'sites', size);
    } else {
      await page.goto('/reports');
      await expect(page.getByTestId(`report-row-${nord}`)).toBeVisible();
      await page.getByTestId('report-period').locator('[data-value="today"]').click();
      await expect(page.getByTestId('report-total-jobs')).toContainText('2');
      await shot(page, 'reports', size);
    }
    await context.close();
  }
  await commandByApi(request, owner, nord, { kind: 'return_to_dock' });
});
