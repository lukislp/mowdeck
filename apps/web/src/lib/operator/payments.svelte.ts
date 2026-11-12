// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * The state of the payments dashboard: Billing's summary, Identity's account deletions and the
 * manual refunds, each loaded on its own (one failing leaves the others on screen), and the
 * "paid back" action. One instance per page; the page creates it only where Billing runs.
 */
import { billingApi, identityApi, unwrap } from '#lib/api/client.js';
import { asProblem, type ApiProblem } from '#lib/api/problem.js';

import {
  splitRefunds,
  type DeletionStats,
  type ManualRefund,
  type PaymentsSummary,
  type WindowMonths,
} from './payments';

export interface PaymentsLoaders {
  summary(months: number): Promise<PaymentsSummary>;
  deletions(months: number): Promise<DeletionStats>;
  /** Open manual refunds and those settled in the last 30 days. */
  refunds(): Promise<ManualRefund[]>;
  settle(id: string): Promise<ManualRefund>;
}

export const defaultPaymentsLoaders: PaymentsLoaders = {
  summary: (months) =>
    unwrap(billingApi.GET('/api/v1/billing/operator/payments', { params: { query: { months } } })),
  deletions: (months) =>
    unwrap(
      identityApi.GET('/api/v1/identity/platform/deletions', { params: { query: { months } } }),
    ),
  refunds: () =>
    unwrap(
      billingApi.GET('/api/v1/billing/operator/manual-refunds', {
        params: { query: { settled: true } },
      }),
    ),
  settle: (id) =>
    unwrap(
      billingApi.POST('/api/v1/billing/operator/manual-refunds/{refund_id}/settle', {
        params: { path: { refund_id: id } },
      }),
    ),
};

export class PaymentsDashboard {
  months = $state<WindowMonths>(12);
  summary = $state<PaymentsSummary | null>(null);
  deletions = $state<DeletionStats | null>(null);
  refunds = $state<ManualRefund[]>([]);
  refundsLoaded = $state(false);
  loading = $state(false);
  problem = $state<ApiProblem | null>(null);
  deletionsProblem = $state<ApiProblem | null>(null);
  refundsProblem = $state<ApiProblem | null>(null);
  /** The id of the refund being settled. */
  settling = $state<string | null>(null);
  settleProblem = $state<ApiProblem | null>(null);
  #loaders: PaymentsLoaders;

  constructor(loaders: PaymentsLoaders = defaultPaymentsLoaders) {
    this.#loaders = loaders;
  }

  get openRefunds(): ManualRefund[] {
    return splitRefunds(this.refunds).open;
  }

  get settledRefunds(): ManualRefund[] {
    return splitRefunds(this.refunds).settled;
  }

  /** Load every part for `months` (the current window when omitted). */
  async load(months: WindowMonths = this.months): Promise<void> {
    this.months = months;
    this.loading = true;
    const [summary, deletions, refunds] = await Promise.allSettled([
      this.#loaders.summary(months),
      this.#loaders.deletions(months),
      this.#loaders.refunds(),
    ]);
    if (summary.status === 'fulfilled') {
      this.summary = summary.value;
      this.problem = null;
    } else {
      this.problem = asProblem(summary.reason);
    }
    if (deletions.status === 'fulfilled') {
      this.deletions = deletions.value;
      this.deletionsProblem = null;
    } else {
      this.deletionsProblem = asProblem(deletions.reason);
    }
    if (refunds.status === 'fulfilled') {
      this.refunds = refunds.value;
      this.refundsProblem = null;
      this.refundsLoaded = true;
    } else {
      this.refundsProblem = asProblem(refunds.reason);
    }
    this.loading = false;
  }

  /** Mark a manual refund as paid back (the page asked for confirmation); true on success. */
  async settle(id: string): Promise<boolean> {
    this.settling = id;
    this.settleProblem = null;
    try {
      const settled = await this.#loaders.settle(id);
      this.refunds = this.refunds.map((r) => (r.id === id ? settled : r));
      // The ledger now counts the refund: the summary's refund figures moved.
      void this.#loaders
        .summary(this.months)
        .then((summary) => (this.summary = summary))
        .catch(() => undefined);
      return true;
    } catch (error) {
      this.settleProblem = asProblem(error);
      return false;
    } finally {
      this.settling = null;
    }
  }
}
