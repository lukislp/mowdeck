<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  The head of the payments dashboard: four readouts on the console's inverted ink band, like the
  totals line of a ledger page. Amounts per currency stack (never added across currencies).
-->
<script lang="ts">
  import { formatNumber } from '#lib/i18n.js';
  import type { PaymentsSummary } from '#lib/operator/payments.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    summary: PaymentsSummary;
    openRefunds: number;
    money: (amount: number, currency: string | null | undefined) => string;
  }
  let { summary, openRefunds, money }: Props = $props();

  const paying = $derived(
    summary.subscriptions.plans.reduce((total, p) => total + p.active + p.past_due, 0),
  );
  const thisMonth = $derived(
    summary.revenue.map((r) => ({
      currency: r.currency,
      net: r.months[r.months.length - 1]?.net ?? 0,
    })),
  );
</script>

<dl class="kpis" data-testid="payments-kpis">
  <div>
    <dt>{m.op_pay_kpi_mrr()}</dt>
    <dd data-testid="payments-kpi-mrr">
      {#each summary.subscriptions.mrr as row (row.currency)}
        <span>{money(row.amount, row.currency)}</span>
      {:else}
        <span class="none">{m.op_pay_none()}</span>
      {/each}
    </dd>
  </div>
  <div>
    <dt>{m.op_pay_kpi_active()}</dt>
    <dd data-testid="payments-kpi-active"><span>{formatNumber(paying)}</span></dd>
  </div>
  <div>
    <dt>{m.op_pay_kpi_net_month()}</dt>
    <dd data-testid="payments-kpi-net">
      {#each thisMonth as row (row.currency)}
        <span class:negative={row.net < 0}>{money(row.net, row.currency)}</span>
      {:else}
        <span class="none">{m.op_pay_none()}</span>
      {/each}
    </dd>
  </div>
  <div>
    <dt>{m.op_pay_kpi_open_refunds()}</dt>
    <dd data-testid="payments-kpi-refunds" data-value={openRefunds}>
      <span class:alert={openRefunds > 0}>{formatNumber(openRefunds)}</span>
    </dd>
  </div>
</dl>

<style>
  .kpis {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    margin: 0;
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .kpis > div {
    display: grid;
    align-content: start;
    gap: var(--md-space-1);
    min-width: 0;
    padding: var(--md-space-3) var(--md-gutter) var(--md-space-4);
    border-top: var(--md-hairline) solid var(--md-color-ink-muted);
  }

  .kpis > div:nth-child(odd) {
    border-inline-end: var(--md-hairline) solid var(--md-color-ink-muted);
  }

  dt {
    opacity: 0.72;
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  dd {
    display: grid;
    margin: 0;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-24);
    font-variant-numeric: tabular-nums;
    line-height: var(--md-line-32);
    overflow-wrap: anywhere;
  }

  .none {
    opacity: 0.6;
    font-family: var(--md-font-sans);
    font-size: var(--md-font-size-16);
  }

  /* Never colour alone: a negative month also carries its minus sign. */
  .negative {
    text-decoration: underline wavy;
    text-decoration-thickness: 1px;
    text-underline-offset: 4px;
  }

  .alert {
    justify-self: start;
    padding: 0 var(--md-space-2);
    background: var(--md-color-ink-inverse);
    color: var(--md-color-ink);
  }

  @media (min-width: 1024px) {
    .kpis {
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }

    .kpis > div {
      border-inline-end: var(--md-hairline) solid var(--md-color-ink-muted);
    }

    .kpis > div:last-child {
      border-inline-end: 0;
    }

    dd {
      font-size: var(--md-font-size-32);
      line-height: 40px;
    }
  }
</style>
