/**
 * Phase 4 Task 21, fleet management in the web app (desktop, 1280 x 800): the owner creates a
 * second site (coordinates from a mower's map origin) and a group, moves a mower to the new
 * site and into the group, and gives a sub-user the Operator role at the group level; the
 * sub-user then sees only that mower (list and fleet map). "Pause all" on the group reports the
 * outcome per mower, and after the mower's job the reports page shows totals and exports a CSV
 * with the expected header.
 */
import { readFile } from 'node:fs/promises';

import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import {
  RUN,
  actor,
  commandByApi,
  loginByUi,
  registerByApi,
  waitForMap,
  watchCsp,
  type Actor,
} from './support';

const CSV_HEADER =
  'mower_id;mower;site;from;to;mowing_hours;blade_hours;distance_m;jobs;faults;availability_percent;faults_by_code';

async function createPlacedMower(
  request: APIRequestContext,
  owner: Actor,
  name: string,
  origin: [number, number],
): Promise<string> {
  const response = await request.post('/api/v1/fleet/mowers', {
    headers: { Authorization: `Bearer ${owner.token}` },
    data: {
      name,
      connection_kind: 'simulated',
      connection_options: { origin_lat: origin[0], origin_lon: origin[1] },
    },
  });
  expect(response.status(), await response.text()).toBe(201);
  return ((await response.json()) as { id: string }).id;
}

