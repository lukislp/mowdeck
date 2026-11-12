// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * The payments dashboard of the operator console (ADR 0083): types of the Billing and Identity
 * answers and the pure presentation rules. Billing aggregates (per UTC month and currency, never
 * across currencies); this module only formats, groups for tables and compares the plans with
 * the configured prices.
 *
 * Money: Billing sends amounts in the provider's smallest unit and, per currency, the exponent of
 * that unit (Stripe's rules: 2 for EUR and for ISK, 0 for JPY, 3 for KWD). The exponent from the
 * answer wins; only a currency the answer does not list (a manual refund) falls back to the
 * locale's digits, with Stripe's two-decimal exceptions ISK and UGX.
 */
import type { components as BillingSchemas } from '#lib/generated/billing.js';
import type { components as IdentitySchemas } from '#lib/generated/identity.js';
import { m } from '#lib/paraglide/messages.js';
import type { Plan } from '#lib/plan/types.js';

type B = BillingSchemas['schemas'];
export type PaymentsSummary = B['PaymentsSummaryOut'];
export type CurrencyRevenue = B['CurrencyRevenueOut'];
export type RefundMonth = B['RefundMonthOut'];
export type Dispute = B['DisputeOut'];
export type FailedPayment = B['FailedPaymentOut'];
export type OperatorPrices = B['OperatorPricesOut'];
export type OperatorPrice = B['OperatorPriceOut'];
export type ManualRefund = B['ManualRefundOut'];
export type CurrencyInfo = B['CurrencyOut'];
export type DeletionStats = IdentitySchemas['schemas']['DeletionStatsOut'];

export const PAYMENTS_URL = '/api/v1/billing/operator/payments';
export const WINDOWS = [6, 12, 24] as const;
export type WindowMonths = (typeof WINDOWS)[number];

/** Currencies Stripe writes with two decimals although they have none (zero-decimal by ISO). */
const TWO_DECIMAL_EXCEPTIONS = new Set(['isk', 'ugx']);

/** The exponent of a currency's provider amount: from Billing's answer, else the fallback. */
export function exponentOf(currency: string, known: readonly CurrencyInfo[] = []): number {
  const code = currency.toLowerCase();
  const listed = known.find((c) => c.code === code);
  if (listed) return listed.exponent;
  if (TWO_DECIMAL_EXCEPTIONS.has(code)) return 2;
  try {
    const digits = new Intl.NumberFormat('en', {
      style: 'currency',
      currency: code.toUpperCase(),
    }).resolvedOptions().maximumFractionDigits;
    return digits ?? 2;
  } catch {
    return 2;
  }
}

/** A provider amount as localized money ("1.178,00 €", "¥1,000"). */
export function formatMoney(
  amount: number,
  currency: string,
  exponent: number,
  locale: string,
): string {
  const value = amount / 10 ** exponent;
  const code = currency.toUpperCase();
  try {
    return new Intl.NumberFormat(locale, { style: 'currency', currency: code }).format(value);
  } catch {
    const plain = new Intl.NumberFormat(locale, {
      minimumFractionDigits: exponent,
      maximumFractionDigits: exponent,
    }).format(value);
    return `${plain} ${code}`;
  }
}

/** A formatter bound to the currencies of one answer and a locale. */
export function moneyFormatter(
  known: readonly CurrencyInfo[],
  locale: string,
): (amount: number, currency: string | null | undefined) => string {
  return (amount, currency) =>
    currency ? formatMoney(amount, currency, exponentOf(currency, known), locale) : String(amount);
}

function monthDate(month: string): Date {
  const [year, index] = month.split('-').map(Number);
  return new Date(Date.UTC(year ?? 1970, (index ?? 1) - 1, 1));
}

/** "Nov 26" (axis and table rows); the month is a UTC calendar month. */
export function monthLabel(month: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, {
    month: 'short',
    year: '2-digit',
    timeZone: 'UTC',
  }).format(monthDate(month));
}

