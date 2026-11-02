/**
 * Demo sandboxes (ADR 0076) in the public demo (ADR 0068), against a demo stack with sandboxes
 * (CI job e2e-demo; locally `MOWDECK_E2E_DEMO=1 pnpm e2e:stack up`). Runs in the projects `demo`
 * and `demo-phone`; on any other stack, and on a demo with the shared user, every test skips.
 *
 * Covers: "Try the demo" gives every browser an own account with own simulated mowers (Clover,
 * Juniper, Pebble) that no other visitor can read, command or subscribe to, and a mow in one
 * sandbox shows nowhere else; the banner and the sign-in page say so; a logout ends the sandbox
 * session and the sign-in page then waits for the visitor; a browser whose sandbox is gone gets a
 * fresh one by itself; a full demo counts down and starts the sandbox by itself, and too many
 * sandboxes from one address say how long to wait.
 *
 * The suite runs from one address and starts many sandboxes, so tests/e2e-web/demo.env lifts the
 * caps (DEMO_SANDBOX_MAX, DEMO_SANDBOX_PER_ADDRESS_PER_HOUR). The real caps are covered by
 * Identity's tests; the full and too-many answers here come from `page.route`.
 */
import {
  expect,
  test,
  type APIRequestContext,
  type Browser,
  type BrowserContext,
  type Page,
} from '@playwright/test';

const IDENTITY = '/api/v1/identity';
const DEMO_SESSION = `${IDENTITY}/demo/session`;
const VISITOR_KEY = 'mowdeck.demo.visitor';
const MOWER_NAMES = ['Clover', 'Juniper', 'Pebble'];

interface DemoConfigBlock {
  resetHours?: number;
  sandbox?: { idleMinutes?: number };
}

async function sandboxDemo(request: APIRequestContext): Promise<DemoConfigBlock | null> {
  const response = await request.get('/config.json');
  if (!response.ok()) return null;
  const config = (await response.json()) as { demo?: DemoConfigBlock | null };
  return config.demo && typeof config.demo.sandbox === 'object' ? config.demo : null;
}

test.beforeEach(async ({ request }) => {
  test.skip(!(await sandboxDemo(request)), 'not a demo stack with sandboxes');
});

interface Visitor {
  page: Page;
  token: string;
  accountId: string;
  mowers: { id: string; name: string }[];
}

const bearer = (token: string) => ({ Authorization: `Bearer ${token}` });

/** The access token of the next demo session this page starts (the app keeps it in memory). */
function nextDemoToken(page: Page): Promise<string> {
  return page
    .waitForResponse(
      (response) =>
        new URL(response.url()).pathname === DEMO_SESSION &&
        response.request().method() === 'POST' &&
        response.status() === 200,
      { timeout: 60_000 },
    )
    .then(async (response) => ((await response.json()) as { access_token: string }).access_token);
}

/** Who a token belongs to, and the mowers it sees. */
async function describeVisitor(
  request: APIRequestContext,
  page: Page,
  token: string,
): Promise<Visitor> {
  const me = await request.get(`${IDENTITY}/me`, { headers: bearer(token) });
  expect(me.status(), await me.text()).toBe(200);
  const accountId = ((await me.json()) as { account_id: string }).account_id;
  const list = await request.get('/api/v1/fleet/mowers', { headers: bearer(token) });
  expect(list.status(), await list.text()).toBe(200);
  const mowers = ((await list.json()) as { items: { id: string; name: string }[] }).items.map(
    ({ id, name }) => ({ id, name }),
  );
  return { page, token, accountId, mowers };
}

/** "Try the demo" on the sign-in page; the visitor with its sandbox. */
async function tryTheDemo(page: Page, request: APIRequestContext): Promise<Visitor> {
  await page.goto('/login');
  const token = nextDemoToken(page);
  await page.getByTestId('login-demo-start').click();
  await expect(page).toHaveURL(/\/mowers$/, { timeout: 60_000 });
  await expect(page.locator('[data-testid^="mower-row-"]')).toHaveCount(3, { timeout: 60_000 });
  return describeVisitor(request, page, await token);
}

