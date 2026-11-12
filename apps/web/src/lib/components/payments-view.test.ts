// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiProblem } from '#lib/api/problem.js';
import type { DeletionStats, ManualRefund, PaymentsSummary } from '#lib/operator/payments.js';
import { PaymentsDashboard, type PaymentsLoaders } from '#lib/operator/payments.svelte.js';
import { setLocale } from '#lib/paraglide/runtime.js';

import PaymentsView from './PaymentsView.svelte';

beforeEach(() => setLocale('en', { reload: false }));

const MONTHS = ['2026-10', '2026-11'];

function summary(overrides: Partial<PaymentsSummary> = {}): PaymentsSummary {
  return {
    generated_at: '2026-11-12T09:00:00Z',
    time_zone: 'UTC',
    months: MONTHS,
    since: '2026-10-01T00:00:00Z',
    currencies: [{ code: 'eur', exponent: 2 }],
    revenue: [
      {
        currency: 'eur',
        gross: 2168,
        refunded: 178,
        net: 1990,
        months: [
          {
            month: '2026-10',
            gross: 990,
            tax: 158,
            refunded: 0,
            net: 990,
            invoices: 1,
            refunds: 0,
          },
          {
            month: '2026-11',
            gross: 1178,
            tax: 188,
            refunded: 178,
            net: 1000,
            invoices: 1,
            refunds: 1,
          },
        ],
      },
    ],
    subscriptions: {
      plans: [{ plan: 'home', trialing: 2, active: 7, past_due: 1 }],
      mrr: [{ currency: 'eur', amount: 7920, subscriptions: 8 }],
      unpriced: 0,
      flow: [
        { month: '2026-10', started: 3, ended: 0 },
        { month: '2026-11', started: 1, ended: 1 },
      ],
    },
    refunds: [{ month: '2026-11', currency: 'eur', reason: 'erasure', count: 1, amount: 178 }],
    problems: {
      failures: [{ month: '2026-11', currency: 'eur', count: 2, amount: 1980 }],
      recent_failures: [
        { at: '2026-11-03T08:00:00Z', currency: 'eur', amount: 990, plan: 'home', attempt: 2 },
      ],
      invoices: [
        { currency: 'eur', open_count: 1, open_amount: 990, overdue_count: 1, overdue_amount: 990 },
      ],
      past_due_subscriptions: 1,
      disputes: [{ month: '2026-11', currency: 'eur', opened: 1, amount: 1178, lost: 0 }],
      open_disputes: [
        {
          opened_at: '2026-11-02T00:00:00Z',
          currency: 'eur',
          amount: 1178,
          status: 'needs_response',
          reason: 'fraudulent',
          due_by: '2026-11-20T00:00:00Z',
          closed_at: null,
        },
      ],
      closed_disputes: [],
    },
    ...overrides,
  };
}

const EMPTY: PaymentsSummary = summary({
  since: null,
  currencies: [],
  revenue: [],
  subscriptions: {
    plans: [],
    mrr: [],
    unpriced: 0,
    flow: MONTHS.map((month) => ({ month, started: 0, ended: 0 })),
  },
  refunds: [],
  problems: {
    failures: [],
    recent_failures: [],
    invoices: [],
    past_due_subscriptions: 0,
    disputes: [],
    open_disputes: [],
    closed_disputes: [],
  },
});

const DELETIONS: DeletionStats = {
  time_zone: 'UTC',
  months: [
    { month: '2026-10', scheduled: 2, restored: 1, completed: 0 },
    { month: '2026-11', scheduled: 1, restored: 0, completed: 1 },
  ],
  in_grace: 1,
  purging: 0,
};

const OPEN: ManualRefund = {
  id: '6f0f6c1e-0000-4000-8000-000000000001',
  payment_ref: 'pi_123',
  invoice_ref: 'in_456',
  amount: 600,
  currency: 'eur',
  reason: 'the payment is too old to refund',
  created_at: '2026-11-01T00:00:00Z',
  settled_at: null,
};

function loaders(overrides: Partial<PaymentsLoaders> = {}): PaymentsLoaders {
  return {
    summary: vi.fn(async () => summary()),
    deletions: vi.fn(async () => DELETIONS),
    refunds: vi.fn(async () => [OPEN]),
    settle: vi.fn(async (id: string) => ({ ...OPEN, id, settled_at: '2026-11-12T10:00:00Z' })),
    ...overrides,
  };
}

async function shown(billing: boolean, given: PaymentsLoaders) {
  const dashboard = new PaymentsDashboard(given);
  if (billing) await dashboard.load(12);
  render(PaymentsView, { props: { billing, dashboard, locale: 'en' } });
  return dashboard;
}

