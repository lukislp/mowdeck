/**
 * Task 27, AI assist ("KI-Hilfe", desktop 1280 x 800) against the stack's rule based fake vision
 * provider (the stack runs with `VISION_ALLOW_FAKE_PROVIDER=true`, tests/e2e-web/stack.env):
 * (a) the owner opens the AI provider settings ("KI-Anbieter"), picks the fake family, saves the
 * model `fake/recover` and a test call answers ok; (b) a simulated mower that gets stuck after
 * 20 s of mowing (`stuck_after_seconds`, a development option of the simulator that Fleet passes
 * through) shows the error state, the AI assist sheet proposes the fake maneuver (back, turn,
 * forward), the owner approves it (blades off), the outcome is "recovered": the mower is free,
 * and like a LUBA it keeps the stuck error of its interrupted job until the job resumes; Fleet
 * lists the drive session with `origin: "vision"`. Stereo: the settings
 * page has "Stereo-Bilder, wenn verfügbar" switched on by default; with both front cameras of the
 * simulator side by side in the camera panel the request carries both frames, and the proposal
 * shows "Stereo" with the two images (left and right) and `stereo: true` in the API; Vision adds
 * its top-down map rendering as a third image ("Karte"). (c) Return to dock: with the cautious
 * fake model `fake/decline` ("recovery not recommended") a paused mower on its lawn still gets
 * the labelled dock fallback; approving it sends the normal dock command and no drive session.
 * (d) Resume: with `fake/resume` a mower stuck during a two-area job gets the proposal to continue
 * it (the job's areas, current and next, shown in the sheet); approving drives the short maneuver
 * with the blades off, then `resume_mow` goes out while the stuck error is still reported (a
 * manual drive does not clear it on a LUBA), the job continues, the error clears after a few
 * metres and the mower mows again. (f) No photo: an automatic analysis that got no camera photo
 * (weak Wi-Fi, -79 dBm; the stack cannot weaken a link, so the proposal is answered by a route)
 * opens from the notification's link with the reason and offers to start the analysis by hand,
 * which then runs like any request.
 */
import { expect, test, type APIRequestContext } from '@playwright/test';

import {
  PLATFORM_ADMIN_ENABLED,
  RUN,
  actor,
  commandByApi,
  fleetAuditCount,
  loginByUi,
  registerByApi,
  waitForMap,
  watchCsp,
  type Actor,
} from './support';
import { routeNoPhoto } from './vision-no-photo';

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
  await expect(sheet.getByTestId('assist-images').getByRole('img')).toHaveCount(3);
  await expect(sheet.getByTestId('assist-images')).toContainText('Front links');
  await expect(sheet.getByTestId('assist-images')).toContainText('Front rechts');
  await expect(sheet.getByTestId('assist-images')).toContainText('Karte (Fahrtrichtung oben)');
  const listed = await request.get(`/api/v1/vision/mowers/${mowerId}/proposals?limit=1`, {
    headers: auth,
  });
  const newest = (
    (await listed.json()) as {
      items: {
        stereo: boolean;
        image_count: number;
        frames: { side: string | null; source: string }[];
      }[];
    }
  ).items[0];
  expect(newest?.stereo).toBe(true);
  expect(newest?.image_count).toBe(3);
  expect(newest?.frames.map((f) => f.side)).toEqual(['left', 'right', null]);
  expect(newest?.frames.at(-1)?.source).toBe('map');
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
  // Free, but like a LUBA the stuck error of the interrupted job stays until it resumes.
  await expect(sheet.getByTestId('assist-detail')).toContainText('still reports stuck');

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

