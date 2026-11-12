// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { beforeEach, describe, expect, it } from 'vitest';

import { setLocale } from '#lib/paraglide/runtime.js';
import type { Plan } from '#lib/plan/types.js';

import {
  checkPlanPrices,
  currenciesOf,
  disputeStatusLabel,
  disputeTone,
  exponentOf,
  formatMoney,
  hasPriceIssues,
  moneyFormatter,
  monthLabel,
  monthLong,
  perMonth,
  refundRows,
  splitRefunds,
  type ManualRefund,
  type OperatorPrice,
} from './payments';
import { consoleTabs, currentTab } from './tabs';

beforeEach(() => setLocale('en', { reload: false }));

// Intl uses narrow no-break spaces in some locales: compare with plain spaces.
const plain = (text: string) => text.replace(/[\u00a0\u202f]/g, ' ');

describe('money', () => {
  it('takes the exponent from the answer, else the locale with Stripe exceptions', () => {
    const known = [
      { code: 'eur', exponent: 2 },
      { code: 'jpy', exponent: 0 },
    ];
    expect(exponentOf('EUR', known)).toBe(2);
    expect(exponentOf('jpy', known)).toBe(0);
    expect(exponentOf('kwd')).toBe(3);
    expect(exponentOf('isk')).toBe(2); // Stripe writes ISK with two decimals
    expect(exponentOf('ugx')).toBe(2);
    expect(exponentOf('xyz')).toBe(2);
  });

  it('formats provider amounts per currency and locale', () => {
    expect(plain(formatMoney(117800, 'eur', 2, 'de-DE'))).toBe('1.178,00 €');
    expect(formatMoney(990, 'eur', 2, 'en')).toBe('€9.90');
    expect(formatMoney(1000, 'jpy', 0, 'en')).toBe('¥1,000');
    expect(plain(formatMoney(1500, 'kwd', 3, 'en'))).toBe('KWD 1.500');
    expect(plain(formatMoney(100000, 'isk', 2, 'en'))).toBe('ISK 1,000');
    expect(formatMoney(-450, 'eur', 2, 'en')).toBe('-€4.50');
    const money = moneyFormatter([{ code: 'eur', exponent: 2 }], 'en');
    expect(money(990, 'eur')).toBe('€9.90');
    expect(money(5, null)).toBe('5');
  });
});

describe('months and tables', () => {
  it('labels months', () => {
    expect(monthLabel('2026-11', 'en')).toBe('Nov 26');
    expect(monthLong('2026-01', 'de')).toBe('Januar 2026');
  });

  it('puts both refund reasons side by side, newest month first', () => {
    const rows = refundRows([
      { month: '2026-10', currency: 'eur', reason: 'erasure', count: 2, amount: 1000 },
      { month: '2026-10', currency: 'eur', reason: 'other', count: 1, amount: 990 },
      { month: '2026-11', currency: 'usd', reason: 'other', count: 1, amount: 1000 },
    ]);
    expect(rows).toEqual([
      {
        month: '2026-11',
        currency: 'usd',
        erasure: { count: 0, amount: 0 },
        other: { count: 1, amount: 1000 },
      },
      {
        month: '2026-10',
        currency: 'eur',
        erasure: { count: 2, amount: 1000 },
        other: { count: 1, amount: 990 },
      },
    ]);
  });

  it('fills every month of the window for one currency', () => {
    const rows = [
      { month: '2026-10', currency: 'eur', count: 2 },
      { month: '2026-10', currency: 'usd', count: 7 },
      { month: '2026-11', currency: 'eur', count: 1 },
    ];
    expect(perMonth(rows, ['2026-09', '2026-10', '2026-11'], 'eur', (r) => r.count)).toEqual([
      0, 2, 1,
    ]);
    expect(currenciesOf(rows)).toEqual(['eur', 'usd']);
  });

  it('lists open manual refunds oldest first and settled ones newest first', () => {
    const refund = (id: string, created: string, settled: string | null): ManualRefund => ({
      id,
      payment_ref: `pi_${id}`,
      invoice_ref: null,
      amount: 600,
      currency: 'eur',
      reason: 'refused',
      created_at: created,
      settled_at: settled,
    });
    const { open, settled } = splitRefunds([
      refund('b', '2026-11-02T00:00:00Z', null),
      refund('a', '2026-11-01T00:00:00Z', null),
      refund('c', '2026-10-01T00:00:00Z', '2026-11-03T00:00:00Z'),
      refund('d', '2026-10-01T00:00:00Z', '2026-11-05T00:00:00Z'),
    ]);
    expect(open.map((r) => r.id)).toEqual(['a', 'b']);
    expect(settled.map((r) => r.id)).toEqual(['d', 'c']);
  });

  it('names dispute statuses with a tone', () => {
    expect(disputeTone('needs_response')).toBe('error');
    expect(disputeTone('warning_under_review')).toBe('paused');
    expect(disputeTone('lost')).toBe('docked');
    expect(disputeTone('charge_refunded')).toBe('unknown');
    expect(disputeStatusLabel('needs_response')).toBe('Needs a response');
    expect(disputeStatusLabel('something_new')).toBe('something_new');
  });
});

describe('plans and prices', () => {
  const plan = (key: string, isDefault = false): Plan =>
    ({
      key,
      name: key,
      description: null,
      limits: {},
      features: [],
      trial_days: 0,
      is_default: isDefault,
      active: true,
    }) as unknown as Plan;
  const price = (planKey: string, amount: number | null): OperatorPrice => ({
    plan: planKey,
    price_id: `price_${planKey}`,
    currency: amount === null ? null : 'eur',
    unit_amount: amount,
    interval: amount === null ? null : 'month',
    interval_count: amount === null ? null : 1,
    exponent: amount === null ? null : 2,
    monthly_amount: amount,
  });

  it('marks priced, unreadable, missing and free plans and prices without a plan', () => {
    const check = checkPlanPrices(
      [plan('free', true), plan('home'), plan('fleet'), plan('pro')],
      [price('home', 990), price('fleet', null), price('legacy', 500)],
    );
    expect(Object.fromEntries(Object.entries(check.byPlan).map(([k, v]) => [k, v.state]))).toEqual({
      free: 'free',
      home: 'priced',
      fleet: 'unreadable',
      pro: 'missing',
    });
    expect(check.orphans.map((p) => p.plan)).toEqual(['legacy']);
    expect(hasPriceIssues(check)).toBe(true);
    expect(hasPriceIssues(checkPlanPrices([plan('home')], [price('home', 990)]))).toBe(false);
  });
});

describe('console tabs', () => {
  it('shows Payments only where Billing runs, between Plans and System', () => {
    expect(consoleTabs(false).map((t) => t.id)).toEqual(['accounts', 'plans', 'system']);
    expect(consoleTabs(true).map((t) => t.id)).toEqual(['accounts', 'plans', 'payments', 'system']);
    expect(consoleTabs(true)[2]?.label()).toBe('Payments');
  });

  it('finds the tab of a path', () => {
    expect(currentTab('/operator')).toBe('accounts');
    expect(currentTab('/operator/accounts/1')).toBe('accounts');
    expect(currentTab('/operator/plans/edit')).toBe('plans');
    expect(currentTab('/operator/payments')).toBe('payments');
    expect(currentTab('/operator/system')).toBe('system');
  });
});
