/**
 * Linking a vendor account in Settings (ADR 0066, desktop): the form is rendered from the
 * providers the Device-Gateway has enabled (`GET /api/v1/fleet/linked-accounts/providers`), so
 * the test reads them first and fills the fields the provider declares. The stack runs the
 * gateway with the fake clouds (`GATEWAY_FAKE_CLOUDS`, see stack.env): the test provider is the
 * first enabled one whose fields are an e-mail and a password, and its fake accepts any
 * `*@fake.mowdeck.test` address with `fake-cloud-password`. Covered: the guidance and fields of
 * the provider, a refused login on the secret field, the linked account's status, a
 * re-authentication with a wrong and then the right secret, and unlinking. No mower is
 * registered, so the fake devices stay free for the other specs.
 *
 * The second test links the untested Husqvarna provider (enabled in stack.env): an application
 * key and secret instead of an e-mail and a password, the "untested" and "developer account"
 * guidance, a refused secret, the linked card with the fake application's two mowers, unlinking.
 */
import { expect, test } from '@playwright/test';

import { RUN, actor, loginByUi, pickProvider, registerByApi, watchCsp } from './support';

const FLEET = '/api/v1/fleet';
const FAKE_PASSWORD = 'fake-cloud-password';
/** Every key with this prefix exists at the fake Husqvarna cloud (letters, digits, dashes). */
const FAKE_HUSQVARNA_KEY = `fake-husqvarna-${RUN.replace(/[^A-Za-z0-9-]/g, '')}`;

interface Field {
  key: string;
  kind: string;
  identity: boolean;
}
interface Provider {
  provider: string;
  name: string;
  fields: Field[];
  tested_on_hardware: boolean;
}

/** The input id the app gives a field of the settings form. */
function inputId(key: string): string {
  return `#linked-${key.replace(/_/g, '-')}`;
}

test('settings: link, re-authenticate and unlink a provider account', async ({ page, request }) => {
  test.setTimeout(240_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('providers'));
  const headers = { Authorization: `Bearer ${owner.token}` };
  const email = `providers-${RUN}@fake.mowdeck.test`;

  const listed = await request.get(`${FLEET}/linked-accounts/providers`, { headers });
  expect(listed.status(), await listed.text()).toBe(200);
  const providers = ((await listed.json()) as { items: Provider[] }).items;
  expect(providers.length).toBeGreaterThan(0);
  const provider = providers.find(
    (p) =>
      p.fields.length === 2 &&
      p.fields.some((f) => f.identity && f.kind === 'email') &&
      p.fields.some((f) => f.kind === 'secret'),
  );
  expect(provider, 'an e-mail and password provider is enabled').toBeTruthy();
  const identity = provider!.fields.find((f) => f.identity)!;
  const secret = provider!.fields.find((f) => f.kind === 'secret')!;

  try {
    await loginByUi(page, owner);
    await page.goto('/settings#linked-accounts');
    await expect(page.getByTestId('linked-empty')).toBeVisible();
    if (providers.length > 1) {
      await page.getByTestId(`provider-option-${provider!.provider}`).click();
    }
    const guide = page.getByTestId('provider-guide');
    await expect(guide).toContainText(provider!.name);
    await expect(page.getByTestId('provider-untested')).toHaveCount(
      provider!.tested_on_hardware ? 0 : 1,
    );

    // A wrong secret is refused by the (fake) vendor and lands on the secret field.
    await page.locator(inputId(identity.key)).fill(email);
    await page.locator(inputId(secret.key)).fill('not-the-password');
    await page.getByTestId('linked-connect-submit').click();
    await expect(page.locator(inputId(secret.key))).toHaveAttribute('aria-invalid', 'true', {
      timeout: 60_000,
    });
    await expect(page.locator(inputId(secret.key))).toHaveValue(''); // write-only

    // The right one links the account: a card with its status, never the secret.
    await page.locator(inputId(secret.key)).fill(FAKE_PASSWORD);
    await page.getByTestId('linked-connect-submit').click();
    const card = page.locator(`[data-testid="linked-account"][data-email="${email}"]`);
    await expect(card).toBeVisible({ timeout: 60_000 });
    await expect(card).toHaveAttribute('data-provider', provider!.provider);
    await expect(card.getByTestId('linked-account-status')).toHaveAttribute('data-status', 'ok');
    await expect(card.getByTestId('linked-account-devices')).not.toHaveText('0');
    await expect(page.locator('body')).not.toContainText(FAKE_PASSWORD);

    const accounts = await request.get(`${FLEET}/linked-accounts`, { headers });
    const linked = ((await accounts.json()) as { items: { id: string; identity: string }[] }).items;
    expect(linked.map((a) => a.identity)).toEqual([email]);
    expect(await accounts.text()).not.toContain(FAKE_PASSWORD);
    const reauthId = `#linked-account-${linked[0]!.id}-${secret.key.replace(/_/g, '-')}`;

    // Re-authentication with a wrong secret: refused at the field, recorded on the card.
    await card.getByTestId('linked-account-password-toggle').click();
    await expect(card.locator(reauthId)).toHaveValue(''); // never prefilled
    await card.locator(reauthId).fill('wrong-again');
    await card.getByTestId('linked-account-sync').click();
    await expect(card.locator(reauthId)).toHaveAttribute('aria-invalid', 'true', {
      timeout: 60_000,
    });
    await expect(card.getByTestId('linked-account-error')).toBeVisible();
    await expect(card.getByTestId('linked-account-status')).toHaveAttribute(
      'data-status',
      'failing',
    );

    // The right secret clears the error.
    await card.locator(reauthId).fill(FAKE_PASSWORD);
    await card.getByTestId('linked-account-sync').click();
    await expect(card.getByTestId('linked-account-error')).toHaveCount(0, { timeout: 60_000 });
    await expect(card.getByTestId('linked-account-status')).toHaveAttribute('data-status', 'ok');
    await expect(card.getByTestId('linked-account-reauth')).toHaveCount(0);

    // Unlinking asks first, then the card is gone.
    await card.getByTestId('linked-account-delete').click();
    await card.getByTestId('linked-account-delete-confirm').click();
    await expect(card).toHaveCount(0, { timeout: 30_000 });
    await expect(page.getByTestId('linked-empty')).toBeVisible();
    csp.assertNone();
  } finally {
    const left = await request.get(`${FLEET}/linked-accounts`, { headers });
    if (left.ok()) {
      for (const account of ((await left.json()) as { items: { id: string }[] }).items) {
        await request.delete(`${FLEET}/linked-accounts/${account.id}`, { headers });
      }
    }
  }
});

