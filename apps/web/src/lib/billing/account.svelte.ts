/**
 * The billing side of the signed-in account (owner, or a member granted `billing.manage`): the
 * subscription as the provider last reported it and the invoices, plus the two redirects
 * (Checkout for a first subscription, the customer portal afterwards). One instance per account
 * page; the caller only creates it when the probe found billing enabled.
 */
import { billingApi, unwrap } from '$lib/api/client';
import { asProblem, type ApiProblem } from '$lib/api/problem';

import type { Invoice, SubscriptionSummary } from './billing';

export interface BillingLoaders {
  subscription(): Promise<SubscriptionSummary>;
  invoices(): Promise<Invoice[]>;
  checkout(plan: string): Promise<{ url: string }>;
  portal(): Promise<{ url: string }>;
}

export const defaultBillingLoaders: BillingLoaders = {
  subscription: () => unwrap(billingApi.GET('/api/v1/billing/subscription')),
  invoices: () => unwrap(billingApi.GET('/api/v1/billing/invoices')),
  checkout: (plan) => unwrap(billingApi.POST('/api/v1/billing/checkout', { body: { plan } })),
  portal: () => unwrap(billingApi.POST('/api/v1/billing/portal')),
};

export class BillingAccount {
  summary = $state<SubscriptionSummary | null>(null);
  invoices = $state<Invoice[]>([]);
  loading = $state(false);
  /** The subscription could not be loaded. */
  problem = $state<ApiProblem | null>(null);
  /** The invoices could not be loaded (the subscription may still show). */
  invoicesProblem = $state<ApiProblem | null>(null);
  #loaders: BillingLoaders;

  constructor(loaders: BillingLoaders = defaultBillingLoaders) {
    this.#loaders = loaders;
  }

  async load(): Promise<void> {
    this.loading = true;
    const [summary, invoices] = await Promise.allSettled([
      this.#loaders.subscription(),
      this.#loaders.invoices(),
    ]);
    if (summary.status === 'fulfilled') {
      this.summary = summary.value;
      this.problem = null;
    } else {
      this.problem = asProblem(summary.reason);
    }
    if (invoices.status === 'fulfilled') {
      this.invoices = [...invoices.value].sort((a, b) => b.issued_at.localeCompare(a.issued_at));
      this.invoicesProblem = null;
    } else {
      this.invoicesProblem = asProblem(invoices.reason);
    }
    this.loading = false;
  }

  /** The URL of a Checkout session for `plan`; throws the problem (409: use the portal). */
  async checkoutUrl(plan: string): Promise<string> {
    return (await this.#loaders.checkout(plan)).url;
  }

  /** The URL of the customer portal; throws the problem (404 before the first checkout). */
  async portalUrl(): Promise<string> {
    return (await this.#loaders.portal()).url;
  }
}
