// SPDX-FileCopyrightText: 2026 Lukas KÃ¶rber
// SPDX-License-Identifier: AGPL-3.0-only

import { render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it } from 'vitest';

import { BillingAccount, type BillingLoaders } from '#lib/billing/account.svelte.js';
import type { BillingStatus } from '#lib/billing/billing.js';
import { m } from '#lib/paraglide/messages.js';
import { setLocale } from '#lib/paraglide/runtime.js';

import BillingPanel from './BillingPanel.svelte';

beforeEach(() => setLocale('en', { reload: false }));

const STATUS: BillingStatus = {
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

function account(hasCustomer: boolean): BillingAccount {
  const loaders: BillingLoaders = {
    subscription: async () => ({
      provider: 'stripe',
      has_customer: hasCustomer,
      subscription: hasCustomer
        ? {
            plan: 'home',
            status: 'active',
            plan_status: 'active',
            current_period_end: '2026-12-10T00:00:00Z',
            trial_end: null,
            grace_until: null,
            cancel_at_period_end: false,
            updated_at: '2026-10-10T00:00:00Z',
          }
        : null,
    }),
    invoices: async () => [
      {
        number: 'INV-1',
        status: 'paid',
        currency: 'eur',
        amount_due: 1250,
        amount_paid: 1250,
        issued_at: '2026-10-10T00:00:00Z',
        hosted_url: 'https://invoice.stripe.com/i/1',
        pdf_url: 'https://invoice.stripe.com/i/1/pdf',
      },
    ],
    checkout: async () => ({ url: 'https://checkout.stripe.com/x' }),
    portal: async () => ({ url: 'https://billing.stripe.com/p/1' }),
  };
  return new BillingAccount(loaders);
}

describe('BillingPanel in a browser', () => {
  it('offers the purchase, the portal and the invoice links', async () => {
    render(BillingPanel, {
      props: { status: STATUS, currentPlan: null, purchasable: true, account: account(true) },
    });
    await waitFor(() => expect(screen.getByTestId('billing-status')).toBeTruthy());
    expect(screen.getByTestId('billing-portal')).toBeTruthy();
    expect(screen.getByTestId('billing-plan-home').textContent).toContain('9');
    expect(screen.queryByTestId('billing-managed-on-web')).toBeNull();
    expect(screen.getAllByRole('link').length).toBeGreaterThan(0);
  });

  it('shows Upgrade while the account has no subscription', async () => {
    render(BillingPanel, {
      props: { status: STATUS, currentPlan: null, purchasable: true, account: account(false) },
    });
    await waitFor(() => expect(screen.getByTestId('billing-upgrade-home')).toBeTruthy());
  });
});

describe('BillingPanel inside a native app shell', () => {
  it('keeps the plan state and replaces every action by one sentence without a link or a price', async () => {
    const { container } = render(BillingPanel, {
      props: { status: STATUS, currentPlan: 'home', purchasable: false, account: account(true) },
    });
    await waitFor(() => expect(screen.getByTestId('billing-status')).toBeTruthy());
    expect(screen.getByTestId('billing-status').dataset.status).toBe('active');
    const note = screen.getByTestId('billing-managed-on-web');
    expect(note.textContent).toBe(m.billing_managed_on_web());
    expect(note.querySelector('a')).toBeNull();
    expect(screen.queryByTestId('billing-plan-home')).toBeNull();
    expect(screen.queryByTestId('billing-upgrade-home')).toBeNull();
    expect(screen.queryByTestId('billing-portal')).toBeNull();
    expect(screen.queryByRole('link')).toBeNull();
    expect(container.textContent).not.toMatch(/â‚¬|EUR|9[.,]90/);
  });

  it('shows no Upgrade without a subscription either', async () => {
    render(BillingPanel, {
      props: { status: STATUS, currentPlan: null, purchasable: false, account: account(false) },
    });
    await waitFor(() => expect(screen.getByTestId('billing-status')).toBeTruthy());
    expect(screen.queryByTestId('billing-upgrade-home')).toBeNull();
    expect(screen.getByTestId('billing-managed-on-web')).toBeTruthy();
  });
});
