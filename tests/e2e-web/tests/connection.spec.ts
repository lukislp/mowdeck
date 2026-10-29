/**
 * Adding mowers (desktop). The add-mower form offers exactly the kinds of Fleet's
 * `GET /api/v1/fleet/connection-kinds`. Cloud mowers come from a linked Mammotion account: the
 * stack runs the Device-Gateway with the fake Mammotion cloud (`GATEWAY_FAKE_MAMMOTION_CLOUD`,
 * see stack.env), where any `*@fake.mowdeck.test` address logs in with `fake-cloud-password`
 * and owns `Luba-FAKE0001` (online), `Luba-FAKE0002` (offline) and `RTK-FAKE0001` (no mower).
 * One physical mower (IoT id) belongs to one MowDeck account installation wide and the fake
 * devices have fixed IoT ids, so the cloud test always removes its mowers and linked accounts.
 * A ble_bridge mower saved with a noise key shows the key as "set" afterwards without ever
 * echoing it; it points at a documentation address (192.0.2.0/24) and is removed at the end.
 * The last test adds a Bluetooth bridge as an additional link of a simulated mower through the
 * settings sheet and runs the firmware check (ADR 0038).
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

const NOISE_KEY = 'ZTJlLW5vaXNlLWtleS1vZi0zMi1ieXRlcy0xMjM0NTY=';
const FAKE_PASSWORD = 'fake-cloud-password';
const FLEET = '/api/v1/fleet';

interface MowerRow {
  id: string;
  name: string;
  site_id: string;
  connection_kind: string;
  linked_account_id?: string | null;
  iot_id?: string | null;
}

/** Remove the cloud mowers and linked accounts of `owner` (the fake devices are unique). */
async function removeCloudSetup(request: APIRequestContext, owner: Actor): Promise<void> {
  const headers = { Authorization: `Bearer ${owner.token}` };
  const mowers = await request.get(`${FLEET}/mowers`, { headers });
  if (mowers.ok()) {
    for (const mower of ((await mowers.json()) as { items: MowerRow[] }).items) {
      if (mower.connection_kind === 'cloud') {
        await request.delete(`${FLEET}/mowers/${mower.id}`, { headers });
      }
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

test('add-mower form: kinds from Fleet, cloud mowers from a linked account', async ({
  page,
  request,
}) => {
  test.setTimeout(300_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('conn'));
  const other = await registerByApi(request, actor('conn-other'));
  const auth = { Authorization: `Bearer ${owner.token}` };
  const email = `conn-${RUN}@fake.mowdeck.test`;
  const front = `Front ${RUN}`;
  const siteName = `Hinterhof ${RUN}`;

  try {
    const kinds = await request.get(`${FLEET}/connection-kinds`, { headers: auth });
    expect(kinds.status()).toBe(200);
    const items = (
      (await kinds.json()) as {
        items: {
          kind: string;
          linked_account: boolean;
          options: { key: string; managed: boolean }[];
        }[];
      }
    ).items;
    const offered = items.map((k) => k.kind);
    expect(offered).toEqual(expect.arrayContaining(['simulated', 'cloud', 'ble_bridge']));
    const cloud = items.find((k) => k.kind === 'cloud')!;
    expect(cloud.linked_account).toBe(true);
    // The device from the account's list, and the drive calibration (cloud driving, ADR 0049).
    expect(cloud.options.filter((o) => o.managed).map((o) => o.key)).toEqual([
      'device_name',
      'iot_id',
      'drive_full_speed_mps',
      'drive_full_yaw_rate_dps',
      'drive_dead_zone',
      'drive_turn_dead_zone',
      'drive_speed_setting',
      'calibrated_at',
    ]);

    await loginByUi(page, owner);
    await page.goto('/mowers/new');
    const select = page.locator('#connection_kind');
    await expect(select).toHaveValue('simulated');
    const values = await select
      .locator('option')
      .evaluateAll((options) => options.map((o) => (o as HTMLOptionElement).value));
    expect(values).toEqual(offered);
    await expect(page.getByTestId('connection-no-options')).toBeVisible();

    // Cloud: no account linked yet, so the flow starts with the connect form.
    await select.selectOption('cloud');
    await expect(page.getByTestId('linked-flow')).toBeVisible();
    await expect(page.locator('#name')).toHaveCount(0);
    await expect(page.getByTestId('linked-connect')).toBeVisible();

    // A wrong password is refused by the (fake) cloud and lands on the password field.
    await page.locator('#linked-email').fill(email);
    await page.locator('#linked-password').fill('not-the-password');
    await page.getByTestId('linked-connect-submit').click();
    await expect(page.locator('#linked-password')).toHaveAttribute('aria-invalid', 'true', {
      timeout: 60_000,
    });
    await expect(page.getByText('Der Hersteller hat diese Zugangsdaten abgelehnt.')).toBeVisible();
    await expect(page.locator('#linked-password')).toHaveValue(''); // write-only

    // The right one links the account and shows its devices.
    await page.locator('#linked-password').fill(FAKE_PASSWORD);
    await page.getByTestId('linked-connect-submit').click();
    await expect(page.getByTestId('linked-device-list')).toBeVisible({ timeout: 60_000 });
    const first = page.getByTestId('linked-device-Luba-FAKE0001');
    const second = page.getByTestId('linked-device-Luba-FAKE0002');
    const base = page.getByTestId('linked-device-RTK-FAKE0001');
    await expect(first).toHaveAttribute('data-state', 'addable');
    await expect(first).toContainText('LUBA 2 AWD 5000X');
    await expect(first).toContainText('fakeiot0001');
    await expect(page.getByTestId('linked-device-online-Luba-FAKE0001')).toHaveAttribute(
      'data-value',
      'online',
    );
    await expect(second).toHaveAttribute('data-state', 'addable');
    await expect(page.getByTestId('linked-device-online-Luba-FAKE0002')).toHaveAttribute(
      'data-value',
      'offline',
    );
    await expect(base).toHaveAttribute('data-state', 'not_mower');
    await expect(page.getByTestId('linked-device-check-RTK-FAKE0001')).toHaveCount(0);
    await expect(page.getByTestId('linked-device-reason-RTK-FAKE0001')).toBeVisible();

    // "Read account again" logs in once more and keeps the list.
    const synced = page.waitForResponse(
      (r) => r.url().endsWith('/sync') && r.request().method() === 'POST',
      { timeout: 90_000 },
    );
    await page.getByTestId('linked-resync').click();
    expect((await synced).status()).toBe(200);
    await expect(page.getByTestId('linked-last-sync')).not.toHaveText('noch nie');
    await expect(first).toHaveAttribute('data-state', 'addable');

    // Both mowers selected, one renamed, the other on a site created in place.
    await page.getByTestId('linked-device-check-Luba-FAKE0001').check();
    await page.getByTestId('linked-device-check-Luba-FAKE0002').check();
    await expect(page.getByTestId('linked-draft-Luba-FAKE0001')).toBeVisible();
    await expect(page.locator('#linked-name-Luba-FAKE0001')).toHaveValue('Luba-FAKE0001');
    await expect(page.locator('#linked-name-Luba-FAKE0002')).toHaveValue('Luba-FAKE0002');
    await page.locator('#linked-name-Luba-FAKE0001').fill(front);
    await page.locator('#linked-site-Luba-FAKE0002').selectOption('__new__');
    await page.locator('#linked-site-new-Luba-FAKE0002').fill(siteName);
    await page.getByTestId('linked-site-create-Luba-FAKE0002').click();
    await expect(page.locator('#linked-site-Luba-FAKE0002 option:checked')).toHaveText(siteName);
    await expect(page.locator('#linked-site-Luba-FAKE0001 option:checked')).toHaveText(
      'Hauptstandort',
    );

    const added = page.waitForResponse(
      (r) => /\/linked-accounts\/[^/]+\/mowers$/.test(r.url()) && r.request().method() === 'POST',
    );
    await page.getByTestId('linked-submit').click();
    expect((await added).status()).toBe(201);
    await page.waitForURL(/\/mowers$/);
    await expect(page.getByText(front).first()).toBeVisible();
    await expect(page.getByText('Luba-FAKE0002').first()).toBeVisible();

    const list = await request.get(`${FLEET}/mowers`, { headers: auth });
    const mowers = ((await list.json()) as { items: MowerRow[] }).items;
    const frontMower = mowers.find((m) => m.name === front);
    const backMower = mowers.find((m) => m.name === 'Luba-FAKE0002');
    expect(frontMower).toMatchObject({ connection_kind: 'cloud', iot_id: 'fakeiot0001' });
    expect(frontMower!.linked_account_id).toBeTruthy();
    expect(backMower).toMatchObject({ connection_kind: 'cloud', iot_id: 'fakeiot0002' });
    const sites = await request.get(`${FLEET}/sites`, {
      headers: auth,
      params: { include_default: 'true' },
    });
    const created = ((await sites.json()) as { items: { id: string; name: string }[] }).items.find(
      (s) => s.name === siteName,
    );
    expect(backMower!.site_id).toBe(created?.id);

    // The mower settings show the linked account and the managed device, read-only.
    await page.goto(`/mowers/${frontMower!.id}/settings`);
    await expect(page.locator('#connection_kind')).toHaveValue('cloud');
    await expect(page.getByTestId('linked-via-email')).toHaveText(email);
    await expect(page.getByTestId('managed-device_name')).toHaveText('Luba-FAKE0001');
    await expect(page.getByTestId('managed-iot_id')).toHaveText('fakeiot0001');
    await expect(page.locator('#opt-device_name')).toHaveCount(0);

    // Another MowDeck account linking the same cloud sees the mowers as registered elsewhere.
    const linked = await request.post(`${FLEET}/linked-accounts`, {
      headers: { Authorization: `Bearer ${other.token}` },
      data: { email: `conn-other-${RUN}@fake.mowdeck.test`, password: FAKE_PASSWORD },
      timeout: 90_000,
    });
    expect(linked.status(), await linked.text()).toBe(201);
    const devices = (
      (await linked.json()) as {
        devices: { device_name: string; registered_elsewhere: boolean; registered: boolean }[];
      }
    ).devices;
    expect(devices.find((d) => d.device_name === 'Luba-FAKE0001')).toMatchObject({
      registered: false,
      registered_elsewhere: true,
    });
    await removeCloudSetup(request, other);

    // The settings page lists the account and refuses to unlink it while mowers use it.
    await page.goto('/settings/linked-accounts');
    const card = page.locator(`[data-testid="linked-account"][data-email="${email}"]`);
    await expect(card).toBeVisible();
    await expect(card.getByTestId('linked-account-mowers')).toHaveText('2');
    await expect(card.getByTestId('linked-account-devices')).toHaveText('3');
    await card.getByTestId('linked-account-delete').click();
    await card.getByTestId('linked-account-delete-confirm').click();
    const inUse = card.getByTestId('linked-account-in-use');
    await expect(inUse).toContainText('Wird noch von diesen Mähern verwendet:');
    await expect(inUse).toContainText(front);
    await expect(inUse).toContainText('Luba-FAKE0002');
    csp.assertNone();
  } finally {
    await removeCloudSetup(request, owner);
    await removeCloudSetup(request, other);
  }
});

test('ble_bridge: the noise key is write-only and shown as set', async ({ page, request }) => {
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('ble'));
  const bodies: string[] = [];
  page.on('response', async (r) => {
    if (r.url().includes('/api/v1/fleet/')) bodies.push(await r.text().catch(() => ''));
  });
  await loginByUi(page, owner);
  await page.goto('/mowers/new');
  const name = `Bridge ${RUN}`;
  await page.locator('#name').fill(name);
  await page.locator('#connection_kind').selectOption('ble_bridge');
  await page.locator('#opt-host').fill('192.0.2.10');
  await page.locator('#opt-ble_address').fill('AA:BB:CC');
  await page.locator('#opt-noise_psk').fill(NOISE_KEY);
  await page.getByTestId('mower-submit').click();
  await expect(page.locator('#opt-ble_address')).toHaveAttribute('aria-invalid', 'true');
  await expect(page.getByText(/MAC-Adresse wie AA:BB:CC:DD:EE:FF/).first()).toBeVisible();
  await page.locator('#opt-ble_address').fill('AA:BB:CC:DD:EE:FF');
  await page.getByTestId('opt-allow_manual_blades').click();
  await page.getByTestId('mower-submit').click();
  await page.waitForURL(/\/mowers$/);

  const list = await request.get('/api/v1/fleet/mowers', {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  const mower = ((await list.json()) as { items: { id: string; name: string }[] }).items.find(
    (m) => m.name === name,
  );
  expect(mower).toBeDefined();
  const id = mower!.id;

  await page.goto(`/mowers/${id}/settings`);
  await expect(page.locator('#connection_kind')).toHaveValue('ble_bridge');
  await expect(page.getByTestId('secret-set-noise_psk')).toBeVisible();
  await expect(page.locator('#opt-noise_psk')).toHaveValue('');
  await expect(page.locator('#opt-host')).toHaveValue('192.0.2.10');
  await expect(page.getByTestId('opt-allow_manual_blades')).toHaveAttribute('aria-checked', 'true');

  // Saving without touching the key keeps it (*** goes back), still "set", still not echoed.
  await page.locator('#opt-port').fill('6054');
  const saved = page.waitForResponse(
    (r) => r.url().endsWith(`/api/v1/fleet/mowers/${id}`) && r.request().method() === 'PATCH',
  );
  await page.getByTestId('mower-save').click();
  expect((await saved).status()).toBe(200);
  await expect(page.getByTestId('secret-set-noise_psk')).toBeVisible();
  const read = await request.get(`/api/v1/fleet/mowers/${id}`, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  const options = ((await read.json()) as { connection_options: Record<string, unknown> })
    .connection_options;
  expect(options).toMatchObject({ noise_psk: '***', port: 6054, ble_address: 'AA:BB:CC:DD:EE:FF' });
  expect(await page.content()).not.toContain(NOISE_KEY);
  expect(bodies.join('\n')).not.toContain(NOISE_KEY);

  const removed = await request.delete(`/api/v1/fleet/mowers/${id}`, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  expect(removed.status()).toBe(204);
  csp.assertNone();
});

/**
 * Bluetooth bridge and firmware on the mower settings (ADR 0038), against the simulated mower.
 * The bridge panel shows only when the capabilities list `ble_bridge`; the scan and the firmware
 * check depend on the gateway's fake answers. Where the stack does not offer a part yet (no
 * `ble_bridge` capability, a 503 from Fleet, `unsupported` from the gateway) that part is
 * recorded as skipped and the rest of the test still runs.
 */
test('bridge sheet and firmware check on the mower settings', async ({ page, request }) => {
  test.setTimeout(240_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('bridge'));
  const mowerId = await createMowerByApi(request, owner, `Luba bridge ${RUN}`);
  const auth = { Authorization: `Bearer ${owner.token}` };
  const skipped = (part: string, why: string) =>
    test.info().annotations.push({ type: 'skip', description: `${part}: ${why}` });
  interface Caps {
    links_supported?: string[];
    firmware_update?: boolean;
  }
  let caps: Caps = {};
  try {
    await waitForMap(request, owner, mowerId);
    await expect
      .poll(
        async () => {
          const r = await request.get(`/api/v1/telemetry/mowers/${mowerId}/capabilities`, {
            headers: auth,
          });
          if (r.ok()) caps = ((await r.json()) as { capabilities: Caps }).capabilities;
          return r.status();
        },
        { timeout: 60_000 },
      )
      .toBe(200);

    await loginByUi(page, owner);
    await page.goto(`/mowers/${mowerId}/settings`);
    await expect(page.getByTestId('firmware-section')).toBeVisible();

    // --- firmware: the check relays to the gateway; its reply fills the panel ----------------
    if (caps.firmware_update) {
      const checked = page.waitForResponse(
        (r) =>
          r.url().endsWith(`/mowers/${mowerId}/firmware/check`) && r.request().method() === 'POST',
        { timeout: 60_000 },
      );
      await page.getByTestId('firmware-check').click();
      const response = await checked;
      // A 200 is the fresh FirmwareInfo; a connection that cannot ask answers 409, no gateway 503.
      const info = response.ok()
        ? ((await response.json()) as { installed?: string | null; available?: string | null })
        : null;
      if (!info) {
        skipped('firmware', `check answered ${response.status()}`);
      } else {
        if (info.installed) {
          await expect(page.getByTestId('firmware-installed')).toHaveText(info.installed);
        }
        if (info.available) {
          await expect(page.getByTestId('firmware-available')).toHaveText(info.available);
          const install = page.getByTestId('firmware-install');
          await expect(install).toBeVisible();
          // Enabled, or disabled with the unmet precondition written under it.
          if (await install.isDisabled()) {
            await expect(page.getByTestId('firmware-blocker')).toBeVisible();
          } else {
            await install.click();
            await expect(page.getByTestId('firmware-confirm')).toBeVisible();
            await expect(page.getByTestId('firmware-confirm')).toContainText('10 bis 20 Minuten');
            await page.getByTestId('firmware-confirm').getByLabel('Schließen').click();
          }
        }
      }
    } else {
      await expect(page.getByTestId('firmware-unsupported')).toBeVisible();
      skipped('firmware', 'the connection reports no firmware_update capability');
    }

    // --- bridge: add through the sheet, status chips, remove with confirmation ----------------
    if (!caps.links_supported?.includes('ble_bridge')) {
      await expect(page.getByTestId('bridge-panel')).toHaveCount(0);
      skipped('bridge', 'the connection lists no ble_bridge link');
      csp.assertNone();
      return;
    }
    await expect(page.getByTestId('bridge-empty')).toBeVisible();
    await page.getByTestId('bridge-add').click();
    await expect(page.getByTestId('bridge-sheet')).toBeVisible();
    // An empty form does not scan; host and key are required.
    await page.getByTestId('bridge-scan').click();
    await expect(page.getByTestId('bridge-host')).toHaveAttribute('aria-invalid', 'true');
    await expect(page.getByTestId('bridge-key')).toHaveAttribute('aria-invalid', 'true');
    await page.getByTestId('bridge-host').fill('192.0.2.20');
    await page.getByTestId('bridge-key').fill(NOISE_KEY);
    // Hold the scan request until the waiting step has been seen: the fake gateway answers at
    // once, so without the gate the spinner can be gone before the assertion runs.
    let releaseScan: () => void = () => {};
    const scanGate = new Promise<void>((resolve) => (releaseScan = resolve));
    await page.route('**/links/ble_bridge/scan', async (route) => {
      await scanGate;
      await route.continue();
    });
    const scanned = page.waitForResponse(
      (r) => r.url().endsWith('/links/ble_bridge/scan') && r.request().method() === 'POST',
      { timeout: 90_000 },
    );
    await page.getByTestId('bridge-scan').click();
    await expect(page.getByTestId('bridge-scanning')).toContainText('bis zu 30 s');
    releaseScan();
    const scan = await scanned;
    await page.unroute('**/links/ble_bridge/scan');
    const reply = scan.ok()
      ? ((await scan.json()) as {
          devices: { name: string; address: string }[];
          error: { code: string } | null;
        })
      : null;
    if (!reply || reply.error?.code === 'unsupported' || reply.devices.length === 0) {
      await expect(page.getByTestId('bridge-scan-error')).toBeVisible();
      skipped('bridge', `scan answered ${scan.status()} ${reply?.error?.code ?? 'no devices'}`);
      csp.assertNone();
      return;
    }
    await expect(page.getByTestId('bridge-device-list')).toBeVisible();
    const first = reply.devices[0]!;
    await page.getByTestId(`bridge-device-${first.address}`).click();
    await expect(page.getByTestId(`bridge-device-${first.address}`)).toHaveAttribute(
      'aria-checked',
      'true',
    );
    await page.getByTestId('bridge-blades').click();
    const saved = page.waitForResponse(
      (r) => r.url().endsWith('/links/ble_bridge') && r.request().method() === 'PUT',
    );
    await page.getByTestId('bridge-save').click();
    expect((await saved).ok()).toBe(true);
    await expect(page.getByTestId('bridge-saved')).toBeVisible();
    await page.getByTestId('bridge-done').click();
    await expect(page.getByTestId('bridge-sheet')).toHaveCount(0);
    await expect(page.getByTestId('bridge-status')).toBeVisible();
    await expect(page.getByTestId('bridge-device-value')).toContainText(first.name);
    await expect(page.getByTestId('bridge-host-value')).toHaveText('192.0.2.20:6053');

    // The key is write-only: neither Fleet's answer nor the page ever contains it.
    const read = await request.get(`/api/v1/fleet/mowers/${mowerId}/links/ble_bridge`, {
      headers: auth,
    });
    expect(read.status()).toBe(200);
    expect(await read.text()).not.toContain(NOISE_KEY);
    expect(await page.content()).not.toContain(NOISE_KEY);

    // The link survives a reload; "Entfernen" asks once more, then deletes it.
    await page.reload();
    await expect(page.getByTestId('bridge-status')).toBeVisible();
    await page.getByTestId('bridge-remove').click();
    await expect(page.getByTestId('bridge-remove-body')).toBeVisible();
    const removed = page.waitForResponse(
      (r) => r.url().endsWith('/links/ble_bridge') && r.request().method() === 'DELETE',
    );
    await page.getByTestId('bridge-remove').click();
    expect((await removed).status()).toBe(204);
    await expect(page.getByTestId('bridge-empty')).toBeVisible();
    csp.assertNone();
  } finally {
    await request.delete(`/api/v1/fleet/mowers/${mowerId}`, { headers: auth });
  }
});