test('vision: the AI declines, the dock fallback is offered and sends the mower home', async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('vision-dock'));
  const auth = { Authorization: `Bearer ${owner.token}` };
  const saved = await request.put('/api/v1/vision/me/provider', {
    headers: auth,
    data: { model: 'fake/decline' },
  });
  expect(saved.status(), await saved.text()).toBe(200);
  const created = await request.post('/api/v1/fleet/mowers', {
    headers: auth,
    data: { name: `Luba dock ${RUN}`, connection_kind: 'simulated', connection_options: {} },
  });
  expect(created.status(), await created.text()).toBe(201);
  const mowerId = ((await created.json()) as { id: string }).id;
  await waitForMap(request, owner, mowerId);
  // Mow a few seconds, then pause on the lawn: paused, no error, like the owner's report.
  await commandByApi(request, owner, mowerId, { kind: 'start_mow', area_ids: ['front'] });
  await loginByUi(page, owner);
  await page.goto(`/mowers/${mowerId}`);
  await expect(page.getByTestId('mower-state')).toContainText('Mäht', { timeout: 60_000 });
  await page.waitForTimeout(4_000);
  await commandByApi(request, owner, mowerId, { kind: 'pause_mow' });
  await expect(page.getByTestId('mower-state')).toContainText('Pausiert', { timeout: 30_000 });

  await page.getByRole('button', { name: 'Ladestation mit KI-Hilfe finden' }).click();
  const sheet = page.getByRole('dialog', { name: 'KI-Hilfe: Ladestation finden' });
  await expect(sheet.getByTestId('assist-provider')).toContainText('fake/decline');
  await sheet.getByTestId('assist-start').click();
  await expect(sheet.getByTestId('assist-state')).toHaveAttribute('data-value', 'proposed', {
    timeout: 30_000,
  });
  const plan = sheet.getByTestId('assist-dock');
  await expect(plan).toHaveAttribute('data-fallback', 'true');
  await expect(plan).toContainText('Zurück zur Ladestation');
  await expect(sheet.getByTestId('assist-fallback')).toContainText('Rückfalloption');
  await expect(sheet.getByTestId('assist-fallback-reasons')).toContainText('safe route');
  await expect(sheet.getByTestId('assist-steps')).toHaveCount(0);
  await expect(sheet.getByTestId('assist-images')).toContainText('Karte (Fahrtrichtung oben)');
  const listed = await request.get(`/api/v1/vision/mowers/${mowerId}/proposals?limit=1`, {
    headers: auth,
  });
  const newest = (
    (await listed.json()) as {
      items: { action: string; fallback: boolean; steps: unknown[]; state: string }[];
    }
  ).items[0];
  expect(newest).toMatchObject({ action: 'return_to_dock', fallback: true, steps: [] });

  // Nothing moved so far; approving sends the normal dock command.
  await sheet.getByRole('button', { name: 'Freigeben und zur Ladestation schicken' }).click();
  await expect(sheet.getByTestId('assist-state')).toHaveAttribute('data-value', 'recovered', {
    timeout: 30_000,
  });
  await expect(sheet.getByTestId('assist-outcome')).toContainText(
    'Der Befehl zur Rückkehr wurde gesendet',
  );
  await expect(page.getByTestId('mower-state')).toContainText(/Fährt zurück|Angedockt|Lädt/, {
    timeout: 30_000,
  });
  const sessions = await request.get(`/api/v1/fleet/mowers/${mowerId}/drive-sessions`, {
    headers: auth,
  });
  const items = ((await sessions.json()) as { items: { origin?: string | null }[] }).items;
  expect(items.some((item) => item.origin === 'vision')).toBe(false);
  await sheet.getByTestId('assist-done').click();
  csp.assertNone();
});

