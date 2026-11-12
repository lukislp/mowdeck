<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  Section 01 of the payments dashboard: net revenue per month for each currency (bars with the
  months as a table behind them), the subscriptions per plan with the MRR, and new and ended
  subscriptions per month. The bars reuse DailyBars; a month whose refunds exceed its revenue has
  no bar, its negative net is in the table.
-->
<script lang="ts">
  import DailyBars from '#lib/components/DailyBars.svelte';
  import PaymentsSection from '#lib/components/PaymentsSection.svelte';
  import PaymentsTable from '#lib/components/PaymentsTable.svelte';
  import { formatNumber } from '#lib/i18n.js';
  import { monthLabel, revenueRows, sum, type PaymentsSummary } from '#lib/operator/payments.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    summary: PaymentsSummary;
    money: (amount: number, currency: string | null | undefined) => string;
    locale: string;
  }
  let { summary, money, locale }: Props = $props();

  const months = $derived(summary.months);
  const first = $derived(monthLabel(months[0] ?? '', locale));
  const last = $derived(monthLabel(months[months.length - 1] ?? '', locale));
  const flow = $derived(summary.subscriptions.flow);
  const started = $derived(sum(flow.map((f) => f.started)));
</script>

<PaymentsSection folio="01" label={m.op_pay_revenue()} testid="payments-revenue">
  <div class="block">
    {#each summary.revenue as revenue (revenue.currency)}
      {@const code = revenue.currency.toUpperCase()}
      <div class="currency" data-testid="payments-revenue-{revenue.currency}">
        <DailyBars
          label={m.op_pay_revenue_net({ currency: code })}
          values={revenue.months.map((month) => Math.max(0, month.net))}
          days={months}
          format={(value) => money(value, revenue.currency)}
          unit=""
          peakLabel={(value) => m.op_pay_peak({ value })}
          summary={m.op_pay_revenue_summary({
            currency: code,
            first,
            last,
            total: money(revenue.net, revenue.currency),
          })}
          firstLabel={first}
          lastLabel={last}
        />
        <PaymentsTable
          caption={m.op_pay_revenue_net({ currency: code })}
          fallback
          testid="payments-revenue-table-{revenue.currency}"
        >
          <thead>
            <tr>
              <th scope="col">{m.op_pay_col_month()}</th>
              <th scope="col" class="num">{m.op_pay_col_gross()}</th>
              <th scope="col" class="num">{m.op_pay_col_tax()}</th>
              <th scope="col" class="num">{m.op_pay_col_refunded()}</th>
              <th scope="col" class="num">{m.op_pay_col_net()}</th>
              <th scope="col" class="num">{m.op_pay_col_invoices()}</th>
            </tr>
          </thead>
          <tbody>
            {#each revenueRows(revenue) as row (row.month)}
              <tr>
                <th scope="row">{monthLabel(row.month, locale)}</th>
                <td class="num">{money(row.gross, revenue.currency)}</td>
                <td class="num">{money(row.tax, revenue.currency)}</td>
                <td class="num">{money(row.refunded, revenue.currency)}</td>
                <td class="num" class:negative={row.net < 0}>{money(row.net, revenue.currency)}</td>
                <td class="num">{formatNumber(row.invoices)}</td>
              </tr>
            {/each}
          </tbody>
        </PaymentsTable>
      </div>
    {:else}
      <p class="empty" data-testid="payments-revenue-empty">{m.op_pay_revenue_none()}</p>
    {/each}
    <p class="rule">{m.op_pay_net_rule()}</p>
  </div>

  <div class="block">
    <h3>{m.op_pay_plans()}</h3>
    {#if summary.subscriptions.plans.length}
      <PaymentsTable caption={m.op_pay_plans()} testid="payments-plans">
        <thead>
          <tr>
            <th scope="col">{m.op_pay_col_plan()}</th>
            <th scope="col" class="num">{m.op_pay_col_trialing()}</th>
            <th scope="col" class="num">{m.op_pay_col_active()}</th>
            <th scope="col" class="num">{m.op_pay_col_past_due()}</th>
          </tr>
        </thead>
        <tbody>
          {#each summary.subscriptions.plans as plan (plan.plan ?? '')}
            <tr data-testid="payments-plan-{plan.plan ?? 'unknown'}">
              <th scope="row">{plan.plan ?? m.op_pay_plan_unknown()}</th>
              <td class="num">{formatNumber(plan.trialing)}</td>
              <td class="num">{formatNumber(plan.active)}</td>
              <td class="num">{formatNumber(plan.past_due)}</td>
            </tr>
          {/each}
        </tbody>
      </PaymentsTable>
    {:else}
      <p class="empty">{m.op_pay_plans_none()}</p>
    {/if}
    {#if summary.subscriptions.unpriced > 0}
      <p class="note" data-testid="payments-unpriced">
        {m.op_pay_mrr_unpriced({ count: formatNumber(summary.subscriptions.unpriced) })}
      </p>
    {/if}
    <p class="rule">{m.op_pay_mrr_rule()}</p>

    <h3>{m.op_pay_flow()}</h3>
    <DailyBars
      label={m.op_pay_flow_new()}
      values={flow.map((f) => f.started)}
      days={months}
      format={(value) => formatNumber(value)}
      unit=""
      peakLabel={(value) => m.op_pay_peak({ value })}
      summary={m.op_pay_flow_summary({ first, last, total: formatNumber(started) })}
      firstLabel={first}
      lastLabel={last}
      testid="payments-flow"
    />
    <PaymentsTable caption={m.op_pay_flow()} fallback testid="payments-flow-table">
      <thead>
        <tr>
          <th scope="col">{m.op_pay_col_month()}</th>
          <th scope="col" class="num">{m.op_pay_flow_new()}</th>
          <th scope="col" class="num">{m.op_pay_flow_ended()}</th>
        </tr>
      </thead>
      <tbody>
        {#each [...flow].reverse() as row (row.month)}
          <tr>
            <th scope="row">{monthLabel(row.month, locale)}</th>
            <td class="num">{formatNumber(row.started)}</td>
            <td class="num">{formatNumber(row.ended)}</td>
          </tr>
        {/each}
      </tbody>
    </PaymentsTable>
  </div>
</PaymentsSection>

<style>
  .block {
    display: grid;
    align-content: start;
    gap: var(--md-space-3);
    min-width: 0;
  }

  .currency {
    display: grid;
    gap: var(--md-space-2);
    min-width: 0;
  }

  h3 {
    margin: var(--md-space-2) 0 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .empty,
  .note,
  .rule {
    margin: 0;
    font-size: var(--md-font-size-13);
  }

  .empty {
    padding: var(--md-space-4);
    border: var(--md-hairline) dashed var(--md-color-line-strong);
    color: var(--md-color-ink-muted);
  }

  .note {
    padding-inline-start: var(--md-space-2);
    border-inline-start: 3px solid var(--md-color-ink);
  }

  .rule {
    color: var(--md-color-ink-muted);
  }
</style>