async function newVisitor(
  browser: Browser,
  request: APIRequestContext,
): Promise<{ context: BrowserContext; visitor: Visitor }> {
  const context = await browser.newContext();
  const visitor = await tryTheDemo(await context.newPage(), request);
  return { context, visitor };
}

const names = (visitor: Visitor) => visitor.mowers.map((mower) => mower.name).sort();

/** Area ids of a mower's map (the gateway publishes it once the simulated mower connects). */
async function areaIds(request: APIRequestContext, token: string, mowerId: string) {
  let ids: string[] = [];
  await expect
    .poll(
      async () => {
        const response = await request.get(`/api/v1/telemetry/mowers/${mowerId}/map`, {
          headers: bearer(token),
        });
        if (!response.ok()) return 0;
        const body = (await response.json()) as {
          areas?: { id: string }[];
          map?: { areas?: { id: string }[] };
        };
        ids = (body.areas ?? body.map?.areas ?? []).map((area) => area.id);
        return ids.length;
      },
      { timeout: 60_000, intervals: [1_000] },
    )
    .toBeGreaterThan(0);
  return ids;
}

async function activityOf(request: APIRequestContext, token: string, mowerId: string) {
  const response = await request.get(`/api/v1/telemetry/mowers/${mowerId}/state`, {
    headers: bearer(token),
  });
  return response.ok()
    ? ((await response.json()) as { state: { activity: string } }).state.activity
    : `http-${response.status()}`;
}

/** Open the app's realtime socket with `token` and subscribe to `mowerIds`; what it answers. */
function subscribeOverRealtime(
  page: Page,
  token: string,
  mowerIds: string[],
): Promise<{ subscribed: string[]; denied: string[]; states: string[] }> {
  return page.evaluate(
    ({ token, mowerIds }) =>
      new Promise<{ subscribed: string[]; denied: string[]; states: string[] }>(
        (resolve, reject) => {
          const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
          const socket = new WebSocket(`${scheme}://${location.host}/api/v1/realtime/ws`);
          const states: string[] = [];
          const timer = setTimeout(() => {
            socket.close();
            reject(new Error('no answer from realtime'));
          }, 20_000);
          socket.onopen = () => socket.send(JSON.stringify({ type: 'auth', token }));
          socket.onmessage = (event) => {
            const message = JSON.parse(String(event.data)) as {
              type: string;
              mower_id?: string;
              mower_ids?: string[];
              denied?: string[];
            };
            if (message.type === 'ready') {
              socket.send(
                JSON.stringify({ type: 'subscribe', mower_ids: mowerIds, request_id: 'e2e-1' }),
              );
            } else if (message.type === 'state' && message.mower_id) {
              states.push(String(message.mower_id));
            } else if (message.type === 'subscribed') {
              // A moment longer: a state of a denied mower must not arrive either.
              setTimeout(() => {
                clearTimeout(timer);
                socket.close();
                resolve({
                  subscribed: (message.mower_ids ?? []).map(String),
                  denied: (message.denied ?? []).map(String),
                  states,
                });
              }, 2_000);
            }
          };
          socket.onerror = () => undefined;
        },
      ),
    { token, mowerIds },
  );
}

