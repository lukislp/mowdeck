/**
 * Helpers of the Phase 2 gate: unique names per run, API setup where the UI is not under test,
 * UI login, and a guard that fails a test on any Content Security Policy violation.
 */
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

import { expect, type APIRequestContext, type Page } from '@playwright/test';

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
 * The suite runs it in the stack's identity container with `docker compose exec`, so it needs the
 * Docker CLI next to the stack: `MOWDECK_E2E_COMPOSE_FILE` names the Compose file (default: this
 * checkout's `infra/compose/docker-compose.yml`), `MOWDECK_E2E_PLATFORM_ADMIN=off` skips the specs
 * that need an operator admin (a stack without local Docker access).
 */
export const PLATFORM_ADMIN_ENABLED = process.env.MOWDECK_E2E_PLATFORM_ADMIN !== 'off';

const COMPOSE_FILE =
  process.env.MOWDECK_E2E_COMPOSE_FILE ??
  fileURLToPath(new URL('../../../infra/compose/docker-compose.yml', import.meta.url));

export function grantPlatformAdmin(who: Actor): void {
  const output = execFileSync(
    'docker',
    [
      'compose',
      '-f',
      COMPOSE_FILE,
      'exec',
      '-T',
      'identity',
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