test('settings: link and unlink an untested Husqvarna application', async ({ page, request }) => {
  test.setTimeout(180_000);
  const csp = watchCsp(page);
  const owner = await registerByApi(request, actor('husqvarna'));
  const headers = { Authorization: `Bearer ${owner.token}` };

  const listed = await request.get(`${FLEET}/linked-accounts/providers`, { headers });
  expect(listed.status(), await listed.text()).toBe(200);
  const providers = ((await listed.json()) as { items: Provider[] }).items;
  const husqvarna = providers.find((p) => p.provider === 'husqvarna');
  expect(husqvarna, 'the stack enables the Husqvarna provider (stack.env)').toBeTruthy();
  expect(husqvarna!.tested_on_hardware).toBe(false);
  expect(husqvarna!.fields.map((f) => [f.key, f.kind, f.identity])).toEqual([
    ['app_key', 'text', true],
    ['app_secret', 'secret', false],
  ]);

  try {
    await loginByUi(page, owner);
    await page.goto('/settings#linked-accounts');
    await expect(page.getByTestId('linked-empty')).toBeVisible();
    await pickProvider(page, 'husqvarna');
    await expect(page.getByTestId('provider-guide')).toContainText(husqvarna!.name);
    await expect(page.getByTestId('provider-untested')).toBeVisible();
    await expect(page.getByTestId('provider-dev-account')).toBeVisible();
    await expect(page.locator('#linked-email')).toHaveCount(0);

    // A wrong secret is refused by the fake Husqvarna cloud and lands on the secret field.
    await page.locator('#linked-app-key').fill(FAKE_HUSQVARNA_KEY);
    await page.locator('#linked-app-secret').fill('not-the-secret');
    await page.getByTestId('linked-connect-submit').click();
    await expect(page.locator('#linked-app-secret')).toHaveAttribute('aria-invalid', 'true', {
      timeout: 60_000,
    });
    await expect(page.locator('#linked-app-secret')).toHaveValue(''); // write-only

    // The right secret links the application: its card, the fake application's two mowers.
    await page.locator('#linked-app-secret').fill(FAKE_PASSWORD);
    await page.getByTestId('linked-connect-submit').click();
    const card = page.locator(
      `[data-testid="linked-account"][data-provider="husqvarna"][data-email="${FAKE_HUSQVARNA_KEY}"]`,
    );
    await expect(card).toBeVisible({ timeout: 60_000 });
    await expect(card.getByTestId('linked-account-status')).toHaveAttribute('data-status', 'ok');
    await expect(card.getByTestId('linked-account-devices')).toHaveText('2');
    await expect(page.locator('body')).not.toContainText(FAKE_PASSWORD);

    const accounts = await request.get(`${FLEET}/linked-accounts`, { headers });
    const linked = (await accounts.json()) as {
      items: { provider: string; identity: string }[];
    };
    expect(linked.items.map((a) => [a.provider, a.identity])).toEqual([
      ['husqvarna', FAKE_HUSQVARNA_KEY],
    ]);
    expect(await accounts.text()).not.toContain(FAKE_PASSWORD);

    await card.getByTestId('linked-account-delete').click();
    await card.getByTestId('linked-account-delete-confirm').click();
    await expect(card).toHaveCount(0, { timeout: 30_000 });
    await expect(page.getByTestId('linked-empty')).toBeVisible();
    csp.assertNone();
  } finally {
    const left = await request.get(`${FLEET}/linked-accounts`, { headers });
    if (left.ok()) {
      for (const account of ((await left.json()) as { items: { id: string }[] }).items) {
        await request.delete(`${FLEET}/linked-accounts/${account.id}`, { headers });
      }
    }
  }
});