test('every visitor gets an own sandbox that nobody else can see or control', async ({
  browser,
  request,
}) => {
  const first = await newVisitor(browser, request);
  const second = await newVisitor(browser, request);
  const a = first.visitor;
  const b = second.visitor;
  try {
    // Two accounts, each with its own three simulated mowers.
    expect(a.accountId).not.toBe(b.accountId);
    expect(names(a)).toEqual(MOWER_NAMES);
    expect(names(b)).toEqual(MOWER_NAMES);
    const aIds = a.mowers.map((mower) => mower.id);
    const bIds = b.mowers.map((mower) => mower.id);
    expect(aIds.filter((id) => bIds.includes(id))).toEqual([]);
    for (const id of aIds) await expect(b.page.getByTestId(`mower-row-${id}`)).toHaveCount(0);
    for (const id of bIds) await expect(a.page.getByTestId(`mower-row-${id}`)).toHaveCount(0);

    // The band says the sandbox is the visitor's own.
    const band = a.page.getByTestId('demo-banner');
    await expect(band).toHaveAttribute('data-mode', 'sandbox');
    await expect(band).toContainText('Sandbox');
    await expect(band).not.toContainText(/geteilt/);

    // B can neither read nor command A's mower: for B it does not exist.
    const foreign = a.mowers[0].id;
    const read = await request.get(`/api/v1/fleet/mowers/${foreign}`, {
      headers: bearer(b.token),
    });
    expect(read.status()).toBe(404);
    const command = await request.post(`/api/v1/fleet/mowers/${foreign}/commands`, {
      headers: bearer(b.token),
      data: { command: { kind: 'pause_mow' } },
    });
    expect([403, 404]).toContain(command.status());
    const state = await request.get(`/api/v1/telemetry/mowers/${foreign}/state`, {
      headers: bearer(b.token),
    });
    expect([403, 404]).toContain(state.status());

    // Realtime refuses B a subscription to A's mower (and sends none of its states).
    const live = await subscribeOverRealtime(b.page, b.token, [foreign]);
    expect(live.denied).toContain(foreign);
    expect(live.subscribed).not.toContain(foreign);
    expect(live.states).not.toContain(foreign);

    // A mows; nothing of it reaches B.
    const areas = await areaIds(request, a.token, foreign);
    const started = await request.post(`/api/v1/fleet/mowers/${foreign}/commands`, {
      headers: bearer(a.token),
      data: { command: { kind: 'start_mow', area_ids: areas.slice(0, 1) } },
    });
    expect(started.status(), await started.text()).toBe(202);
    await expect
      .poll(() => activityOf(request, a.token, foreign), { timeout: 60_000, intervals: [1_000] })
      .toBe('mowing');
    const after = await describeVisitor(request, b.page, b.token);
    expect(after.mowers.map((mower) => mower.id).sort()).toEqual([...bIds].sort());
    await expect(b.page.locator('[data-testid^="mower-row-"]')).toHaveCount(3);
    await expect(b.page.getByTestId(`mower-row-${foreign}`)).toHaveCount(0);
    const bLive = await subscribeOverRealtime(b.page, b.token, [...bIds, foreign]);
    expect(bLive.denied).toEqual([foreign]);
    expect(bLive.states).not.toContain(foreign);
  } finally {
    await first.context.close();
    await second.context.close();
  }
});

test('the sign-in page explains the sandbox and remembers the visitor', async ({
  page,
  request,
}) => {
  const config = await sandboxDemo(request);
  await page.goto('/login');
  const entry = page.getByTestId('login-demo');
  await expect(entry).toHaveAttribute('data-mode', 'sandbox');
  const note = page.getByTestId('login-demo-note');
  await expect(note).toContainText('Sandbox');
  await expect(note).toContainText(`${config?.sandbox?.idleMinutes ?? 30} min`);
  await expect(note).not.toContainText(/teilen/);
  expect(await page.evaluate((key) => localStorage.getItem(key), VISITOR_KEY)).toBeNull();
  await tryTheDemo(page, request);
  expect(await page.evaluate((key) => localStorage.getItem(key), VISITOR_KEY)).toBe('1');
});

test('a logout ends the sandbox session and the sign-in page then waits', async ({
  page,
  request,
}) => {
  await tryTheDemo(page, request);
  await page.goto('/settings#profile');
  await page.getByTestId('logout').click();
  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByTestId('login-demo-start')).toBeVisible();
  expect(await page.evaluate((key) => localStorage.getItem(key), VISITOR_KEY)).toBeNull();

  // The refresh cookie holds no session any more (401), or is gone altogether (422: no token).
  const refresh = await page.evaluate(async () => {
    const response = await fetch('/api/v1/identity/session/refresh', {
      method: 'POST',
      headers: { 'X-Requested-With': 'mowdeck-web', 'X-Mowdeck-Refresh': 'cookie-only' },
      credentials: 'same-origin',
    });
    return response.status;
  });
  expect([401, 422]).toContain(refresh);

  // No fresh sandbox by itself after a logout: the normal entry, and no demo session request.
  let started = 0;
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === DEMO_SESSION) started += 1;
  });
  await page.reload();
  await expect(page.getByTestId('login-demo-start')).toBeVisible();
  await page.waitForTimeout(2_000);
  await expect(page.getByTestId('login-demo-recreating')).toHaveCount(0);
  await expect(page).toHaveURL(/\/login/);
  expect(started).toBe(0);
});

