/**
 * Chat control (ADR 0075): the optional assistant runs in every test stack with the phrase
 * parser (tests/e2e-web/stack.env), so no AI is ever called. Identity does not list it there, so
 * this spec adds the assistant's UI area to the page's ui-areas answer; everything else is the
 * real stack: the assistant reads Identity, Fleet and Telemetry with the user's token, and a
 * confirmed work order goes through Fleet's commands and the Scheduler's schedules.
 *
 * The spec checks that nothing runs before the confirmation (no schedule, no command), that a
 * confirmed later start becomes a one-off schedule on the named area, that driving by chat is
 * refused with an explanation, that a discarded order sends nothing and a confirmed start sends
 * one `start_mow`, and that without an AI provider the box links to the provider settings.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

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

const ASSISTANT_AREA = {
  route: '/assistant',
  module: 'assistant',
  required_permission: 'mower.control',
};

/** The assistant's UI area in the ui-areas answer (Identity of the test stack does not list it). */
async function announceAssistant(page: Page): Promise<void> {
  await page.route('**/api/v1/identity/ui-areas', async (route) => {
    const response = await route.fetch();
    const areas = (await response.json()) as unknown[];
    await route.fulfill({ response, json: [...areas, ASSISTANT_AREA] });
  });
}

async function schedulesOf(request: APIRequestContext, owner: Actor, mowerId: string) {
  const response = await request.get('/api/v1/scheduler/schedules', {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  expect(response.status(), await response.text()).toBe(200);
  const items = ((await response.json()) as { items: Record<string, unknown>[] }).items;
  return items.filter((s) => s.target_id === mowerId);
}

async function commandKinds(request: APIRequestContext, owner: Actor, mowerId: string) {
  const response = await request.get(`/api/v1/fleet/mowers/${mowerId}/commands`, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  expect(response.status(), await response.text()).toBe(200);
  return ((await response.json()) as { items: { kind: string }[] }).items.map((c) => c.kind);
}

async function ask(page: Page, text: string): Promise<void> {
  const input = page.getByTestId('chat-input');
  await input.fill(text);
  await input.press('Enter');
  await expect(page.getByTestId('chat-order')).toBeVisible();
}

function tomorrowInBerlin(): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Berlin' }).format(
    new Date(Date.now() + 24 * 3600 * 1000),
  );
}

test('chat control: a typed request becomes a work order that runs only after confirmation', async ({
  page,
  request,
}) => {
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('chat'));
  const mowerId = await createMowerByApi(request, owner, `Chat ${RUN}`);
  await waitForMap(request, owner, mowerId);

  await announceAssistant(page);
  await loginByUi(page, owner);
  await page.goto(`/mowers/${mowerId}`);
  const chat = page.getByTestId('chat-control');
  await expect(chat).toBeVisible();
  await expect(page.getByTestId('chat-source')).toContainText('ohne KI');

  // A later start on the back lawn: shown as a work order, nothing created yet.
  await ask(page, 'mäh morgen früh nur hinten');
  const later = page.getByTestId('chat-action');
  await expect(later).toHaveCount(1);
  await expect(later).toHaveAttribute('data-kind', 'start_later');
  await expect(page.getByTestId('chat-areas')).toHaveText('Back lawn');
  await expect(page.getByTestId('chat-when')).toContainText('08:00');
  expect(await schedulesOf(request, owner, mowerId)).toEqual([]);

  await page.getByTestId('chat-confirm').click();
  await expect(page.getByTestId('chat-order')).toBeHidden();
  await expect.poll(async () => (await schedulesOf(request, owner, mowerId)).length).toBe(1);
  const [schedule] = await schedulesOf(request, owner, mowerId);
  expect(schedule).toMatchObject({
    kind: 'once',
    anchor_date: tomorrowInBerlin(),
    start_time: '08:00:00',
    area_ids: ['back'],
    timezone: 'Europe/Berlin',
  });

  // Driving by chat is refused with an explanation: no action to confirm.
  await ask(page, 'fahr zwei Meter nach links');
  await expect(page.getByTestId('chat-nothing')).toBeVisible();
  await expect(page.getByTestId('chat-notes')).toContainText('Fahrsteuerung');
  await expect(page.getByTestId('chat-confirm')).toHaveCount(0);

  // A discarded order sends nothing; a confirmed one sends exactly one start.
  const before = await commandKinds(request, owner, mowerId);
  await ask(page, 'Mäh jetzt vorne');
  await expect(page.getByTestId('chat-action')).toHaveAttribute('data-kind', 'start_now');
  await expect(page.getByTestId('chat-areas')).toHaveText('Front lawn');
  await page.getByTestId('chat-discard').click();
  await expect(page.getByTestId('chat-order')).toBeHidden();
  expect(await commandKinds(request, owner, mowerId)).toEqual(before);

  await ask(page, 'Mäh jetzt vorne');
  await page.getByTestId('chat-confirm').click();
  await expect(page.getByTestId('chat-order')).toBeHidden();
  await expect
    .poll(async () =>
      (await commandKinds(request, owner, mowerId)).filter((k) => k === 'start_mow'),
    )
    .toEqual(['start_mow']);

  csp.assertNone();
});

test('chat control: without an AI provider the box links to the provider settings', async ({
  page,
  request,
}) => {
  const owner = await registerByApi(request, actor('chat-noai'));
  const mowerId = await createMowerByApi(request, owner, `Chat noai ${RUN}`);

  await announceAssistant(page);
  // The stack runs the phrase parser; this is how the provider interpreter answers a user
  // without an own provider and without the installation's model.
  await page.route('**/api/v1/assistant/status', (route) =>
    route.fulfill({
      json: {
        ready: false,
        interpreter: 'provider',
        reason: 'no_provider',
        model: null,
        hosted: null,
      },
    }),
  );
  await loginByUi(page, owner);
  await page.goto(`/mowers/${mowerId}`);
  const setup = page.getByTestId('chat-no-provider');
  await expect(setup).toBeVisible();
  await expect(page.getByTestId('chat-input')).toHaveCount(0);
  await expect(page.getByTestId('chat-provider-settings')).toHaveAttribute(
    'href',
    '/settings#ai-provider',
  );
});

test('chat control: hidden where the add-on is not deployed', async ({ page, request }) => {
  const owner = await registerByApi(request, actor('chat-off'));
  const mowerId = await createMowerByApi(request, owner, `Chat off ${RUN}`);
  await loginByUi(page, owner);
  await page.goto(`/mowers/${mowerId}`);
  await expect(page.getByTestId('mower-state')).toBeVisible();
  await expect(page.getByTestId('chat-control')).toHaveCount(0);
});
