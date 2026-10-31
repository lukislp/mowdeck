/**
 * The public demo (ADR 0068), against a demo stack: infra/compose/docker-compose.demo.yml on top
 * of the test stack (CI job e2e-demo; locally `MOWDECK_E2E_DEMO=1 pnpm e2e:stack up`). On any
 * other stack (no `demo` block in /config.json) every test skips, so the regular shards pass it.
 *
 * Covers: "Try the demo" signs in with one click into the seeded account; registration and the
 * other sign-in methods are neither shown nor reachable; the banner and the "Demo" title label
 * are on every main page and cannot be dismissed; the API marks its responses and refuses what
 * the demo does not allow; the app locks those actions with one hint ("In der Demo nicht
 * verfügbar") and shows a refusal as one German sentence; the served HTML is labelled too.
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

test('the app locks what the demo refuses, with one hint', async ({ page }) => {
  await tryTheDemo(page);
  const first = page.locator('[data-testid^="mower-row-"]').first();
  await expect(first).toBeVisible({ timeout: 60_000 });
  const mowerId = ((await first.getAttribute('data-testid')) ?? '').replace('mower-row-', '');
  // No way into adding mowers (Fleet refuses it); the page itself is locked when typed in.
  await expect(page.getByTestId('add-mower')).toHaveCount(0);

  const hint = /In der Demo nicht verfügbar/;
  const locked = async (testid: string) => {
    const lock = page.getByTestId(testid);
    await expect(lock).toBeVisible();
    await expect(lock).toContainText(hint);
    // A disabled fieldset disables every control inside (Playwright judges controls, not it).
    await expect(lock.locator('fieldset').first()).toHaveAttribute('disabled', '');
    return lock;
  };

  await page.goto('/settings');
  await expectLabelled(page);
  const password = await locked('demo-lock-password');
  await expect(password.getByRole('button')).toBeDisabled();
  await locked('demo-lock-security');
  await expect(page.getByTestId('security-alerts-switch')).toBeDisabled();
  await locked('demo-lock-users');
  await expect(page.getByTestId('user-submit')).toBeDisabled();
  await locked('demo-lock-channels');
  await expect(page.getByTestId('notif-test')).toBeDisabled();
  // What works in the demo stays usable: the notification preferences.
  await expect(page.getByTestId('notif-save')).toBeEnabled();

  await page.goto('/mowers/new');
  await locked('demo-lock-mower-new');
  await expect(page.getByTestId('mower-submit')).toBeDisabled();

  await page.goto(`/mowers/${mowerId}/settings`);
  await locked('demo-lock-mower-settings');
  await expect(page.getByTestId('mower-save')).toBeDisabled();
  await locked('demo-lock-mower-delete');
  await expect(page.getByTestId('mower-delete')).toBeDisabled();
  await locked('demo-lock-placement');
  await expect(page.getByTestId('placement-move')).toBeDisabled();

  await page.goto('/fleet/base-stations');
  await expectLabelled(page);
  await expect(page.getByTestId('bs-add')).toHaveCount(0);
  await locked('demo-lock-bs-add');
  await expect(page.getByTestId('bs-add-empty')).toBeDisabled();
});

test('a base station in the demo can be looked at, not changed', async ({ page }) => {
  await tryTheDemo(page);
  const first = page.locator('[data-testid^="mower-row-"]').first();
  await expect(first).toBeVisible({ timeout: 60_000 });
  const mowerId = ((await first.getAttribute('data-testid')) ?? '').replace('mower-row-', '');
  // The demo has no base station and Fleet refuses registering one, so the list answers with a
  // simulated base station that one demo mower uses (as an older demo state would show it).
  const now = new Date().toISOString();
  await page.route('**/api/v1/fleet/base-stations', async (route) => {
    if (route.request().method() !== 'GET') return route.continue();
    await route.fulfill({
      json: {
        items: [
          {
            id: '00000000-0000-4000-8000-0000000000b5',
            name: 'RTK Garten',
            source: 'simulated',
            device_name: null,
            iot_id: null,
            model: null,
            linked_account_id: null,
            mower_ids: [mowerId],
            created_at: now,
            updated_at: now,
          },
        ],
      },
    });
  });

  await page.goto('/fleet/base-stations');
  await expectLabelled(page);
  const base = page.locator('li[data-testid^="bs-"][data-name="RTK Garten"]');
  await expect(base).toBeVisible();
  // Its state, mowers and firmware stay readable.
  await expect(base.getByTestId('bs-mowers')).toBeVisible();
  await expect(base.getByTestId('bs-firmware')).toBeVisible();
  // Rename and remove are entry points: hidden. Neither may a mower be taken off it.
  await expect(base.getByTestId('bs-rename')).toHaveCount(0);
  await expect(base.getByTestId('bs-delete')).toHaveCount(0);
  await expect(base.getByTestId('bs-unlink')).toHaveCount(0);

  const hint = /In der Demo nicht verfügbar/;
  const assign = base.getByTestId('demo-lock-bs-assign');
  await expect(assign).toContainText(hint);
  await expect(assign.locator('fieldset').first()).toHaveAttribute('disabled', '');
  await expect(base.getByTestId('bs-link')).toBeDisabled();
  const firmware = base.getByTestId('demo-lock-bs-fw');
  await expect(firmware).toContainText(hint);
  await expect(base.getByTestId('bs-fw-check')).toBeDisabled();
});

test('a refusal of the server is one sentence in the language of the app', async ({ page }) => {
  await tryTheDemo(page);
  await page.goto('/settings');
  const lock = page.getByTestId('demo-lock-password');
  const fieldset = lock.locator('fieldset').first();
  await expect(fieldset).toHaveAttribute('disabled', '');
  await expect(lock.getByRole('button')).toBeDisabled();
  // The lock is only the app's courtesy; Identity refuses the change itself. Lift it the way a
  // stale page or a script would, and send the form.
  await fieldset.evaluate((node) => node.removeAttribute('disabled'));
  await page.locator('#current-password').fill('a-long-password-1');
  await page.locator('#account-new-password').fill('another-long-password-2');
  const refused = page.waitForResponse(
    (response) =>
      response.url().endsWith('/api/v1/identity/me/password') && response.status() === 403,
  );
  await lock.getByRole('button').click();
  expect((await (await refused).json()).type).toBe(DEMO_PROBLEM);
  const notice = lock.getByRole('alert');
  await expect(notice).toContainText('In der Demo nicht verfügbar');
  // Only the German sentence: not the service's English reason under it.
  await expect(notice).not.toContainText(/showcase|cannot be changed here/i);
});

test('the served page carries the demo label without JavaScript', async ({ request }) => {
  for (const path of ['/', '/login', '/track']) {
    const html = await (await request.get(path)).text();
    expect(html).toContain('<title>Demo · MowDeck</title>');
    expect(html).toContain('<meta property="og:title" content="Demo · MowDeck" />');
    expect(html).toMatch(/<meta name="description" content="[^"]*Demo[^"]*"/);
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