async function activityOf(
  request: APIRequestContext,
  owner: Actor,
  mowerId: string,
): Promise<string> {
  const response = await request.get(`/api/v1/telemetry/mowers/${mowerId}/state`, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  return response.ok()
    ? ((await response.json()) as { state: { activity: string } }).state.activity
    : 'none';
}

async function idByName(
  request: APIRequestContext,
  owner: Actor,
  path: string,
  name: string,
): Promise<string> {
  const response = await request.get(path, {
    headers: { Authorization: `Bearer ${owner.token}` },
  });
  const items = ((await response.json()) as { items: { id: string; name: string }[] }).items;
  const found = items.find((item) => item.name === name);
  expect(found, `${name} in ${path}`).toBeDefined();
  return found!.id;
}

async function number(page: Page, testid: string): Promise<number> {
  const text = (await page.getByTestId(testid).textContent()) ?? '';
  return Number(text.replace(/[^\d,]/g, '').replace(',', '.'));
}

test('fleet: sites, groups, staff per group, bulk pause, reports with CSV', async ({
  page,
  request,
  browser,
}) => {
  test.setTimeout(420_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('fleet'));
  const nord = await createPlacedMower(request, owner, `Luba Nord ${RUN}`, [48.137154, 11.576124]);
  const sued = await createPlacedMower(request, owner, `Luba Süd ${RUN}`, [48.1186, 11.6012]);
  await waitForMap(request, owner, nord);
  await waitForMap(request, owner, sued);

  // --- a second site, coordinates from the mower's map origin ----------------------------
  const siteName = `Werk Süd ${RUN}`;
  await loginByUi(page, owner);
  await page.getByTestId('nav-fleet').click();
  await page.waitForURL(/\/fleet$/);
  await page.getByTestId('fleet-tab-sites').click();
  await page.waitForURL(/\/fleet\/sites$/);
  await expect(page.getByTestId('site-default')).toBeVisible();
  await page.getByTestId('site-add').click();
  const sheet = page.getByTestId('site-sheet');
  await sheet.getByTestId('site-form-name').fill(siteName);
  await sheet.locator('#site-form-city').fill('München');
  await sheet.locator('#site-form-country').fill('de');
  await sheet.locator('#site-form-origin').selectOption({ label: `Luba Süd ${RUN}` });
  await sheet.getByTestId('site-form-origin').click();
  await expect(sheet.locator('#site-form-latitude')).toHaveValue('48.118600');
  await page.getByTestId('site-form-save').click();
  await expect(page.getByText(`Standort ${siteName} angelegt`)).toBeVisible();
  const siteId = await idByName(
    request,
    owner,
    '/api/v1/fleet/sites?include_default=true',
    siteName,
  );
  const card = page.getByTestId(`site-${siteId}`);
  await expect(card).toContainText('48,1186');
  await expect(card).toContainText('München, DE');

  // --- move the south mower to the new site ----------------------------------------------
  await card.getByTestId(`site-move-${siteId}`).click();
  await page.getByTestId(`move-${sued}`).click();
  await expect(page.getByText(`Luba Süd ${RUN} ist jetzt in ${siteName}`)).toBeVisible();
  await page.getByTestId('site-move-sheet').getByRole('button', { name: 'Schließen' }).click();
  await expect(card).toContainText(`Luba Süd ${RUN}`);

  // --- a group at the new site with the moved mower --------------------------------------
  const groupName = `Kolonne ${RUN}`;
  await page.getByTestId('fleet-tab-groups').click();
  await page.waitForURL(/\/fleet\/groups$/);
  await page.getByTestId('group-add').click();
  await page.getByTestId('group-form-name').fill(groupName);
  await page.locator('#group-form-site').selectOption({ label: siteName });
  await page.getByTestId('group-form-save').click();
  await expect(page.getByText(`Gruppe ${groupName} angelegt`)).toBeVisible();
  const groupId = await idByName(request, owner, '/api/v1/fleet/groups', groupName);
  await page.getByTestId(`group-members-edit-${groupId}`).click();
  await page.getByTestId(`member-${sued}`).click();
  await expect(page.getByTestId(`member-${sued}`)).toHaveAttribute('aria-pressed', 'true');
  await page.getByTestId('group-members-sheet').getByRole('button', { name: 'Schließen' }).click();
  await expect(page.getByTestId(`group-members-${groupId}`)).toContainText(`Luba Süd ${RUN}`);
  await expect(page.getByTestId(`group-members-${groupId}`)).not.toContainText('Luba Nord');

  // The mower settings show the new site and the group.
  await page.goto(`/mowers/${sued}/settings`);
  await expect(page.locator('#placement-site')).toHaveValue(siteId);
  await expect(page.getByTestId(`placement-group-${groupId}`)).toHaveAttribute(
    'aria-pressed',
    'true',
  );

  // --- staff: a sub-user with the Operator role on the group -----------------------------
  const staff = actor('fleet-staff');
  await page.goto('/settings/users');
  await page.locator('#new-username').fill(staff.username);
  await page.locator('#new-password').fill(staff.password);
  await page.locator('#new-role').selectOption('');
  await page.getByLabel('Passwort beim ersten Anmelden ändern').uncheck();
  await page.getByTestId('user-submit').click();
  await expect(page.getByText(`Benutzer ${staff.username} angelegt`)).toBeVisible();
  await page.getByTestId(`assign-${staff.username}`).click();
  const assign = page.getByTestId('assign-sheet');
  await assign.locator('#assign-role').selectOption({ label: 'Bediener' });
  await assign.getByTestId('assign-level-type').locator('[data-value="group"]').click();
  await assign.locator('#assign-level-object').selectOption({ label: groupName });
  await page.getByTestId('assign-save').click();
  const member = page.getByTestId(`member-${staff.username}`);
  await expect(member).toContainText('Bediener');
  await expect(member).toContainText(`Gruppe ${groupName}`);

  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const staffPage = await context.newPage();
  const staffCsp = watchCsp(staffPage);
  await loginByUi(staffPage, staff);
  await expect(staffPage.getByTestId(`mower-row-${sued}`)).toBeVisible();
  await expect(staffPage.getByTestId(`mower-row-${nord}`)).toHaveCount(0);
  await staffPage.goto('/fleet');
  const staffMap = staffPage.getByTestId('fleet-map');
  await expect(staffMap).toHaveAttribute('data-ready', 'true');
  await expect(staffMap).toHaveAttribute('data-markers', '1');
  await expect(staffMap.getByTestId('fleet-marker')).toHaveCount(1);
  await expect(staffPage.getByTestId('fleet-pause-all')).toHaveCount(0); // no fleet.bulk_command
  await staffMap.getByTestId('fleet-marker').click();
  await expect(staffPage.getByTestId('fleet-card')).toContainText(`Luba Süd ${RUN}`);
  // A bulk command through the API is refused for the staff member (no can_bulk_command).
  const login = await request.post('/api/v1/identity/session', {
    data: { username: staff.username, password: staff.password },
  });
  const staffToken = ((await login.json()) as { access_token: string }).access_token;
  const refused = await request.post(`/api/v1/fleet/groups/${groupId}/commands`, {
    headers: { Authorization: `Bearer ${staffToken}` },
    data: { command: { kind: 'pause_mow' } },
  });
  expect(refused.status()).toBe(403);
  staffCsp.assertNone();
  await context.close();

  // --- the owner's fleet map shows both mowers -------------------------------------------
  await page.goto('/fleet');
  const map = page.getByTestId('fleet-map');
  await expect(map).toHaveAttribute('data-ready', 'true');
  await expect(map).toHaveAttribute('data-markers', '2');
  await expect(page.getByTestId('fleet-legend')).toContainText('2');
  await page.locator('#fleet-site').selectOption(siteId);
  await expect(map).toHaveAttribute('data-markers', '1');

  // --- a job of the south mower, then "pause all" on the group ---------------------------
  await commandByApi(request, owner, sued, { kind: 'start_mow', area_ids: ['front', 'back'] });
  await expect
    .poll(() => activityOf(request, owner, sued), { timeout: 60_000, intervals: [1_000] })
    .toBe('mowing');
  // Let it mow a while, so the report has blade and mowing time.
  await page.waitForTimeout(20_000);
  await page.goto('/fleet/groups');
  await page.getByTestId(`group-pause-${groupId}`).click();
  const bulk = page.getByTestId('bulk-sheet');
  await expect(bulk).toContainText(`Luba Süd ${RUN}`);
  await page.getByTestId('bulk-confirm').click();
  await expect(page.getByTestId(`bulk-line-${sued}`)).toHaveAttribute('data-outcome', 'accepted');
  await expect(page.getByTestId('bulk-summary')).toContainText('1 angenommen');
  await page.getByTestId('bulk-done').click();
  await expect
    .poll(() => activityOf(request, owner, sued), { timeout: 30_000, intervals: [1_000] })
    .toBe('paused');

  // --- reports: totals after the job and the CSV export ----------------------------------
  await page.goto('/reports');
  await expect(page.getByTestId(`report-row-${sued}`)).toBeVisible();
  await expect.poll(() => number(page, 'report-total-jobs')).toBeGreaterThan(0);
  expect(await number(page, 'report-total-mowing')).toBeGreaterThan(0);
  // Availability over today (the share of the elapsed UTC day online) is above zero. The
  // displayed total rounds to one decimal, which late in the UTC day is "0,0" for a mower that
  // was online for seconds, so the unrounded meter width of the mowing mower's row is read.
  await page.getByTestId('report-period').locator('[data-value="today"]').click();
  await expect
    .poll(async () => {
      const width = await page
        .getByTestId(`report-row-${sued}`)
        .locator('.fill')
        .evaluate((el) => (el as HTMLElement).style.inlineSize);
      return Number.parseFloat(width || '0');
    })
    .toBeGreaterThan(0);
  await expect(page.getByTestId('report-total-availability')).toBeVisible();
  await expect(page.getByTestId(`report-row-${nord}`)).toBeVisible();
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByTestId('report-csv').click(),
  ]);
  expect(download.suggestedFilename()).toMatch(
    /^mowdeck-report-alle-standorte-\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}\.csv$/,
  );
  const csv = await readFile((await download.path())!, 'utf-8');
  const lines = csv.split('\r\n');
  expect(lines[0]).toBe(String.fromCharCode(0xfeff) + CSV_HEADER);
  expect(csv).toContain(`${sued};Luba Süd ${RUN};${siteName};`);
  expect(lines.some((line) => line.startsWith('total;Summe;'))).toBe(true);

  // Per site: only the moved mower.
  await page.locator('#report-site').selectOption(siteId);
  await expect(page.getByTestId(`report-row-${nord}`)).toHaveCount(0);
  await expect(page.getByTestId(`report-row-${sued}`)).toBeVisible();
  csp.assertNone();

  await commandByApi(request, owner, sued, { kind: 'return_to_dock' });
});
