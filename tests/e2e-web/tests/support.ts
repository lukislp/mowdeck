/**
 * Helpers of the Phase 2 gate: unique names per run, API setup where the UI is not under test,
 * UI login, and a guard that fails a test on any Content Security Policy violation.
 */
import { execFileSync } from 'node:child_process';

import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test';

export const RUN = process.env.MOWDECK_E2E_RUN_ID ?? Date.now().toString(36).slice(-6);
export const PASSWORD = 'gate-password-' + RUN;
const IDENTITY = '/api/v1/identity';

export interface Actor {
  username: string;
  password: string;
  accountId?: string;
  token?: string;
}

export function actor(name: string): Actor {
  return { username: `${name}-${RUN}`, password: PASSWORD };
}

/** Register an account through the API and log in (token for further API setup). */
export async function registerByApi(request: APIRequestContext, who: Actor): Promise<Actor> {
  const registered = await request.post(`${IDENTITY}/register`, {
    data: { username: who.username, password: who.password, account_name: `${who.username} home` },
  });
  expect(registered.status(), await registered.text()).toBe(201);
  return loginByApi(request, who);
}

export async function loginByApi(request: APIRequestContext, who: Actor): Promise<Actor> {
  const login = await request.post(`${IDENTITY}/session`, {
    data: { username: who.username, password: who.password },
  });
  expect(login.status(), await login.text()).toBe(200);
  const body = (await login.json()) as { access_token: string; account_id: string };
  return { ...who, token: body.access_token, accountId: body.account_id };
}

export async function createMowerByApi(
  request: APIRequestContext,
  owner: Actor,
  name: string,
): Promise<string> {
  const response = await request.post('/api/v1/fleet/mowers', {
    headers: { Authorization: `Bearer ${owner.token}` },
    data: { name, connection_kind: 'simulated' },
  });
  expect(response.status(), await response.text()).toBe(201);
  return ((await response.json()) as { id: string }).id;
}

export async function commandByApi(
  request: APIRequestContext,
  owner: Actor,
  mowerId: string,
  command: Record<string, unknown>,
): Promise<void> {
  const response = await request.post(`/api/v1/fleet/mowers/${mowerId}/commands`, {
    headers: { Authorization: `Bearer ${owner.token}` },
    data: { command },
  });
  expect(response.status(), await response.text()).toBe(202);
}

/** Wait until Telemetry has the map of the mower (the gateway publishes it after connecting). */
export async function waitForMap(
  request: APIRequestContext,
  owner: Actor,
  mowerId: string,
): Promise<void> {
  await expect
    .poll(
      async () =>
        (
          await request.get(`/api/v1/telemetry/mowers/${mowerId}/map`, {
            headers: { Authorization: `Bearer ${owner.token}` },
          })
        ).status(),
      { timeout: 60_000, intervals: [1_000] },
    )
    .toBe(200);
}

/**
 * Operator admins exist only through Identity's CLI (`grant-platform-admin`, a tuple in OpenFGA).
 * The suite runs it in the stack's identity container with `docker exec`, so it needs the Docker
 * CLI next to the stack. The container is looked up by its Compose labels in the project
 * `MOWDECK_E2E_COMPOSE_PROJECT`, which the global setup takes from the stack's e2e marker (so the
 * grant always lands in the stack the browser tests, never in another stack on the same host).
 * `MOWDECK_E2E_PLATFORM_ADMIN=off` skips the specs that need an operator admin (a stack without
 * local Docker access).
 */
export const PLATFORM_ADMIN_ENABLED = process.env.MOWDECK_E2E_PLATFORM_ADMIN !== 'off';