test('a browser whose sandbox is gone gets a fresh one by itself', async ({
  page,
  context,
  request,
}) => {
  const first = await tryTheDemo(page, request);

  // The sandbox ends behind the visitor's back (as an idle deletion or a reset would): its
  // session is logged out with this browser's cookie, the visitor flag stays.
  const ended = await page.evaluate(async () => {
    const response = await fetch('/api/v1/identity/session/logout', {
      method: 'POST',
      headers: { 'X-Requested-With': 'mowdeck-web', 'X-Mowdeck-Refresh': 'cookie-only' },
      credentials: 'same-origin',
    });
    return response.status;
  });
  expect(ended).toBeLessThan(300);
  expect(await page.evaluate((key) => localStorage.getItem(key), VISITOR_KEY)).toBe('1');

  // The next visit lands on a fresh sandbox, no error page in between.
  let token = nextDemoToken(page);
  await page.goto('/mowers');
  await expect(page).toHaveURL(/\/mowers$/, { timeout: 60_000 });
  const second = await describeVisitor(request, page, await token);
  expect(second.accountId).not.toBe(first.accountId);
  expect(names(second)).toEqual(MOWER_NAMES);
  await expect(page.locator('[data-testid^="mower-row-"]')).toHaveCount(3, { timeout: 60_000 });

  // The same with the cookies gone (an expired cookie, cleared site data but the flag).
  await context.clearCookies();
  token = nextDemoToken(page);
  await page.reload();
  await expect(page).toHaveURL(/\/mowers$/, { timeout: 60_000 });
  const third = await describeVisitor(request, page, await token);
  expect(third.accountId).not.toBe(second.accountId);
  expect(third.accountId).not.toBe(first.accountId);
});

test('a full demo counts down and starts the sandbox by itself', async ({ page }) => {
  // The real cap is covered by Identity's tests; here the first answer is a full demo.
  let calls = 0;
  await page.route(`**${DEMO_SESSION}`, async (route) => {
    calls += 1;
    if (calls > 1) return route.continue();
    return route.fulfill({
      status: 503,
      headers: { 'Content-Type': 'application/problem+json', 'Retry-After': '3' },
      body: JSON.stringify({
        type: 'urn:mowdeck:problem:demo-full',
        title: 'The demo is full',
        status: 503,
      }),
    });
  });
  await page.goto('/login');
  await page.getByTestId('login-demo-start').click();

  const full = page.getByTestId('login-demo-full');
  await expect(full).toBeVisible();
  await expect(full.getByRole('heading', { name: 'Die Demo ist voll' })).toBeFocused();
  await expect(page.getByTestId('login-demo-countdown')).toHaveText(/^00:0[0-3]$/);
  await expect(page.getByTestId('login-demo-retry')).toBeVisible();
  await expect(full.getByRole('status')).toHaveCount(1);

  await expect(page).toHaveURL(/\/mowers$/, { timeout: 30_000 });
  expect(calls).toBe(2);
  await expect(page.locator('[data-testid^="mower-row-"]')).toHaveCount(3, { timeout: 60_000 });
});

test('too many sandboxes from one address say how long to wait', async ({ page }) => {
  let calls = 0;
  await page.route(`**${DEMO_SESSION}`, async (route) => {
    calls += 1;
    return route.fulfill({
      status: 429,
      headers: { 'Content-Type': 'application/problem+json', 'Retry-After': '600' },
      body: JSON.stringify({
        type: 'urn:mowdeck:problem:too-many-requests',
        title: 'Too many requests',
        status: 429,
      }),
    });
  });
  await page.goto('/login');
  await page.getByTestId('login-demo-start').click();
  await expect(page.getByTestId('login-demo-busy')).toContainText('10 min');
  await expect(page.getByTestId('login-demo-full')).toHaveCount(0);
  await page.waitForTimeout(2_000);
  expect(calls).toBe(1);
  await expect(page).toHaveURL(/\/login/);
});
