/**
 * The source code of the running version (AGPL-3.0 section 13, LICENSING.md): the link is on
 * the sign-in and registration pages for anyone, and in Settings, About, for every user; the
 * API names the same address in the X-Source-Code header. Runs on a phone, the desktop and in
 * WebKit.
 */
import { expect, test } from '@playwright/test';

import { actor, loginByUi, registerByApi, watchCsp } from './support';

/** The repository the test stack links (the official one unless the stack sets its own). */
const REPOSITORY = process.env.MOWDECK_E2E_SOURCE_URL ?? 'https://github.com/lukislp/mowdeck';

function expectSourceHref(href: string | null): void {
  expect(href).not.toBeNull();
  expect(href === REPOSITORY || href!.startsWith(`${REPOSITORY}/tree/`), href!).toBe(true);
}

test('the sign-in and registration pages link the source code without an account', async ({
  page,
}) => {
  const csp = watchCsp(page);
  await page.goto('/login');
  const footer = page.getByTestId('source-code');
  await expect(footer).toBeVisible();
  await expect(footer).toContainText('AGPL-3.0');
  await expect(page.getByTestId('source-version')).not.toBeEmpty();
  const link = page.getByRole('link', { name: /Quellcode/ });
  await expect(link).toBeVisible();
  await expect(link).toHaveAttribute('target', '_blank');
  expectSourceHref(await link.getAttribute('href'));

  await page.goto('/register');
  await expect(page.getByTestId('source-code-link')).toBeVisible();
  csp.assertNone();
});

test('Settings, About shows the version, the licence and the source of this build', async ({
  page,
  request,
}, testInfo) => {
  // One user per project: the projects share the stack and the run id.
  const user = actor(`source-${testInfo.project.name}`);
  await registerByApi(request, user);
  await loginByUi(page, user);
  await page.goto('/settings#about');
  const about = page.getByTestId('settings-section-about');
  await expect(about).toBeVisible();
  await expect(about.getByRole('heading', { name: 'Über' })).toBeVisible();
  await expect(about).toContainText('GNU AGPL-3.0');
  await expect(about.getByTestId('source-version')).not.toBeEmpty();
  const link = about.getByTestId('source-code-link');
  await expect(link).toBeVisible();
  const href = await link.getAttribute('href');
  expectSourceHref(href);
  await expect(link).toContainText(href!.replace(/^https?:\/\//, ''));
  await expect(page.getByTestId('settings-index-about')).toBeVisible();
});

test('every API response names the same source code address', async ({ page, request }) => {
  await page.goto('/login');
  const href = await page.getByTestId('source-code-link').getAttribute('href');
  const response = await request.get('/api/v1/identity/session');
  expect(response.headers()['x-source-code']).toBe(href);
});