/** The running container of a Compose service in the stack under test. */
export function serviceContainer(service: string): string {
  const project = process.env.MOWDECK_E2E_COMPOSE_PROJECT;
  if (!project) {
    throw new Error(
      'MOWDECK_E2E_COMPOSE_PROJECT is not set: name the Compose project of the stack under test ' +
        'or set MOWDECK_E2E_PLATFORM_ADMIN=off',
    );
  }
  const ids = execFileSync(
    'docker',
    [
      'ps',
      '-q',
      '--filter',
      `label=com.docker.compose.project=${project}`,
      '--filter',
      `label=com.docker.compose.service=${service}`,
    ],
    { encoding: 'utf-8', timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe'] },
  )
    .split(/\s+/)
    .filter(Boolean);
  if (ids.length === 0) {
    throw new Error(`no running ${service} container in the Compose project ${project}`);
  }
  return ids[0]!;
}

function identityContainer(): string {
  return serviceContainer('identity');
}

export function grantPlatformAdmin(who: Actor): void {
  const output = execFileSync(
    'docker',
    [
      'exec',
      identityContainer(),
      'python',
      '-m',
      'identity.cli',
      'grant-platform-admin',
      who.username,
    ],
    { encoding: 'utf-8', timeout: 60_000, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  expect(output).toContain('operator admin');
}

export async function loginByUi(page: Page, who: Actor): Promise<void> {
  await page.goto('/login');
  await page.locator('#username').fill(who.username);
  await page.locator('#password').fill(who.password);
  await page.getByTestId('login-submit').click();
  await page.waitForURL(/\/mowers/);
}

/** Collect CSP violations reported on the console; `assertNone` fails the test with them. */
export function watchCsp(page: Page): { assertNone(): void } {
  const violations: string[] = [];
  page.on('console', (message) => {
    const text = message.text();
    if (/content security policy|Refused to (load|apply|execute|connect)/i.test(text)) {
      violations.push(text);
    }
  });
  return {
    assertNone() {
      expect(violations, violations.join('\n')).toEqual([]);
    },
  };
}

// --- spot mowing (spot.spec.ts, spot-phone.spec.ts) ------------------------------------------

/** The capabilities a spot test looks at. */
export interface SpotCaps {
  manual_drive?: boolean;
  manual_drive_blades?: boolean;
}

export async function capabilities(
  request: APIRequestContext,
  who: Actor,
  mowerId: string,
): Promise<SpotCaps | null> {
  const response = await request.get(`/api/v1/telemetry/mowers/${mowerId}/capabilities`, {
    headers: { Authorization: `Bearer ${who.token}` },
  });
  return response.ok()
    ? ((await response.json()) as { capabilities: SpotCaps }).capabilities
    : null;
}

/**
 * Send `spot_stop` and wait for its outcome: false when Fleet refuses the kind or the gateway
 * answers that the connection has no spot runs.
 */
export async function spotRunsSupported(
  request: APIRequestContext,
  who: Actor,
  mowerId: string,
): Promise<{ supported: boolean; why: string }> {
  const auth = { Authorization: `Bearer ${who.token}` };
  const sent = await request.post(`/api/v1/fleet/mowers/${mowerId}/commands`, {
    headers: auth,
    data: { command: { kind: 'spot_stop' } },
  });
  if (sent.status() !== 202) return { supported: false, why: `Fleet answered ${sent.status()}` };
  const { command_id: commandId } = (await sent.json()) as { command_id: string };
  let record: { status: string; reason?: string | null } = { status: 'pending' };
  await expect
    .poll(
      async () => {
        const r = await request.get(`/api/v1/fleet/commands/${commandId}`, { headers: auth });
        if (r.ok()) record = (await r.json()) as typeof record;
        return record.status;
      },
      { timeout: 30_000, intervals: [500] },
    )
    .not.toBe('pending');
  const reason = record.reason ?? '';
  if (record.status === 'rejected' && /unsupported|not supported|unknown/i.test(reason)) {
    return { supported: false, why: `gateway: ${reason}` };
  }
  return { supported: true, why: `${record.status} ${reason}`.trim() };
}

// --- camera relay (camera-relay-web.spec.ts) -------------------------------------------------

/** Force the camera transport of this browser (the per-browser camera option) on every page. */
export async function forceCameraTransport(
  page: Page,
  transport: 'auto' | 'relay' | 'direct',
): Promise<void> {
  await page.addInitScript((value) => {
    try {
      const key = 'mowdeck.cameraOptions';
      const stored = JSON.parse(localStorage.getItem(key) ?? '{}') as Record<string, unknown>;
      localStorage.setItem(key, JSON.stringify({ ...stored, transport: value }));
    } catch {
      // no storage: the default (auto) applies
    }
  }, transport);
}

/** Skip in a browser without WebRTC (never fake a picture there). */
export async function requireWebRtc(page: Page, browserName: string): Promise<void> {
  await page.goto('/login');
  const available = await page.evaluate(() => typeof RTCPeerConnection === 'function');
  test.skip(
    !available,
    `${browserName} in this Playwright build has no WebRTC (RTCPeerConnection is missing); ` +
      'the relay player cannot run here',
  );
  // The relay sends H.264 only (ADR 0045). A browser without an H.264 decoder offers none and
  // MediaMTX refuses the offer ("codecs not supported by client"): fail with the reason instead
  // of a timeout. Playwright's Firefox on Linux has no OpenH264 unless MOZ_GMP_PATH points at
  // the plugin (CI installs it; see tests/e2e-web/README.md).
  const h264 = await page.evaluate(
    () =>
      RTCRtpReceiver.getCapabilities?.('video')?.codecs.some(
        (codec) => codec.mimeType.toLowerCase() === 'video/h264',
      ) ?? false,
  );
  expect(
    h264,
    `${browserName} offers no H.264 decoder for WebRTC; the relay sends H.264 only ` +
      '(Firefox on Linux: install the OpenH264 plugin and set MOZ_GMP_PATH)',
  ).toBe(true);
}

/** The `<video>` presents new frames (requestVideoFrameCallback, else decoded frames grow). */
export async function presentsFrames(video: Locator): Promise<number> {
  return video.evaluate(async (node) => {
    const element = node as HTMLVideoElement;
    if (typeof element.requestVideoFrameCallback !== 'function') {
      const before = element.getVideoPlaybackQuality?.().totalVideoFrames ?? 0;
      await new Promise((resolve) => setTimeout(resolve, 1_500));
      return (element.getVideoPlaybackQuality?.().totalVideoFrames ?? 0) - before;
    }
    let count = 0;
    await new Promise<void>((resolve) => {
      const tick = (): void => {
        count += 1;
        if (count >= 3) resolve();
        else element.requestVideoFrameCallback(tick);
      };
      element.requestVideoFrameCallback(tick);
      setTimeout(resolve, 4_000);
    });
    return count;
  });
}

/** A coarse fingerprint of the current video frame (every 997th pixel of a small copy). */
export async function videoFingerprint(
  video: Locator,
): Promise<{ colours: number; print: string }> {
  return video.evaluate((node) => {
    const element = node as HTMLVideoElement;
    const canvas = document.createElement('canvas');
    canvas.width = 160;
    canvas.height = 120;
    const ctx = canvas.getContext('2d')!;
    ctx.drawImage(element, 0, 0, canvas.width, canvas.height);
    const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    const colours = new Set<string>();
    const parts: string[] = [];
    for (let i = 0; i < data.length; i += 4 * 97) {
      colours.add(`${data[i]! >> 4},${data[i + 1]! >> 4},${data[i + 2]! >> 4}`);
      parts.push(`${data[i]! >> 5},${data[i + 1]! >> 5}`);
    }
    return { colours: colours.size, print: parts.join(';') };
  });
}

/** The stage plays the relay: provider and transport `relay`, live, real frames, not blank. */
export async function expectRelayLive(page: Page, testid = 'camera-video'): Promise<void> {
  const stage = page.getByTestId('camera-stage');
  await expect(stage).toHaveAttribute('data-provider', 'relay', { timeout: 30_000 });
  await expect(stage).toHaveAttribute('data-transport', 'relay');
  await expect(stage).toHaveAttribute('data-phase', 'live', { timeout: 30_000 });
  const video = page.getByTestId(testid);
  await expect(video).toBeVisible();
  await expect(video).toHaveAttribute('data-camera-video', '');
  await expect.poll(() => presentsFrames(video), { timeout: 15_000 }).toBeGreaterThanOrEqual(3);
  // Not a blank picture: the pattern's bars arrive.
  await expect
    .poll(async () => (await videoFingerprint(video)).colours, { timeout: 10_000 })
    .toBeGreaterThan(3);
}

/**
 * Pause or resume the relay's fake camera sources (test hook of the test stack,
 * `POST /internal/v1/test/freeze` inside the camera-relay container, never routed outside).
 */
export function freezeRelaySources(frozen: boolean): number {
  const script =
    'import json, urllib.request as u; ' +
    `r = u.Request("http://127.0.0.1:8000/internal/v1/test/freeze", data=json.dumps({"frozen": ${frozen ? 'True' : 'False'}}).encode(), headers={"Content-Type": "application/json"}, method="POST"); ` +
    'print(json.load(u.urlopen(r, timeout=10))["changed"])';
  const output = execFileSync(
    'docker',
    ['exec', serviceContainer('camera-relay'), 'python', '-c', script],
    { encoding: 'utf-8', timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  return Number(output.trim());
}

/** Fleet's audit rows of a mower with `action` (read in the stack's PostgreSQL). */
export function fleetAuditCount(mowerId: string, action: string): number {
  if (!/^[0-9a-f-]{36}$/.test(mowerId) || !/^[a-z._]+$/.test(action)) {
    throw new Error('unexpected mower id or action');
  }
  const output = execFileSync(
    'docker',
    [
      'exec',
      serviceContainer('postgres'),
      'psql',
      '-U',
      'postgres',
      '-d',
      'fleet',
      '-tAc',
      `select count(*) from audit_log where target_id = '${mowerId}' and action = '${action}'`,
    ],
    { encoding: 'utf-8', timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  return Number(output.trim());
}

/** Pretend the page went hidden or visible again (`visibilitychange` with the new state). */
export async function setPageVisibility(page: Page, visible: boolean): Promise<void> {
  await page.evaluate((shown) => {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => (shown ? 'visible' : 'hidden'),
    });
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => !shown });
    document.dispatchEvent(new Event('visibilitychange'));
  }, visible);
}

/** Create a sub-user with the account's `viewer` role (a second user of the same account). */
export async function createAccountViewer(
  request: APIRequestContext,
  owner: Actor,
  name: string,
): Promise<Actor> {
  const auth = { Authorization: `Bearer ${owner.token}` };
  const roles = (await (
    await request.get(`/api/v1/identity/accounts/${owner.accountId}/roles`, { headers: auth })
  ).json()) as { id: string; key: string | null }[];
  const viewerRole = roles.find((role) => role.key === 'viewer');
  expect(viewerRole, 'viewer role').toBeDefined();
  const viewer = actor(name);
  const created = await request.post(`/api/v1/identity/accounts/${owner.accountId}/users`, {
    headers: auth,
    data: {
      username: viewer.username,
      password: viewer.password,
      assignments: [{ role_id: viewerRole!.id, level: { type: 'account', id: owner.accountId } }],
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  return loginByApi(request, viewer);
}
