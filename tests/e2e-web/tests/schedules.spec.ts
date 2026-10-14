/**
 * Schedules with rain pause against the stack's Scheduler (desktop, 1280 x 800), with the
 * static weather provider and its development endpoint (tests/e2e-web/stack.env):
 *
 * 1. the site gets the coordinates of the mower's map origin (the rain pause location);
 * 2. a weekly schedule for the simulated mower is created in the form one or two minutes ahead
 *    (weekday chip, time, window, both areas tapped in order) and fires: the run is `started`
 *    and the mower mows; the mower page shows it as the next schedule;
 * 3. skip-next marks the next firing as skipped;
 * 4. with the weather switched to rain, run-now with "respect the rain pause" is `skipped_rain`;
 * 5. a Viewer sees the list but no create, edit, skip or run controls.
 */
import { expect, test, type APIRequestContext } from '@playwright/test';

import {
  RUN,
  actor,
  commandByApi,
  createMowerByApi,
  loginByUi,
  registerByApi,
  waitForMap,
  watchCsp,
  type Actor,
} from './support';

const SCHEDULER = '/api/v1/scheduler';

/** Weekday (`mon`..`sun`) and `HH:MM` of a time in the stack's default site time zone. */
function berlin(date: Date): { day: string; time: string } {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-GB', {
      timeZone: 'Europe/Berlin',
      weekday: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    })
      .formatToParts(date)
      .map((part) => [part.type, part.value]),
  );
  return {
    day: String(parts.weekday).toLowerCase().slice(0, 3),
    time: `${parts.hour}:${parts.minute}`,
  };
}

async function setWeather(request: APIRequestContext, who: Actor, mode: string): Promise<void> {
  const response = await request.put(`${SCHEDULER}/dev/weather`, {
    headers: { Authorization: `Bearer ${who.token}` },
    data: { mode },
  });
  expect(response.status(), await response.text()).toBe(200);
}

async function activity(request: APIRequestContext, who: Actor, mowerId: string) {
  const response = await request.get(`/api/v1/telemetry/mowers/${mowerId}/state`, {
    headers: { Authorization: `Bearer ${who.token}` },
  });
  return response.ok()
    ? ((await response.json()) as { state: { activity: string } }).state.activity
    : null;
}