test('vision: a mower stuck in a two-area job continues it after an approved resume', async ({
  page,
  request,
}) => {
  test.setTimeout(300_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('vision-resume'));
  const auth = { Authorization: `Bearer ${owner.token}` };
  const saved = await request.put('/api/v1/vision/me/provider', {
    headers: auth,
    data: { model: 'fake/resume' },
  });
  expect(saved.status(), await saved.text()).toBe(200);
  // Stuck after 20 s of mowing the front lawn; the back lawn is still to come.
  const mowerId = await createStuckMower(request, owner, `Luba resume ${RUN}`);
  await waitForMap(request, owner, mowerId);
  await commandByApi(request, owner, mowerId, { kind: 'start_mow', area_ids: ['front', 'back'] });
  await loginByUi(page, owner);
  await page.goto(`/mowers/${mowerId}`);
  await expect(page.getByTestId('mower-state')).toContainText('Fehler', { timeout: 60_000 });

  await page.getByRole('button', { name: 'Mit KI-Hilfe befreien' }).click();
  const sheet = page.getByRole('dialog', { name: 'KI-Hilfe' });
  await expect(sheet.getByTestId('assist-provider')).toContainText('fake/resume');
  await sheet.getByTestId('assist-start').click();
  await expect(sheet.getByTestId('assist-state')).toHaveAttribute('data-value', 'proposed', {
    timeout: 30_000,
  });
  await expect(sheet.getByTestId('assist-resume')).toContainText('Mähauftrag fortsetzen');
  await expect(sheet.getByTestId('assist-resume-plan')).toContainText('Erst die Fahrschritte');
  await expect(sheet.getByTestId('assist-resume-plan')).toContainText('Back lawn');
  const areas = sheet.getByTestId('assist-job-areas').getByRole('listitem');
  await expect(areas).toHaveCount(2);
  await expect(areas.nth(0)).toContainText('Front lawn');
  await expect(areas.nth(0)).toContainText('Aktuell');
  await expect(areas.nth(1)).toContainText('Back lawn');
  await expect(areas.nth(1)).toContainText('Als Nächstes');
  await expect(sheet.getByTestId('assist-steps').getByRole('listitem').first()).toContainText(
    'Zurück',
  );
  await expect(page.getByRole('button', { name: 'Not-Stopp' })).toBeVisible();
  const listed = await request.get(`/api/v1/vision/mowers/${mowerId}/proposals?limit=1`, {
    headers: auth,
  });
  const newest = (
    (await listed.json()) as {
      items: { action: string; job: { status: string; next_area: string | null } | null }[];
    }
  ).items[0];
  expect(newest?.action).toBe('resume');
  expect(newest?.job).toMatchObject({ status: 'paused_by_error', next_area: 'Back lawn' });

  // Approve: the short maneuver (blades off), then the job continues.
  await sheet.getByRole('button', { name: 'Freigeben und Auftrag fortsetzen' }).click();
  await expect(sheet.getByTestId('assist-state')).toHaveAttribute('data-value', 'recovered', {
    timeout: 45_000,
  });
  await expect(sheet.getByTestId('assist-outcome')).toContainText('Der Mähauftrag läuft weiter.');
  await expect(page.getByTestId('mower-state')).toContainText('Mäht', { timeout: 30_000 });
  const sessions = await request.get(`/api/v1/fleet/mowers/${mowerId}/drive-sessions`, {
    headers: auth,
  });
  const items = ((await sessions.json()) as { items: { origin?: string | null }[] }).items;
  expect(items.some((item) => item.origin === 'vision')).toBe(true);
  await sheet.getByTestId('assist-done').click();
  csp.assertNone();
});

test('vision: an automatic analysis without photo says why and starts again by hand', async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('vision-nophoto'));
  const auth = { Authorization: `Bearer ${owner.token}` };
  const saved = await request.put('/api/v1/vision/me/provider', {
    headers: auth,
    data: { model: 'fake/recover' },
  });
  expect(saved.status(), await saved.text()).toBe(200);
  const mowerId = await createStuckMower(request, owner, `Luba no photo ${RUN}`);
  await waitForMap(request, owner, mowerId);
  await commandByApi(request, owner, mowerId, { kind: 'start_mow', area_ids: ['front'] });
  await loginByUi(page, owner);
  // The notification's link to the failed automatic analysis (weak Wi-Fi, -79 dBm).
  const proposalId = '6f1e2d3c-4b5a-4968-8776-655443322110';
  await routeNoPhoto(page, mowerId, proposalId);
  await page.goto(`/mowers/${mowerId}?assist=${proposalId}`);
  const sheet = page.getByRole('dialog', { name: 'KI-Hilfe' });
  const note = sheet.getByTestId('assist-photo-problem');
  await expect(note).toContainText('Kein Kamerabild', { timeout: 30_000 });
  await expect(note).toContainText('Das WLAN des Mähers war zu schwach (-79 dBm)');
  await expect(note).toContainText('Starte die KI-Hilfe von Hand');
  await expect(sheet.getByTestId('assist-state')).toHaveAttribute('data-value', 'failed');

  // By hand once the mower reports the error: a fresh request with the mower's photo.
  await expect(page.getByTestId('mower-state')).toContainText('Fehler', { timeout: 60_000 });
  await sheet.getByTestId('assist-start-by-hand').click();
  await expect(sheet.getByTestId('assist-start')).toBeVisible();
  await sheet.getByTestId('assist-start').click();
  await expect(sheet.getByTestId('assist-state')).toHaveAttribute('data-value', 'proposed', {
    timeout: 30_000,
  });
  await expect(sheet.getByTestId('assist-photo-problem')).toHaveCount(0);
  await sheet.getByTestId('assist-reject').click();
  csp.assertNone();
});

/** Width and height of a baseline or progressive JPEG (its SOF segment). */
function jpegSize(data: Buffer): { width: number; height: number } | null {
  let at = 2;
  while (at + 9 < data.length) {
    if (data[at] !== 0xff) return null;
    const marker = data[at + 1]!;
    const length = data.readUInt16BE(at + 2);
    if (marker >= 0xc0 && marker <= 0xc3) {
      return { height: data.readUInt16BE(at + 5), width: data.readUInt16BE(at + 7) };
    }
    at += 2 + length;
  }
  return null;
}