describe('PaymentsView', () => {
  it('says so and loads nothing where Billing does not run', async () => {
    const given = loaders();
    await shown(false, given);
    expect(screen.getByTestId('payments-billing-off')).toBeTruthy();
    expect(screen.queryByTestId('payments-dashboard')).toBeNull();
    expect(screen.queryByTestId('payments-window')).toBeNull();
    expect(given.summary).not.toHaveBeenCalled();
  });

  it('renders every section with money per currency and the table fallbacks', async () => {
    const given = loaders();
    await shown(true, given);
    expect(given.summary).toHaveBeenCalledWith(12);
    expect(screen.getByTestId('payments-kpi-mrr').textContent).toContain('€79.20');
    expect(screen.getByTestId('payments-kpi-active').textContent).toContain('8');
    expect(screen.getByTestId('payments-kpi-net').textContent).toContain('€10.00');
    expect(screen.getByTestId('payments-kpi-refunds').dataset.value).toBe('1');
    expect(screen.getByTestId('payments-since').textContent).toContain('Recorded since');

    const table = screen.getByTestId('payments-revenue-table-eur');
    expect(table.tagName).toBe('DETAILS');
    const rows = within(table).getAllByRole('row');
    expect(rows[1]?.textContent).toContain('Nov 26');
    expect(rows[1]?.textContent).toContain('€11.78');
    expect(rows[1]?.textContent).toContain('€1.78');
    // The chart has a text summary for assistive technology.
    const chart = screen.getByTestId('payments-revenue-eur').querySelector('svg');
    expect(chart?.getAttribute('aria-label')).toContain('€19.90');

    expect(screen.getByTestId('payments-plan-home').textContent).toContain('7');
    expect(screen.getByTestId('payments-refunds-table').textContent).toContain('Account deletion');
    expect(screen.getByTestId('payments-past-due').dataset.value).toBe('1');
    expect(screen.getByTestId('payments-open-invoices').textContent).toContain('1 overdue');
    expect(screen.getByTestId('dispute-open-0').dataset.status).toBe('needs_response');
    expect(screen.getByTestId('dispute-open-0').textContent).toContain('Needs a response');
    expect(screen.getByTestId('payments-deletions-table').textContent).toContain('Nov 26');
    expect(screen.getByTestId('payments-deletions-now').textContent).toContain('1 in the grace');
  });

  it('shows empty states without data', async () => {
    await shown(
      true,
      loaders({ summary: vi.fn(async () => EMPTY), refunds: vi.fn(async () => []) }),
    );
    expect(screen.getByTestId('payments-since').textContent).toContain('No payments recorded yet');
    expect(screen.getByTestId('payments-revenue-empty')).toBeTruthy();
    expect(screen.getByTestId('payments-refunds-empty')).toBeTruthy();
    expect(screen.getByTestId('payments-manual-none')).toBeTruthy();
    expect(screen.getByTestId('payments-failed-none')).toBeTruthy();
    expect(screen.getByTestId('payments-disputes-none')).toBeTruthy();
    expect(screen.getByTestId('payments-kpi-mrr').textContent).toContain('None');
  });

  it('keeps the dashboard when Identity cannot answer', async () => {
    await shown(
      true,
      loaders({
        deletions: vi.fn(async () => {
          throw ApiProblem.network();
        }),
      }),
    );
    expect(screen.getByTestId('payments-revenue')).toBeTruthy();
    const section = screen.getByTestId('payments-deletions');
    expect(within(section).getByRole('alert')).toBeTruthy();
    expect(screen.queryByTestId('payments-deletions-table')).toBeNull();
  });

  it('asks before marking a manual refund as paid back', async () => {
    const given = loaders();
    await shown(true, given);
    await fireEvent.click(screen.getByTestId(`settle-${OPEN.id}`));
    const question = screen.getByTestId(`settle-confirm-${OPEN.id}`);
    expect(question.textContent).toContain('€6.00');
    expect(question.textContent).toContain('pi_123');
    expect(given.settle).not.toHaveBeenCalled();

    await fireEvent.click(within(question).getByText('Cancel'));
    expect(screen.queryByTestId(`settle-confirm-${OPEN.id}`)).toBeNull();
    expect(given.settle).not.toHaveBeenCalled();

    await fireEvent.click(screen.getByTestId(`settle-${OPEN.id}`));
    await fireEvent.click(screen.getByTestId(`settle-yes-${OPEN.id}`));
    await waitFor(() => expect(given.settle).toHaveBeenCalledWith(OPEN.id));
    await waitFor(() => expect(screen.getByTestId('payments-manual-settled')).toBeTruthy());
    expect(screen.queryByTestId(`manual-refund-${OPEN.id}`)).toBeNull();
    // The summary is read again: the ledger now counts the refund.
    await waitFor(() => expect(given.summary).toHaveBeenCalledTimes(2));
  });

  it('keeps the question open with the problem when settling fails', async () => {
    const given = loaders({
      settle: vi.fn(async () => {
        throw ApiProblem.network();
      }),
    });
    await shown(true, given);
    await fireEvent.click(screen.getByTestId(`settle-${OPEN.id}`));
    await fireEvent.click(screen.getByTestId(`settle-yes-${OPEN.id}`));
    const question = await screen.findByTestId(`settle-confirm-${OPEN.id}`);
    await waitFor(() => expect(within(question).getByRole('alert')).toBeTruthy());
    expect(screen.getByTestId(`manual-refund-${OPEN.id}`)).toBeTruthy();
  });

  it('loads another window', async () => {
    const given = loaders();
    await shown(true, given);
    const window = screen.getByTestId('payments-window');
    await fireEvent.click(within(window).getByText('24 mo.'));
    await waitFor(() => expect(given.summary).toHaveBeenLastCalledWith(24));
    expect(given.deletions).toHaveBeenLastCalledWith(24);
  });
});
