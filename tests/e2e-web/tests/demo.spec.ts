/**
 * The public demo (ADR 0065), against a demo stack: infra/compose/docker-compose.demo.yml on top
 * of the test stack (CI job e2e-demo; locally `MOWDECK_E2E_DEMO=1 pnpm e2e:stack up`). On any
 * other stack (no `demo` block in /config.json) every test skips, so the regular shards pass it.
 *
 * Covers: "Try the demo" signs in with one click into the seeded account; registration and the
 * other sign-in methods are neither shown nor reachable; the banner and the "Demo" title label
 * are on every main page and cannot be dismissed; the API marks its responses and refuses what
 * the demo does not allow.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const DEMO_PROBLEM = 'urn:mowdeck:problem:demo-mode';

async function demoStack(request: APIRequestContext): Promise<boolean> {
  const response = await request.get('/config.json');
  if (!response.ok()) return false;
  const config = (await response.json()) as Record<string, unknown>;
  return typeof config.demo === 'object' && config.demo !== null;
}

test.beforeEach(async ({ request }) => {
  test.skip(!(await demoStack(request)), 'not a demo stack (docker-compose.demo.yml)');
});

async function expectLabelled(page: Page): Promise<void> {
  const band = page.getByTestId('demo-banner');
  await expect(band).toBeVisible();
  await expect(band.locator('button')).toHaveCount(0);
  await expect(page).toHaveTitle(/^Demo · /);
}

async function tryTheDemo(page: Page): Promise<void> {
  await page.goto('/login');
  await page.getByTestId('login-demo-start').click();
  await expect(page).toHaveURL(/\/mowers$/);
}

test('"Try the demo" signs in with one click into the seeded account', async ({ page }) => {
  await page.goto('/login');
  await expectLabelled(page);
  await expect(page.getByTestId('login-demo-badge')).toBeVisible();
  // Only the demo entry: no password form, no other sign-in method, no registration link.
  await expect(page.getByTestId('login-submit')).toHaveCount(0);
  await expect(page.getByTestId('login-passkey')).toHaveCount(0);
  await expect(page.getByTestId('login-github')).toHaveCount(0);
  await expect(page.locator('a[href="/register"]')).toHaveCount(0);

  await page.getByTestId('login-demo-start').click();
  await expect(page).toHaveURL(/\/mowers$/);
  await expectLabelled(page);
  await expect(page.locator('[data-testid^="mower-row-"]')).toHaveCount(3, { timeout: 60_000 });
});

test('registration is not reachable, neither in the app nor in the API', async ({
  page,
  request,
}) => {
  await page.goto('/register');
  await expectLabelled(page);
  await expect(page.getByTestId('register-demo-closed')).toBeVisible();
  await expect(page.getByTestId('register-submit')).toHaveCount(0);

  const register = await request.post('/api/v1/identity/register', {
    data: { username: 'visitor', password: 'a-long-password-1', account_name: 'Mine' },
  });
  expect(register.status()).toBe(403);
  expect(register.headers()['x-mowdeck-demo']).toBe('1');
  expect((await register.json()).type).toBe(DEMO_PROBLEM);

  const password = await request.post('/api/v1/identity/session', {
    data: { username: 'demo', password: 'a-long-password-1' },
  });
  expect(password.status()).toBe(403);

  // The seed secret header never passes the gateway: a guessed one changes nothing.
  const seed = await request.post('/api/v1/identity/demo/seed', {
    headers: { 'X-MowDeck-Demo-Seed': 'guess' },
  });
  expect(seed.status()).toBe(403);
});

test('the banner and the title label stay on every main page', async ({ page }) => {
  await tryTheDemo(page);
  const first = page.locator('[data-testid^="mower-row-"]').first();
  await expect(first).toBeVisible({ timeout: 60_000 });
  const testid = (await first.getAttribute('data-testid')) ?? '';
  const mowerId = testid.replace('mower-row-', '');
  const pages = [
    '/mowers',
    `/mowers/${mowerId}`,
    `/mowers/${mowerId}/health`,
    `/mowers/${mowerId}/map`,
    '/fleet',
    '/schedules',
    '/notifications',
    '/reports',
    '/settings',
  ];
  for (const path of pages) {
    await page.goto(path);
    await expectLabelled(page);
  }
});

test('the API marks every response and refuses what the demo does not allow', async ({ page }) => {
  await tryTheDemo(page);
  const result = await page.evaluate(async () => {
    // The access token lives in the app's memory; a refresh gives the page's own copy.
    const refreshed = await fetch('/api/v1/identity/session/refresh', {
      method: 'POST',
      headers: { 'X-Requested-With': 'mowdeck-web' },
      credentials: 'same-origin',
    });
    const { access_token: token } = (await refreshed.json()) as { access_token: string };
    const auth = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' };
    const call = async (method: string, path: string, body?: unknown) => {
      const response = await fetch(path, {
        method,
        headers: auth,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const text = await response.text();
      return {
        status: response.status,
        demo: response.headers.get('x-mowdeck-demo'),
        type: text.startsWith('{') ? (JSON.parse(text) as { type?: string }).type : undefined,
      };
    };
    return {
      mowers: await call('GET', '/api/v1/fleet/mowers'),
      token: await call('POST', '/api/v1/identity/me/api-tokens', { name: 'x', scopes: [] }),
      link: await call('POST', '/api/v1/fleet/linked-accounts', {
        vendor: 'mammotion',
        username: 'me@example.org',
        password: 'x'.repeat(12),
      }),
      ntfy: await call('PUT', '/api/v1/notifications/ntfy', {
        server: 'https://ntfy.sh',
        topic: 'x',
      }),
    };
  });
  expect(result.mowers).toMatchObject({ status: 200, demo: '1' });
  for (const refused of [result.token, result.link, result.ntfy]) {
    expect(refused).toMatchObject({ status: 403, demo: '1', type: DEMO_PROBLEM });
  }
});