/**
 * AI assist asks the mower's gateway for a photo; returns the side and size of every camera image
 * the model saw (both front cameras of the same moment when the photo comes through the relay;
 * the map rendering that follows them is checked, not returned).
 */
async function assistPhotos(
  request: APIRequestContext,
  owner: Actor,
  mowerId: string,
): Promise<{
  stereo: boolean;
  images: { side: string | null; size: ReturnType<typeof jpegSize> }[];
}> {
  const auth = { Authorization: `Bearer ${owner.token}` };
  const started = await request.post(`/api/v1/vision/mowers/${mowerId}/recover`, {
    headers: auth,
    data: { task: 'recover' },
  });
  expect(started.status(), await started.text()).toBe(202);
  const proposal = (await started.json()) as {
    id: string;
    stereo: boolean;
    image_count: number;
    frames: { source: string; side: string | null }[];
  };
  // The camera frames, then the map rendering as the last image (#46).
  expect(proposal.image_count).toBe(proposal.frames.length);
  expect(proposal.frames.at(-1)?.source).toBe('map');
  const cameras = proposal.frames.slice(0, -1);
  expect(cameras.every((f) => f.source === 'mower')).toBe(true);
  const images = [];
  for (const [index, frame] of cameras.entries()) {
    const image = await request.get(`/api/v1/vision/proposals/${proposal.id}/images/${index}`, {
      headers: auth,
    });
    expect(image.status()).toBe(200);
    expect(image.headers()['content-type']).toContain('image/jpeg');
    images.push({ side: frame.side, size: jpegSize(await image.body()) });
  }
  return { stereo: proposal.stereo, images };
}

/**
 * Vision through the camera relay (ADR 0045): a simulated mower with `photos: false` takes no
 * stills of its own (like the Mammotion cloud connection), so the Device-Gateway asks the relay's
 * frame tap (the test stack sets GATEWAY_RELAY_URL and RELAY_FRAMES_TOKEN, tests/e2e-web/stack.env).
 * The model sees both front cameras of the simulator's `fake` source as one stereo pair (the
 * relay's frame set: two 640x480 frames of the same moment, left first), the relay's lifecycle
 * events reach Fleet's audit as a stream started (and, after the 15 s grace,
 * stopped) for a mower nobody watched. A normal simulated mower keeps its own 480x360 still and
 * never starts the relay.
 */
test('vision: a mower without own photos gets both front cameras from the camera relay', async ({
  request,
}) => {
  test.skip(!PLATFORM_ADMIN_ENABLED, 'reads the audit in the stack (docker exec)');
  test.setTimeout(180_000);
  const owner = await registerByApi(request, actor('vision-relay'));
  const auth = { Authorization: `Bearer ${owner.token}` };
  const saved = await request.put('/api/v1/vision/me/provider', {
    headers: auth,
    data: { model: 'fake/recover' },
  });
  expect(saved.status(), await saved.text()).toBe(200);

  const create = async (name: string, options: Record<string, unknown>): Promise<string> => {
    const response = await request.post('/api/v1/fleet/mowers', {
      headers: auth,
      data: { name, connection_kind: 'simulated', connection_options: options },
    });
    expect(response.status(), await response.text()).toBe(201);
    const id = ((await response.json()) as { id: string }).id;
    await waitForMap(request, owner, id);
    return id;
  };

  // The simulator's own still: unchanged, and the relay is never asked.
  const own = await create(`Luba own photo ${RUN}`, {});
  expect(await assistPhotos(request, owner, own)).toEqual({
    stereo: false,
    images: [{ side: null, size: { width: 480, height: 360 } }],
  });
  expect(fleetAuditCount(own, 'mower.camera_stream_started')).toBe(0);

  // Without own photos: both front cameras of the relay as a stereo pair.
  const relayed = await create(`Luba relay photo ${RUN}`, { photos: false });
  expect(fleetAuditCount(relayed, 'mower.camera_stream_started')).toBe(0);
  expect(await assistPhotos(request, owner, relayed)).toEqual({
    stereo: true,
    images: [
      { side: 'left', size: { width: 640, height: 480 } },
      { side: 'right', size: { width: 640, height: 480 } },
    ],
  });
  await expect
    .poll(() => fleetAuditCount(relayed, 'mower.camera_stream_started'), { timeout: 20_000 })
    .toBe(1);
  // The vision-only session ends after the grace (15 s) and is released.
  await expect
    .poll(() => fleetAuditCount(relayed, 'mower.camera_stream_stopped'), { timeout: 45_000 })
    .toBe(1);
});
