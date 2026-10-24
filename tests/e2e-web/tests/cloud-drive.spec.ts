/**
 * Manual driving over the Mammotion cloud (ADR 0049, desktop), against the fake Mammotion cloud
 * of the test stack (`GATEWAY_FAKE_MAMMOTION_CLOUD`, see stack.env): its mowers offer the
 * vendor's cloud remote drive without a Bluetooth bridge, forwards and turns only.
 *
 * - The owner account: `manual_drive` without `drive_reverse`; a nudge (one `manual_drive`
 *   frame with `ttl_ms` 500, as the Home Assistant integration sends it) is accepted, a
 *   backwards nudge is rejected with the no-reverse reason; the drive page opens a session, marks
 *   the backwards half off and drives forwards.
 * - A shared account (`shared-...@fake.mowdeck.test`, refused with code 50803 like the real
 *   vendor): the nudge is rejected with the owner account reason.
 *
 * The fake devices have fixed IoT ids (one physical mower per installation), so every test
 * removes its cloud mowers and linked accounts again.
 */
import { expect, test, type APIRequestContext } from '@playwright/test';

import { RUN, actor, loginByUi, registerByApi, watchCsp, type Actor } from './support';

const FLEET = '/api/v1/fleet';
const FAKE_PASSWORD = 'fake-cloud-password';
const DEVICE = 'Luba-FAKE0001';

interface CommandRecord {
  status: string;
  reason?: string | null;
}

async function removeCloudSetup(request: APIRequestContext, owner: Actor): Promise<void> {
  const headers = { Authorization: `Bearer ${owner.token}` };
  const mowers = await request.get(`${FLEET}/mowers`, { headers });
  if (mowers.ok()) {
    const rows = ((await mowers.json()) as { items: { id: string; connection_kind: string }[] })
      .items;
    for (const mower of rows.filter((m) => m.connection_kind === 'cloud')) {
      await request.delete(`${FLEET}/mowers/${mower.id}`, { headers });
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

/** Link the fake account `email` and register its online mower; returns the mower id. */
async function cloudMower(
  request: APIRequestContext,
  owner: Actor,
  email: string,
): Promise<string> {
  const headers = { Authorization: `Bearer ${owner.token}` };
  const linked = await request.post(`${FLEET}/linked-accounts`, {
    headers,
    data: { email, password: FAKE_PASSWORD },
    timeout: 60_000,
  });
  expect(linked.status(), await linked.text()).toBe(201);
  const accountId = ((await linked.json()) as { id: string }).id;
  const created = await request.post(`${FLEET}/linked-accounts/${accountId}/mowers`, {
    headers,
    data: { mowers: [{ device_name: DEVICE, name: `Cloud ${RUN}` }] },
    timeout: 60_000,
  });
  expect(created.status(), await created.text()).toBeLessThan(300);
  const body = (await created.json()) as { created: { id: string }[]; failed: unknown[] };
  expect(body.failed).toEqual([]);
  return body.created[0]!.id;
}

async function driveCapabilities(
  request: APIRequestContext,
  owner: Actor,
  mowerId: string,
): Promise<{ manual_drive?: boolean; drive_reverse?: boolean } | null> {
  const response = await request.get(`/api/v1/telemetry/mowers/${mowerId}/capabilities`, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  return response.ok()
    ? ((await response.json()) as { capabilities: { manual_drive?: boolean } }).capabilities
    : null;
}

/** Send a nudge (one `manual_drive` frame) and wait for its final record. */
async function nudge(
  request: APIRequestContext,
  owner: Actor,
  mowerId: string,
  linear: number,
): Promise<CommandRecord> {
  const auth = { Authorization: `Bearer ${owner.token}` };
  const sent = await request.post(`${FLEET}/mowers/${mowerId}/commands`, {
    headers: auth,
    data: { command: { kind: 'manual_drive', linear, angular: 0, ttl_ms: 500 } },
  });
  expect(sent.status(), await sent.text()).toBe(202);
  const { command_id: commandId } = (await sent.json()) as { command_id: string };
  let record: CommandRecord = { status: 'pending' };
  await expect
    .poll(
      async () => {
        const r = await request.get(`${FLEET}/commands/${commandId}`, { headers: auth });
        if (r.ok()) record = (await r.json()) as CommandRecord;
        return record.status;
      },
      { timeout: 30_000, intervals: [250] },
    )
    .not.toBe('pending');
  return record;
}

test('cloud drive: owner account drives forwards, no reverse; the drive page', async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('cloud-drive'));
  try {
    const mowerId = await cloudMower(request, owner, `cdrive-${RUN}@fake.mowdeck.test`);
    await expect
      .poll(async () => (await driveCapabilities(request, owner, mowerId))?.manual_drive, {
        timeout: 60_000,
      })
      .toBe(true);
    expect((await driveCapabilities(request, owner, mowerId))?.drive_reverse).toBe(false);

    const forward = await nudge(request, owner, mowerId, 0.4);
    expect(forward.status, forward.reason ?? '').toBe('accepted');
    const back = await nudge(request, owner, mowerId, -0.4);
    expect(back.status).toBe('rejected');
    expect(back.reason).toContain('cannot drive backwards');

    await loginByUi(page, owner);
    await page.goto(`/mowers/${mowerId}/drive`);
    await expect(page.getByTestId('drive-state')).toHaveText('Startklar');
    await expect(page.getByTestId('drive-pad')).toHaveAttribute('data-reverse', 'false');
    await expect(page.getByTestId('drive-no-reverse')).toBeVisible();
    await page.getByTestId('drive-start').click();
    await expect(page.getByTestId('drive-state')).toHaveText('Fahrt aktiv');
    const pad = page.getByTestId('joystick');
    await expect(pad).toHaveAttribute('aria-disabled', 'false');
    await pad.scrollIntoViewIfNeeded();
    const box = (await pad.boundingBox())!;
    const cx = box.x + box.width / 2;
    const cy = box.y + box.height / 2;
    await page.mouse.move(cx, cy);
    await page.mouse.down();
    await page.mouse.move(cx, cy - box.height * 0.4, { steps: 5 });
    await expect(page.getByTestId('drive-rate-acked')).not.toHaveText(/^0/, { timeout: 5_000 });
    await page.waitForTimeout(1_000);
    await page.mouse.up();
    // Still open after the frames: the cloud session took the token and drove.
    await expect(page.getByTestId('drive-state')).toHaveText('Fahrt aktiv');
    await page.getByTestId('drive-stop').click();
    await expect(page.getByTestId('drive-state')).toHaveText('Startklar');
    csp.assertNone();
  } finally {
    await removeCloudSetup(request, owner);
  }
});

test('cloud drive: a shared account is told to use the owner account', async ({ request }) => {
  test.setTimeout(180_000);
  const owner = await registerByApi(request, actor('cloud-shared'));
  try {
    const mowerId = await cloudMower(request, owner, `shared-cdrive-${RUN}@fake.mowdeck.test`);
    await expect
      .poll(async () => (await driveCapabilities(request, owner, mowerId))?.manual_drive, {
        timeout: 60_000,
      })
      .toBe(true);
    const refused = await nudge(request, owner, mowerId, 0.4);
    expect(refused.status).toBe('rejected');
    expect(refused.reason).toContain("needs the mower's owner account");
  } finally {
    await removeCloudSetup(request, owner);
  }
});
