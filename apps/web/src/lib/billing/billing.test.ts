import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiProblem } from '$lib/api/problem';
import { m } from '$lib/paraglide/messages';

import { BillingAccount, type BillingLoaders } from './account.svelte';
import {
  BILLING_STATUS_URL,
  formatAmount,
  formatPrice,
  hasOpenSubscription,
  intervalLabel,
  invoiceStatusLabel,
  parseBillingStatus,
  planTitle,
  probeBilling,
  resetBillingProbe,
  safeExternalUrl,
  subscriptionStatusLabel,
  type BillingPlan,
  type BillingPrice,
  type Invoice,
  type Subscription,
  type SubscriptionStatus,
} from './billing';

const STATUS = {
  enabled: true,
  provider: 'stripe',
  currency: 'eur',
  automatic_tax: true,
  plans: [
    {
      key: 'home',
      name: 'Home',
      description: null,
      trial_days: 14,
      price: {
        price_id: 'price_1',
        currency: 'eur',
        unit_amount: 990,
        interval: 'month',
        interval_count: 1,
      },
    },
  ],
};

function json(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
    ...init,
  });
}

function price(overrides: Partial<BillingPrice> = {}): BillingPrice {
  return {
    price_id: 'price_x',
    currency: 'eur',
    unit_amount: 990,
    interval: 'month',
    interval_count: 1,
    ...overrides,
  };
}

function subscription(status: SubscriptionStatus): Subscription {
  return {
    plan: 'home',
    status,
    plan_status: 'active',
    current_period_end: '2026-11-10T00:00:00Z',
    trial_end: null,
    grace_until: null,
    cancel_at_period_end: false,
    updated_at: '2026-10-10T00:00:00Z',
  };
}

function invoice(number: string, issuedAt: string): Invoice {
  return {
    number,
    status: 'paid',
    currency: 'eur',
    amount_due: 990,
    amount_paid: 990,
    issued_at: issuedAt,
    hosted_url: 'https://invoice.stripe.com/i/1',
    pdf_url: null,
  };
}

/** Normalize the narrow no-break spaces Intl puts between amount and symbol. */
const plain = (text: string) => text.replace(/\s/g, ' ');

describe('probeBilling', () => {
  afterEach(() => resetBillingProbe());

  it('returns the status for a 200 JSON answer with enabled true', async () => {
    const fetchFn = vi.fn(async () => json(STATUS));
    const status = await probeBilling(fetchFn);
    expect(status?.provider).toBe('stripe');
    expect(status?.plans.map((p) => p.key)).toEqual(['home']);
    expect(fetchFn).toHaveBeenCalledWith(BILLING_STATUS_URL, expect.any(Object));
  });

  it('probes once per page load and caches the answer', async () => {
    const fetchFn = vi.fn(async () => json(STATUS));
    const first = await probeBilling(fetchFn);
    const second = await probeBilling(fetchFn);
    expect(second).toBe(first);
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });

  it('treats a 404 as billing disabled', async () => {
    expect(await probeBilling(async () => json({ detail: 'nope' }, { status: 404 }))).toBeNull();
  });

  it('treats a network error as billing disabled', async () => {
    expect(await probeBilling(async () => Promise.reject(new TypeError('offline')))).toBeNull();
  });

  it('treats the SPA fallback (200 text/html) as billing disabled', async () => {
    const html = new Response('<!doctype html><html></html>', {
      status: 200,
      headers: { 'content-type': 'text/html' },
    });
    expect(await probeBilling(async () => html)).toBeNull();
  });

  it('treats invalid JSON as billing disabled', async () => {
    const broken = new Response('{not json', { headers: { 'content-type': 'application/json' } });
    expect(await probeBilling(async () => broken)).toBeNull();
  });

  it('treats enabled false or a missing flag as billing disabled', async () => {
    expect(await probeBilling(async () => json({ ...STATUS, enabled: false }))).toBeNull();
    resetBillingProbe();
    const withoutFlag: Record<string, unknown> = { ...STATUS };
    delete withoutFlag.enabled;
    expect(await probeBilling(async () => json(withoutFlag))).toBeNull();
  });
});

describe('parseBillingStatus', () => {
  it('rejects bodies that are not a status', () => {
    expect(parseBillingStatus(null)).toBeNull();
    expect(parseBillingStatus('enabled')).toBeNull();
    expect(parseBillingStatus({ enabled: 'true', plans: [] })).toBeNull();
    expect(parseBillingStatus({ enabled: true })).toBeNull();
  });

  it('drops malformed plans and defaults the other fields', () => {
    const status = parseBillingStatus({
      enabled: true,
      plans: [null, { key: 'x' }, { key: 'ok', price: price() }, { price: price() }],
    });
    expect(status).toEqual({
      enabled: true,
      provider: '',
      currency: '',
      automatic_tax: false,
      plans: [{ key: 'ok', price: price() }],
    });
  });
});

