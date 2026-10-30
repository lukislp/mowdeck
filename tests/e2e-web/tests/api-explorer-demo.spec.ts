/**
 * The API explorer (ADR 0072) in the public demo, against a demo stack (CI job e2e-demo; locally
 * `MOWDECK_E2E_DEMO=1 pnpm e2e:stack up`). Runs in the projects `demo` and `demo-phone`; on any
 * other stack every test skips.
 *
 * Covers: the banner links the explorer; the page renders Scalar in its frame with the demo
 * note; "try it" sends a GET with the visitor's session to /api/v1 on the same origin and gets the
 * simulated mowers; the credential the explorer uses is refused (403 demo-mode) for a mutation
 * the demo does not allow; no request of the page or the frame leaves the origin; the frame has
 * its own CSP and the app's CSP stays as it was.
 */
import { expect, test, type APIRequestContext, type Frame, type Page } from '@playwright/test';

const DEMO_PROBLEM = 'urn:mowdeck:problem:demo-mode';
const MOWERS = '/api/v1/fleet/mowers';

async function explorerDemo(request: APIRequestContext): Promise<boolean> {
  const response = await request.get('/config.json');
  if (!response.ok()) return false;
  const config = (await response.json()) as Record<string, unknown>;
  return typeof config.demo === 'object' && config.demo !== null && config.apiExplorer === true;
}

test.beforeEach(async ({ request }) => {
  test.skip(!(await explorerDemo(request)), 'not a demo stack with the API explorer');
});

/** Every request the page and its frame send that does not go to the stack's own origin. */
function watchForeignRequests(page: Page, origin: string): string[] {
  const foreign: string[] = [];
  page.on('request', (request) => {
    const url = request.url();
    if (/^(data|blob|about):/.test(url)) return;
    if (new URL(url).origin !== origin) foreign.push(url);
  });
  return foreign;
}

async function openExplorer(page: Page): Promise<Frame> {
  await page.goto('/login');
  await page.getByTestId('login-demo-start').click();
  await expect(page).toHaveURL(/\/mowers$/);
  await page.getByTestId('demo-api-explorer').click();
  await expect(page).toHaveURL(/\/api-explorer$/);
  await expect(page.getByTestId('api-explorer-note')).toContainText('403 demo-mode');
  const element = page.getByTestId('api-explorer-frame');
  await expect(element).toBeVisible();
  const frame = await (await element.elementHandle())!.contentFrame();
  expect(frame).not.toBeNull();
  // Scalar has rendered the prepared Fleet document (the default): this origin as its server.
  await expect(frame!.locator('.scalar-app').first()).toBeVisible({ timeout: 60_000 });
  await expect(frame!.getByText('This installation').first()).toBeVisible({ timeout: 60_000 });
  return frame!;
}

test('the explorer renders the API documents with the demo note, nothing from elsewhere', async ({
  page,
  baseURL,
}) => {
  const foreign = watchForeignRequests(page, new URL(baseURL!).origin);
  await openExplorer(page);
  // Every document and the bundle are loaded before the check.
  await page.waitForLoadState('networkidle');
  expect(foreign).toEqual([]);
});

test('"try it" lists the simulated mowers; the same credential cannot do what the demo refuses', async ({
  page,
}) => {
  const frame = await openExplorer(page);
  const answer = page.waitForResponse(
    (response) =>
      response.request().method() === 'GET' &&
      new URL(response.url()).pathname === MOWERS &&
      response.request().frame() === frame,
  );
  // The operation's own link (what "Copy link" gives, the same on a phone and a desktop), then
  // Test Request and Send as a visitor clicks them. The reloaded frame asks the page for the
  // token again.
  await frame.goto(`${new URL(frame.url()).pathname}#fleet/tag/mowers/GET${MOWERS}`);
  await frame.locator('button', { hasText: `get ${MOWERS})` }).click({ timeout: 60_000 });
  // The client has one Send button per layout; the one shown is the one a visitor sees.
  await frame.locator('button', { hasText: /^Send/ }).filter({ visible: true }).first().click();

  const response = await answer;
  // The page handed the session's token to the frame; Scalar sent it, same origin, no proxy.
  const authorization = response.request().headers().authorization ?? '';
  expect(authorization).toMatch(/^Bearer [\w.-]+$/);
  expect(response.status()).toBe(200);
  expect(response.headers()['x-mowdeck-demo']).toBe('1');
  const { items } = (await response.json()) as { items: unknown[] };
  expect(items).toHaveLength(3);

  // The explorer's credential and request path (fetch from the frame) for a mutation the demo
  // refuses: a personal access token.
  const refused = await frame.evaluate(async (auth) => {
    const reply = await fetch('/api/v1/identity/me/api-tokens', {
      method: 'POST',
      headers: { Authorization: auth, 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'explorer', scopes: ['mower.view'], expires_in_days: 30 }),
    });
    return {
      status: reply.status,
      demo: reply.headers.get('x-mowdeck-demo'),
      type: ((await reply.json()) as { type?: string }).type,
    };
  }, authorization);
  expect(refused).toEqual({ status: 403, demo: '1', type: DEMO_PROBLEM });
});

test('the frame has its own CSP and the app keeps its own', async ({ request }) => {
  const app = await request.get('/api-explorer');
  const frame = await request.get('/_explorer/index.html');
  expect(frame.status()).toBe(200);
  const appPolicy = app.headers()['content-security-policy'] ?? '';
  const framePolicy = frame.headers()['content-security-policy'] ?? '';
  expect(appPolicy).toContain("frame-ancestors 'none'");
  expect(appPolicy).not.toContain('unsafe-inline');
  expect(framePolicy).toContain("frame-ancestors 'self'");
  expect(framePolicy).toContain("script-src 'self';");
  expect(framePolicy).toContain("connect-src 'self';");
  expect(framePolicy).not.toMatch(/https?:/);
  expect((await request.get('/_explorer/scalar.js')).status()).toBe(200);
});