async function createViewer(request: APIRequestContext, owner: Actor): Promise<Actor> {
  const auth = { Authorization: `Bearer ${owner.token}` };
  const roles = (await (
    await request.get(`/api/v1/identity/accounts/${owner.accountId}/roles`, { headers: auth })
  ).json()) as { id: string; key: string | null }[];
  const viewerRole = roles.find((role) => role.key === 'viewer');
  expect(viewerRole, 'viewer role').toBeDefined();
  const viewer = actor('sched-viewer');
  const created = await request.post(`/api/v1/identity/accounts/${owner.accountId}/users`, {
    headers: auth,
    data: {
      username: viewer.username,
      password: viewer.password,
      assignments: [{ role_id: viewerRole!.id, level: { type: 'account', id: owner.accountId } }],
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  return viewer;
}

test('schedules: create, fire, skip next, run now in the rain, viewer read-only', async ({
  page,
  request,
  browser,
}) => {
  test.setTimeout(420_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('sched'));
  const mowerId = await createMowerByApi(request, owner, `Luba ${RUN}`);
  await waitForMap(request, owner, mowerId);
  await setWeather(request, owner, 'dry');

  try {
    await loginByUi(page, owner);

    // 1. The site of the mower gets the map origin as its coordinates.
    await page.goto(`/mowers/${mowerId}/settings`);
    const site = page.getByTestId('site-location');
    await expect(site.getByTestId('site-no-coordinates')).toBeVisible();
    await site.getByTestId('site-use-origin').click();
    await expect(page.locator('#site-latitude')).not.toHaveValue('');
    const siteSaved = page.waitForResponse(
      (r) => r.url().includes('/api/v1/fleet/sites/') && r.request().method() === 'PATCH',
    );
    await site.getByTestId('site-save').click();
    expect((await siteSaved).status()).toBe(200);
    await expect(site.getByTestId('site-no-coordinates')).toHaveCount(0);

    // 2. A weekly schedule one or two minutes ahead, created in the form.
    await page.getByTestId('nav-schedules').click();
    await page.waitForURL(/\/schedules$/);
    await expect(page.getByTestId('schedules-empty')).toBeVisible();
    await page.getByTestId('schedule-new').click();
    await page.waitForURL(/\/schedules\/new$/);

    const now = new Date();
    const at = berlin(new Date(now.getTime() + (now.getSeconds() >= 40 ? 2 : 1) * 60_000));
    await page.locator('#sched-name').fill('Morgens vorne');
    await expect(page.locator('#sched-target')).toHaveValue(mowerId);
    const days = page.getByTestId('sched-days');
    for (const day of ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']) {
      const chip = days.locator(`button[data-day="${day}"]`);
      const pressed = (await chip.getAttribute('aria-pressed')) === 'true';
      if (pressed !== (day === at.day)) await chip.click();
    }
    // Client checks: no name refuses to save and names the field.
    await page.locator('#sched-name').fill('');
    await page.getByTestId('sched-save').click();
    await expect(page.locator('#sched-name')).toHaveAttribute('aria-invalid', 'true');
    await page.locator('#sched-name').fill('Morgens vorne');
    await page.locator('#sched-time').fill(at.time);
    await page.locator('#sched-duration').fill('60');
    const areas = page.getByTestId('sched-areas');
    await expect(page.getByTestId('sched-map')).toHaveAttribute('data-ready', 'true');
    await areas.locator('button[data-area="front"]').click();
    await areas.locator('button[data-area="back"]').click();
    await expect(page.getByTestId('sched-area-summary')).toContainText('2');
    await expect(page.getByTestId('preview-verdict')).toHaveAttribute('data-value', 'run');
    const created = page.waitForResponse(
      (r) => r.url().endsWith(`${SCHEDULER}/schedules`) && r.request().method() === 'POST',
    );
    await page.getByTestId('sched-save').click();
    const createdResponse = await created;
    expect(createdResponse.status(), await createdResponse.text()).toBe(201);
    const scheduleId = ((await createdResponse.json()) as { id: string }).id;
    await page.waitForURL(/\/schedules$/);

    const row = page.locator(`[data-testid="schedule-row"][data-id="${scheduleId}"]`);
    await expect(row.getByTestId('schedule-rule')).toContainText(`${at.time}, 60 min`);
    await expect(row.getByTestId('schedule-verdict')).toHaveAttribute('data-value', 'run');
    await expect(row.getByTestId('schedule-last')).toContainText('nie');

    // The schedule fires at its minute: the run is started and the mower mows.
    const auth = { Authorization: `Bearer ${owner.token}` };
    await expect
      .poll(
        async () => {
          const runs = (await (
            await request.get(`${SCHEDULER}/schedules/${scheduleId}/runs`, { headers: auth })
          ).json()) as { items: { outcome: string }[] };
          return runs.items.map((run) => run.outcome);
        },
        { timeout: 200_000, intervals: [2_000] },
      )
      .toContain('started');
    await expect.poll(() => activity(request, owner, mowerId), { timeout: 30_000 }).toBe('mowing');
    await page.reload();
    await expect(row.getByTestId('schedule-last-outcome')).toHaveAttribute('data-value', 'started');

    await page.goto(`/mowers/${mowerId}`);
    await expect(page.getByTestId('mower-state')).toContainText('Mäht');
    await expect(page.getByTestId('next-schedule-link')).toContainText('Morgens vorne');

    await page.goto(`/schedules/${scheduleId}/runs`);
    const run = page.getByTestId('run-row').first();
    await expect(run).toHaveAttribute('data-outcome', 'started');
    await expect(run.getByTestId('run-command')).toHaveAttribute(
      'href',
      `/mowers/${mowerId}#recent-commands`,
    );

    // 3. Skip the next firing.
    await page.goto('/schedules');
    await row.getByTestId('schedule-skip').click();
    await expect(row.getByTestId('schedule-skipped')).toBeVisible();

    // 4. Rain: run now with the rain pause respected is skipped. The mower must be docked first
    // (a busy mower is skipped before the weather is asked).
    await commandByApi(request, owner, mowerId, { kind: 'return_to_dock' });
    await expect
      .poll(() => activity(request, owner, mowerId), { timeout: 120_000, intervals: [1_000] })
      .toMatch(/docked|charging|idle/);
    await setWeather(request, owner, 'rain');
    await row.getByTestId('schedule-run').click();
    const sheet = page.getByTestId('run-sheet');
    await expect(sheet.getByTestId('run-respect-rain')).toHaveAttribute('aria-checked', 'true');
    await sheet.getByTestId('run-confirm').click();
    await expect(sheet.getByTestId('run-result')).toContainText('Regen');
    await expect(
      sheet.getByTestId('run-result').locator('[data-value="skipped_rain"]'),
    ).toBeVisible();
    await sheet.getByTestId('run-close').click();
    await expect(row.getByTestId('schedule-last-outcome')).toHaveAttribute(
      'data-value',
      'skipped_rain',
    );
    expect(await activity(request, owner, mowerId)).not.toBe('mowing');
    csp.assertNone();

    // 5. A Viewer sees the schedule but none of the controls.
    const viewer = await createViewer(request, owner);
    const viewerPage = await (
      await browser.newContext({ viewport: { width: 1280, height: 800 }, locale: 'de-DE' })
    ).newPage();
    await loginByUi(viewerPage, viewer);
    await viewerPage.getByTestId('nav-schedules').click();
    const viewerRow = viewerPage.locator(`[data-testid="schedule-row"][data-id="${scheduleId}"]`);
    await expect(viewerRow).toContainText('Morgens vorne');
    await expect(viewerRow.getByTestId('schedule-state')).toBeVisible();
    await expect(viewerPage.getByTestId('schedule-new')).toHaveCount(0);
    for (const control of ['schedule-edit', 'schedule-skip', 'schedule-run', 'schedule-enabled']) {
      await expect(viewerRow.getByTestId(control)).toHaveCount(0);
    }
    await viewerRow.getByTestId('schedule-link').click();
    await expect(viewerPage.getByTestId('schedule-readonly')).toBeVisible();
    await expect(viewerPage.getByTestId('sched-save')).toHaveCount(0);
    await viewerPage.context().close();
  } finally {
    await setWeather(request, owner, 'dry');
  }
});