describe('billing price formatting', () => {
  it('formats cents with the currency of the locale', () => {
    expect(plain(formatAmount(990, 'eur', 'de-DE'))).toBe('9,90 €');
    expect(plain(formatAmount(129900, 'usd', 'en-US'))).toBe('$1,299.00');
  });

  it('falls back to the plain number for an unknown currency code', () => {
    expect(plain(formatAmount(1050, 'not-a-code', 'en-US'))).toBe('10.50 NOT-A-CODE');
  });

  it('labels every interval, single and multiple', () => {
    expect(intervalLabel('X', 'day')).toBe(m.billing_price_per_day({ price: 'X' }));
    expect(intervalLabel('X', 'week', null)).toBe(m.billing_price_per_week({ price: 'X' }));
    expect(intervalLabel('X', 'month', 1)).toBe(m.billing_price_per_month({ price: 'X' }));
    expect(intervalLabel('X', 'year')).toBe(m.billing_price_per_year({ price: 'X' }));
    expect(intervalLabel('X', 'day', 2)).toBe(m.billing_price_every_days({ price: 'X', count: 2 }));
    expect(intervalLabel('X', 'week', 2)).toBe(
      m.billing_price_every_weeks({ price: 'X', count: 2 }),
    );
    expect(intervalLabel('X', 'month', 3)).toBe(
      m.billing_price_every_months({ price: 'X', count: 3 }),
    );
    expect(intervalLabel('X', 'year', 2)).toBe(
      m.billing_price_every_years({ price: 'X', count: 2 }),
    );
    expect(intervalLabel('X', null)).toBeNull();
    expect(intervalLabel('X', 'fortnight')).toBeNull();
  });

  it('formats a plan price with its interval and the status currency as fallback', () => {
    expect(plain(formatPrice(price(), 'usd', 'de-DE'))).toBe(
      plain(m.billing_price_per_month({ price: formatAmount(990, 'eur', 'de-DE') })),
    );
    const fallback = formatPrice(price({ currency: null, interval: null }), 'usd', 'en-US');
    expect(plain(fallback)).toBe('$9.90');
  });

  it('says so when the price is not known', () => {
    expect(formatPrice(price({ unit_amount: null }), 'eur')).toBe(m.billing_price_unknown());
    expect(formatPrice(price({ currency: null }), '')).toBe(m.billing_price_unknown());
  });

  it('names a plan, the key when the name is missing', () => {
    const plan = STATUS.plans[0] as BillingPlan;
    expect(planTitle(plan)).toBe('Home');
    expect(planTitle({ ...plan, name: null })).toBe('home');
    expect(planTitle({ ...plan, name: '  ' })).toBe('home');
  });
});

describe('billing subscription and invoice labels', () => {
  const ALL: SubscriptionStatus[] = [
    'incomplete',
    'incomplete_expired',
    'trialing',
    'active',
    'past_due',
    'canceled',
    'unpaid',
    'paused',
  ];

  it('opens checkout only without a running subscription', () => {
    expect(hasOpenSubscription(null)).toBe(false);
    expect(hasOpenSubscription(undefined)).toBe(false);
    const open = ALL.filter((s) => hasOpenSubscription(subscription(s)));
    expect(open).toEqual(['incomplete', 'trialing', 'active', 'past_due', 'unpaid', 'paused']);
  });

  it('translates every subscription status', () => {
    const labels = ALL.map(subscriptionStatusLabel);
    expect(new Set(labels).size).toBe(ALL.length);
    expect(labels.every((label) => label.length > 0)).toBe(true);
  });

  it('translates the known invoice statuses and passes others through', () => {
    const known = ['paid', 'open', 'draft', 'void', 'uncollectible'].map(invoiceStatusLabel);
    expect(new Set(known).size).toBe(5);
    expect(invoiceStatusLabel('mystery')).toBe('mystery');
  });

  it('keeps only http(s) links of the provider', () => {
    expect(safeExternalUrl('https://invoice.stripe.com/i/1')).toBe(
      'https://invoice.stripe.com/i/1',
    );
    expect(safeExternalUrl('http://localhost:12111/x')).toBe('http://localhost:12111/x');
    expect(safeExternalUrl('javascript:alert(1)')).toBeNull();
    expect(safeExternalUrl('/relative')).toBeNull();
    expect(safeExternalUrl(null)).toBeNull();
    expect(safeExternalUrl('')).toBeNull();
  });
});

describe('BillingAccount', () => {
  function loaders(overrides: Partial<BillingLoaders> = {}): BillingLoaders {
    return {
      subscription: async () => ({ provider: 'stripe', has_customer: false, subscription: null }),
      invoices: async () => [],
      checkout: async (plan) => ({ url: `https://checkout.stripe.com/${plan}` }),
      portal: async () => ({ url: 'https://billing.stripe.com/p/1' }),
      ...overrides,
    };
  }

  it('loads the subscription and the invoices, newest invoice first', async () => {
    const account = new BillingAccount(
      loaders({
        subscription: async () => ({
          provider: 'stripe',
          has_customer: true,
          subscription: subscription('active'),
        }),
        invoices: async () => [
          invoice('A-1', '2026-08-01T00:00:00Z'),
          invoice('A-3', '2026-10-01T00:00:00Z'),
          invoice('A-2', '2026-09-01T00:00:00Z'),
        ],
      }),
    );
    await account.load();
    expect(account.loading).toBe(false);
    expect(account.summary?.has_customer).toBe(true);
    expect(account.invoices.map((i) => i.number)).toEqual(['A-3', 'A-2', 'A-1']);
    expect(account.problem).toBeNull();
    expect(account.invoicesProblem).toBeNull();
  });

  it('keeps the problems of the subscription and the invoices apart', async () => {
    const forbidden = new ApiProblem({ status: 403 });
    const account = new BillingAccount(
      loaders({
        subscription: async () => Promise.reject(forbidden),
        invoices: async () => Promise.reject(new Error('boom')),
      }),
    );
    await account.load();
    expect(account.problem).toBe(forbidden);
    expect(account.invoicesProblem?.status).toBe(0);
    expect(account.summary).toBeNull();
  });

  it('hands out the checkout and portal URLs', async () => {
    const account = new BillingAccount(loaders());
    expect(await account.checkoutUrl('fleet')).toBe('https://checkout.stripe.com/fleet');
    expect(await account.portalUrl()).toBe('https://billing.stripe.com/p/1');
  });
});
