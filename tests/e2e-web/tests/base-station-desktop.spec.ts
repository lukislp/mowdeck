/**
 * RTK base stations under Fleet (desktop, 1280 x 800), ADR 0047. With the simulator: the empty
 * page explains, the simulated base station is added from the sheet, a mower is assigned and the
 * base's state arrives over the socket (LoRa, receiving, satellites, position); the firmware
 * offered by the simulator is checked and installed through the mower after the confirmation,
 * until the new version is installed; rename, unassign and remove. With the fake Mammotion cloud
 * (stack.env): the base station of a linked account is listed in the sheet and added; the test
 * removes it and the account again (the fake devices are unique installation wide).
 */
import { expect, test, type APIRequestContext } from '@playwright/test';

import {
  RUN,
  actor,
  createMowerByApi,
  loginByUi,
  registerByApi,
  waitForMap,
  watchCsp,
  type Actor,
} from './support';

const FLEET = '/api/v1/fleet';
const NEW_BASE = 'sim-base-1.1.0';

async function createSimMower(
  request: APIRequestContext,
  owner: Actor,
  name: string,
): Promise<string> {
  const response = await request.post(`${FLEET}/mowers`, {
    headers: { Authorization: `Bearer ${owner.token}` },
    // The simulated base station offers a newer firmware (ADR 0047, the simulator's option).
    data: {
      name,
      connection_kind: 'simulated',
      connection_options: { base_station_firmware_available: NEW_BASE },
    },
  });
  expect(response.status(), await response.text()).toBe(201);
  return ((await response.json()) as { id: string }).id;
}

/** Remove the base stations and then the linked accounts of `owner`. */
async function removeCloudSetup(request: APIRequestContext, owner: Actor): Promise<void> {
  const headers = { Authorization: `Bearer ${owner.token}` };
  const bases = await request.get(`${FLEET}/base-stations`, { headers });
  if (bases.ok()) {
    for (const base of ((await bases.json()) as { items: { id: string }[] }).items) {
      await request.delete(`${FLEET}/base-stations/${base.id}`, { headers });
    }
  }
  const accounts = await request.get(`${FLEET}/linked-accounts`, { headers });
  if (accounts.ok()) {
    for (const account of ((await accounts.json()) as { items: { id: string }[] }).items) {
      const removed = await request.delete(`${FLEET}/linked-accounts/${account.id}`, { headers });
      expect(removed.status(), await removed.text()).toBe(204);
    }
  }
}

test('base station: add the simulated one, assign a mower, live state, firmware update', async ({
  page,
  request,
}) => {
  test.setTimeout(300_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('base'));
  const mowerId = await createSimMower(request, owner, `Luba base ${RUN}`);
  await createMowerByApi(request, owner, `Luba plain ${RUN}`);
  await waitForMap(request, owner, mowerId);

  await loginByUi(page, owner);
  await page.goto('/fleet');
  await page.getByTestId('fleet-tab-base-stations').click();
  await page.waitForURL(/\/fleet\/base-stations$/);
  await expect(page.getByTestId('bs-empty')).toContainText('Noch keine Basisstation');

  // Add the simulator's base station (no linked account: the sheet says so).
  await page.getByTestId('bs-add-empty').click();
  const sheet = page.getByTestId('bs-register');
  await expect(sheet.getByTestId('bs-register-none')).toBeVisible();
  await sheet.getByTestId('bs-simulated-add').click();
  const base = page.locator('li[data-testid^="bs-"][data-name]');
  await expect(base).toHaveCount(1);
  await expect(base.getByTestId('bs-name')).toHaveText('Simulierte Basisstation');
  await expect(base.getByTestId('bs-state-empty')).toContainText('ordne einen Mäher zu');

  // Assign the mower: its state reports the base station, live.
  await base.locator('select').selectOption({ label: `Luba base ${RUN}` });
  await base.getByTestId('bs-link').click();
  await expect(base.getByTestId('bs-mowers')).toContainText(`Luba base ${RUN}`);
  const state = base.getByTestId('bs-state');
  await expect(state).toBeVisible({ timeout: 60_000 });
  await expect(state.getByTestId('bs-correction')).toContainText('LoRa-Funk');
  await expect(state.getByTestId('bs-correction')).toContainText('Kanal 7');
  await expect(state.getByTestId('bs-link')).toHaveAttribute('data-up', 'true');
  await expect(state.getByTestId('bs-satellites')).toContainText('32');
  await expect(state.getByTestId('bs-position')).toContainText('48.13');
  await expect(base.getByTestId('bs-reported-by')).toContainText(`Luba base ${RUN}`);

  // Firmware: check, then install through the mower after the confirmation.
  const firmware = base.getByTestId('bs-firmware');
  await firmware.getByTestId('bs-fw-check').click();
  await expect(firmware.getByTestId('bs-fw-available')).toHaveText(NEW_BASE, { timeout: 60_000 });
  await expect(firmware).toHaveAttribute('data-state', 'available');
  await firmware.getByTestId('bs-fw-install').click();
  const confirm = page.getByTestId('bs-fw-confirm');
  await expect(confirm).toContainText('keine RTK-Korrekturen');
  await confirm.getByTestId('bs-fw-go').click();
  await expect(firmware.getByTestId('bs-fw-request')).toBeVisible({ timeout: 30_000 });
  await expect(firmware.getByTestId('bs-fw-installed')).toHaveText(NEW_BASE, { timeout: 180_000 });

  // Rename, unassign, remove.
  await base.getByTestId('bs-rename').click();
  await base.getByLabel('Name').fill(`Garten-Basis ${RUN}`);
  await base.getByTestId('bs-rename-save').click();
  await expect(base.getByTestId('bs-name')).toHaveText(`Garten-Basis ${RUN}`);
  await base.getByTestId('bs-unlink').click();
  await expect(base.getByTestId('bs-mowers')).toHaveCount(0);
  await base.getByTestId('bs-delete').click();
  await page.getByTestId('bs-delete-go').click();
  await expect(page.getByTestId('bs-empty')).toBeVisible();
  csp.assertNone();
});

test('base station: added from a linked Mammotion account', async ({ page, request }) => {
  test.setTimeout(180_000);
  const owner = await registerByApi(request, actor('base-cloud'));
  const headers = { Authorization: `Bearer ${owner.token}` };
  try {
    const linked = await request.post(`${FLEET}/linked-accounts`, {
      headers,
      data: {
        provider: 'mammotion',
        credentials: { email: `base-${RUN}@fake.mowdeck.test`, password: 'fake-cloud-password' },
      },
      timeout: 60_000,
    });
    expect(linked.status(), await linked.text()).toBe(201);

    await loginByUi(page, owner);
    await page.goto('/fleet/base-stations');
    await page.getByTestId('bs-add-empty').click();
    const device = page.getByTestId('bs-register').locator('[data-device="RTK-FAKE0001"]');
    await expect(device).toBeVisible({ timeout: 30_000 });
    await device.getByTestId('bs-device-add').click();
    const base = page.locator('li[data-testid^="bs-"][data-name]');
    await expect(base).toHaveCount(1);
    await expect(base).toContainText('Verknüpftes Konto');
    await expect(base).toContainText('RTK-FAKE0001');
    // No cloud mower is assigned: nothing to show yet, and the firmware needs a mower.
    await expect(base.getByTestId('bs-state-empty')).toBeVisible();
    await expect(base.getByTestId('bs-fw-blocker')).toHaveAttribute('data-reason', 'no_mower');
  } finally {
    await removeCloudSetup(request, owner);
  }
});
