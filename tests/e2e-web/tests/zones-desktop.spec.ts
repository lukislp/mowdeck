/**
 * Virtual zones (ADR 0070) on a mower whose vendor shares no map but reports a GPS position: the
 * online fake Worx mower `Wanda` of the fake clouds (`GATEWAY_FAKE_CLOUDS`, Worx enabled in
 * stack.env) stands at a fixed position. The test links a fake Worx account and adds Wanda by
 * API, waits until Telemetry offers zones (the gateway has published the map without areas and a
 * position: a real condition, polled), opens the zone page from the mower page, draws a zone
 * around the mower by tapping four corners, names it and links it to the mower's own zone 1.
 * Telemetry evaluates the last position at once, so the page says the mower is in the zone and
 * the mower page shows it as a chip. "Start here" sends the linked vendor start (the fake mower
 * then mows zone 1), and deleting the zone empties the list. Every wait is on a condition, never
 * a fixed sleep. Wanda has a fixed serial number, so the test always removes mower and account.
 */
import { expect, test, type APIRequestContext } from '@playwright/test';

import { RUN, actor, loginByUi, registerByApi, watchCsp, type Actor } from './support';

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

test('zones: draw a zone around a GPS mower without a vendor map, see it is inside, start it', async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('zones'));
  const headers = { Authorization: `Bearer ${owner.token}` };
  const email = `zones-${RUN.replace(/[^A-Za-z0-9-]/g, '').toLowerCase()}@fake.mowdeck.test`;
  const zoneName = `Beet ${RUN}`.slice(0, 60);

  try {
    const linked = await request.post(`${FLEET}/linked-accounts`, {
      headers,
      data: { provider: 'worx', credentials: { brand: 'worx', email, password: FAKE_PASSWORD } },
      timeout: 60_000,
    });
    expect(linked.status(), await linked.text()).toBe(201);
    const accountId = ((await linked.json()) as { id: string }).id;
    const added = await request.post(`${FLEET}/linked-accounts/${accountId}/mowers`, {
      headers,
      data: { mowers: [{ device_name: 'Wanda', name: `Wanda ${RUN}` }] },
    });
    expect(added.status(), await added.text()).toBe(201);
    const mowers = await request.get(`${FLEET}/mowers`, { headers });
    const mower = ((await mowers.json()) as { items: { id: string; name: string }[] }).items.find(
      (m) => m.name === `Wanda ${RUN}`,
    );
    expect(mower).toBeTruthy();
    const zonesUrl = `${TELEMETRY}/mowers/${mower!.id}/zones`;

    // Telemetry offers zones once the gateway published the map (no areas) and a position.
    await expect
      .poll(
        async () => {
          const r = await request.get(zonesUrl, { headers });
          return r.ok() ? ((await r.json()) as { availability: string }).availability : null;
        },
        { timeout: 90_000 },
      )
      .toBe('available');

    await loginByUi(page, owner);
    await page.goto(`/mowers/${mower!.id}`);
    const entry = page.getByTestId('map-zones-link');
    await expect(entry).toBeVisible({ timeout: 60_000 });
    await entry.click();
    await expect(page).toHaveURL(new RegExp(`/mowers/${mower!.id}/zones$`));
    await expect(page.getByTestId('zones-page')).toHaveAttribute('data-availability', 'available');
    await expect(page.getByTestId('zones-empty')).toBeVisible();

    // Draw around the mower marker: four taps on the map, then name and link the zone.
    const map = page.getByTestId('zones-map');
    await expect(map).toHaveAttribute('data-ready', 'true');
    const marker = page.getByTestId('mower-marker');
    await expect(marker).toBeVisible({ timeout: 30_000 });
    await page.getByTestId('zones-draw').click();
    await expect(page.getByTestId('zones-drawbar')).toBeVisible();
    const box = (await marker.boundingBox())!;
    const cx = box.x + box.width / 2;
    const cy = box.y + box.height / 2;
    for (const [dx, dy] of [
      [-70, -70],
      [70, -70],
      [70, 70],
      [-70, 70],
    ] as const) {
      await page.mouse.click(cx + dx, cy + dy);
    }
    await expect(map).toHaveAttribute('data-vzone-draft', '4');
    await expect(page.getByTestId('zones-finish')).toBeEnabled();
    await page.getByTestId('zones-finish').click();
    const sheet = page.getByTestId('zones-sheet');
    await expect(sheet).toBeVisible();
    await page.getByTestId('zone-name-input').fill(zoneName);
    await page.locator('#zone-link').selectOption('zone:1');
    await page.getByTestId('zone-save').click();

    // Saved, drawn, and the mower stands in it (evaluated from its last position at once).
    const row = page.getByTestId('zone-row').filter({ hasText: zoneName });
    await expect(row).toBeVisible();
    await expect(map).toHaveAttribute('data-vzones', '1');
    await expect(page.getByTestId('zones-now')).toContainText(zoneName);
    const created = (await (await request.get(zonesUrl, { headers })).json()) as {
      zones: { id: string; start_plan_ref: string | null }[];
      presence: { zone_id: string | null } | null;
    };
    expect(created.zones).toHaveLength(1);
    expect(created.zones[0]!.start_plan_ref).toBe('zone:1');
    expect(created.presence?.zone_id).toBe(created.zones[0]!.id);
    await expect(map).toHaveAttribute('data-vzone-current', created.zones[0]!.id);
    const stats = await request.get(`${zonesUrl}/stats`, { headers, params: { days: 1 } });
    expect(stats.status()).toBe(200);
    expect(((await stats.json()) as { items: { entries: number }[] }).items[0]!.entries).toBe(1);

    // The mower page shows the zone the mower is in.
    await page.goto(`/mowers/${mower!.id}`);
    await expect(page.getByTestId('mower-zone')).toContainText(zoneName, { timeout: 60_000 });
    await page.getByTestId('mower-zone').click();
    await expect(page.getByTestId('zones-page')).toBeVisible();

    // "Start here" sends the linked vendor zone start; the fake mower mows zone 1.
    await row.getByTestId('zone-start').click();
    await expect
      .poll(
        async () => {
          const state = await request.get(`${TELEMETRY}/mowers/${mower!.id}/state`, { headers });
          if (!state.ok()) return null;
          const body = (await state.json()) as {
            state?: { activity?: string; raw?: { zone?: number } };
          };
          return `${body.state?.activity}:${body.state?.raw?.zone}`;
        },
        { timeout: 60_000 },
      )
      .toBe('mowing:1');

    // Delete it again.
    await row.getByTestId('zone-edit').click();
    await page.getByTestId('zone-delete').click();
    await page.getByTestId('zone-delete-confirm').click();
    await expect(page.getByTestId('zones-empty')).toBeVisible();
    await expect(map).toHaveAttribute('data-vzones', '0');
    csp.assertNone();
  } finally {
    await cleanUp(request, owner);
  }
});