/** "November 2026". */
export function monthLong(month: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, {
    month: 'long',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(monthDate(month));
}

export const sum = (values: readonly number[]): number => values.reduce((a, b) => a + b, 0);

/** The months of one currency, newest first, for the table under the chart. */
export function revenueRows(revenue: CurrencyRevenue): CurrencyRevenue['months'] {
  return [...revenue.months].reverse();
}

export interface RefundRow {
  month: string;
  currency: string;
  erasure: { count: number; amount: number };
  other: { count: number; amount: number };
}

/** Refunds per month and currency with both reasons side by side, newest month first. */
export function refundRows(refunds: readonly RefundMonth[]): RefundRow[] {
  const rows = new Map<string, RefundRow>();
  for (const r of refunds) {
    const key = `${r.month}|${r.currency}`;
    const row = rows.get(key) ?? {
      month: r.month,
      currency: r.currency,
      erasure: { count: 0, amount: 0 },
      other: { count: 0, amount: 0 },
    };
    const slot = r.reason === 'erasure' ? row.erasure : row.other;
    slot.count += r.count;
    slot.amount += r.amount;
    rows.set(key, row);
  }
  return [...rows.values()].sort(
    (a, b) => b.month.localeCompare(a.month) || a.currency.localeCompare(b.currency),
  );
}

/** One value per month of the window for a currency (zeros where nothing happened). */
export function perMonth<T extends { month: string; currency: string }>(
  rows: readonly T[],
  months: readonly string[],
  currency: string,
  value: (row: T) => number,
): number[] {
  return months.map((month) =>
    sum(rows.filter((r) => r.month === month && r.currency === currency).map(value)),
  );
}

/** The currencies that appear in rows, sorted. */
export function currenciesOf(rows: readonly { currency: string | null }[]): string[] {
  return [...new Set(rows.map((r) => r.currency).filter((c): c is string => !!c))].sort();
}

/** Open manual refunds first (oldest first), then the settled ones (newest settled first). */
export function splitRefunds(refunds: readonly ManualRefund[]): {
  open: ManualRefund[];
  settled: ManualRefund[];
} {
  const open = refunds
    .filter((r) => !r.settled_at)
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
  const settled = refunds
    .filter((r) => !!r.settled_at)
    .sort((a, b) => (b.settled_at ?? '').localeCompare(a.settled_at ?? ''));
  return { open, settled };
}

export type DisputeTone = 'error' | 'paused' | 'docked' | 'unknown';

/** Needs a response from the operator: error; under review: paused; decided: docked. */
export function disputeTone(status: string | null): DisputeTone {
  switch (status) {
    case 'needs_response':
    case 'warning_needs_response':
      return 'error';
    case 'under_review':
    case 'warning_under_review':
      return 'paused';
    case 'won':
    case 'lost':
    case 'warning_closed':
    case 'prevented':
      return 'docked';
    default:
      return 'unknown';
  }
}

/** Stripe's dispute status in words; an unknown one as sent. */
export function disputeStatusLabel(status: string | null): string {
  switch (status) {
    case 'needs_response':
    case 'warning_needs_response':
      return m.op_pay_dispute_needs_response();
    case 'under_review':
    case 'warning_under_review':
      return m.op_pay_dispute_under_review();
    case 'won':
      return m.op_pay_dispute_won();
    case 'lost':
      return m.op_pay_dispute_lost();
    case 'warning_closed':
      return m.op_pay_dispute_closed();
    case 'prevented':
      return m.op_pay_dispute_prevented();
    default:
      return status ?? '';
  }
}

export function refundReasonLabel(reason: 'erasure' | 'other'): string {
  return reason === 'erasure' ? m.op_pay_reason_erasure() : m.op_pay_reason_other();
}

// --- plans and prices ----------------------------------------------------------------------

export type PlanPriceState =
  | { state: 'priced'; price: OperatorPrice }
  | { state: 'unreadable'; price: OperatorPrice }
  | { state: 'missing' }
  | { state: 'free' };

export interface PlanPriceCheck {
  byPlan: Record<string, PlanPriceState>;
  /** Prices in the price file whose plan Entitlements does not offer. */
  orphans: OperatorPrice[];
}

/**
 * Compare the offered plans with the configured prices: a priced plan, a price Stripe did not
 * answer for (`unreadable`), an offered plan without a price (`missing`, except the default
 * plan, which accounts get without paying: `free`), and prices whose plan is unknown.
 */
export function checkPlanPrices(
  plans: readonly Plan[],
  prices: readonly OperatorPrice[],
): PlanPriceCheck {
  const byKey = new Map(prices.map((p) => [p.plan, p]));
  const byPlan: Record<string, PlanPriceState> = {};
  for (const plan of plans) {
    const price = byKey.get(plan.key);
    if (price) {
      byPlan[plan.key] =
        price.unit_amount === null || !price.currency
          ? { state: 'unreadable', price }
          : { state: 'priced', price };
    } else {
      byPlan[plan.key] = plan.is_default ? { state: 'free' } : { state: 'missing' };
    }
  }
  const offered = new Set(plans.map((p) => p.key));
  return { byPlan, orphans: prices.filter((p) => !offered.has(p.plan)) };
}

/** Whether any plan or price needs the operator's attention. */
export function hasPriceIssues(check: PlanPriceCheck): boolean {
  return (
    check.orphans.length > 0 ||
    Object.values(check.byPlan).some((s) => s.state === 'missing' || s.state === 'unreadable')
  );
}
