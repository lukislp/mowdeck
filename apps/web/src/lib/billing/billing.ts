/**
 * Billing in the web app: whether this installation sells subscriptions at all (the probe of
 * `GET /api/v1/billing/status`) and the formatting of prices, intervals and statuses.
 *
 * The Billing service only runs when billing is enabled (Compose profile `billing`). Without it
 * the gateway answers 404, or the SPA fallback answers with HTML and status 200. Every answer
 * that is not JSON with `enabled: true` therefore means "billing disabled": the app then shows
 * no billing element at all.
 */
import type { components } from '$lib/generated/billing';
import { m } from '$lib/paraglide/messages';
import { getLocale } from '$lib/paraglide/runtime';

export type BillingStatus = components['schemas']['BillingStatusOut'];
export type BillingPlan = components['schemas']['StatusPlanOut'];
export type BillingPrice = components['schemas']['PriceOut'];
export type SubscriptionSummary = components['schemas']['SubscriptionSummaryOut'];
export type Subscription = components['schemas']['SubscriptionOut'];
export type SubscriptionStatus = components['schemas']['SubscriptionStatus'];
export type Invoice = components['schemas']['InvoiceOut'];

export const BILLING_STATUS_URL = '/api/v1/billing/status';

/** Statuses after which a new checkout is possible (the subscription is over). */
const CLOSED: readonly SubscriptionStatus[] = ['canceled', 'incomplete_expired'];

/** Validate an untrusted status body; null unless it says `enabled: true` with a plan list. */
export function parseBillingStatus(raw: unknown): BillingStatus | null {
  if (!raw || typeof raw !== 'object') return null;
  const body = raw as Partial<BillingStatus>;
  if (body.enabled !== true || !Array.isArray(body.plans)) return null;
  const plans = body.plans.filter(
    (plan): plan is BillingPlan =>
      !!plan &&
      typeof plan === 'object' &&
      typeof plan.key === 'string' &&
      !!plan.price &&
      typeof plan.price === 'object',
  );
  return {
    enabled: true,
    provider: typeof body.provider === 'string' ? body.provider : '',
    currency: typeof body.currency === 'string' ? body.currency : '',
    automatic_tax: body.automatic_tax === true,
    plans,
  };
}

let pending: Promise<BillingStatus | null> | null = null;

/** Probe `GET /api/v1/billing/status` once per page load; never rejects. Null: billing is off. */
export function probeBilling(
  fetchFn: typeof fetch = (input, init) => fetch(input, init),
): Promise<BillingStatus | null> {
  pending ??= fetchFn(BILLING_STATUS_URL, {
    cache: 'no-cache',
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  })
    .then(async (response) => {
      if (!response.ok) return null;
      const type = response.headers.get('content-type') ?? '';
      if (!type.includes('json')) return null; // the SPA fallback (index.html with 200)
      return parseBillingStatus(await response.json());
    })
    .catch(() => null);
  return pending;
}

/** Forget the cached probe (tests). */
export function resetBillingProbe(): void {
  pending = null;
}

/** The subscription still runs (or is being set up): checkout is closed, the portal is the way. */
export function hasOpenSubscription(subscription: Subscription | null | undefined): boolean {
  return !!subscription && !CLOSED.includes(subscription.status);
}

/** An amount in the smallest currency unit (cents) as a localized currency string. */
export function formatAmount(cents: number, currency: string, locale: string = getLocale()) {
  const code = currency.toUpperCase();
  try {
    return new Intl.NumberFormat(locale, { style: 'currency', currency: code }).format(cents / 100);
  } catch {
    // An unknown currency code: plain number with the code.
    return `${new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(cents / 100)} ${code}`;
  }
}

/** "€9.90 per month", "€29.70 every 3 months"; null for a one-time price or an unknown interval. */
export function intervalLabel(
  price: string,
  interval: string | null,
  count: number | null = 1,
): string | null {
  const n = count && count > 1 ? count : 1;
  switch (interval) {
    case 'day':
      return n === 1
        ? m.billing_price_per_day({ price })
        : m.billing_price_every_days({ price, count: n });
    case 'week':
      return n === 1
        ? m.billing_price_per_week({ price })
        : m.billing_price_every_weeks({ price, count: n });
    case 'month':
      return n === 1
        ? m.billing_price_per_month({ price })
        : m.billing_price_every_months({ price, count: n });
    case 'year':
      return n === 1
        ? m.billing_price_per_year({ price })
        : m.billing_price_every_years({ price, count: n });
    default:
      return null;
  }
}

/** The price of a plan with its interval ("9,90 € pro Monat"); fallback currency from the status. */
export function formatPrice(
  price: BillingPrice,
  fallbackCurrency: string,
  locale: string = getLocale(),
): string {
  const currency = price.currency || fallbackCurrency;
  if (price.unit_amount === null || !currency) return m.billing_price_unknown();
  const amount = formatAmount(price.unit_amount, currency, locale);
  return intervalLabel(amount, price.interval, price.interval_count) ?? amount;
}

/** Name of a plan on sale, the key when Entitlements did not answer with one. */
export function planTitle(plan: BillingPlan): string {
  return plan.name?.trim() || plan.key;
}

export function subscriptionStatusLabel(status: SubscriptionStatus): string {
  switch (status) {
    case 'trialing':
      return m.billing_sub_trialing();
    case 'active':
      return m.billing_sub_active();
    case 'past_due':
      return m.billing_sub_past_due();
    case 'canceled':
      return m.billing_sub_canceled();
    case 'unpaid':
      return m.billing_sub_unpaid();
    case 'paused':
      return m.billing_sub_paused();
    case 'incomplete':
      return m.billing_sub_incomplete();
    case 'incomplete_expired':
      return m.billing_sub_incomplete_expired();
  }
}

/** Stripe's invoice statuses translated; anything else as sent. */
export function invoiceStatusLabel(status: string): string {
  switch (status) {
    case 'paid':
      return m.billing_invoice_paid();
    case 'open':
      return m.billing_invoice_open();
    case 'draft':
      return m.billing_invoice_draft();
    case 'void':
      return m.billing_invoice_void();
    case 'uncollectible':
      return m.billing_invoice_uncollectible();
    default:
      return status;
  }
}

/** Only absolute http(s) links of the provider become anchors (hosted invoice, PDF). */
export function safeExternalUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.href : null;
  } catch {
    return null;
  }
}
