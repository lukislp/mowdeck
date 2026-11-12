<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  Section 02 of the payments dashboard: refunds per month (bars per currency, the reasons side by
  side in the table), the refunds of deleted accounts the operator pays back by hand, and those
  paid back in the last 30 days. "Mark as paid back" asks first: the row turns into the question
  with the amount and the payment, and only "Yes, paid back" settles it.
-->
<script lang="ts">
  import { Button, ProblemNotice } from '@mowdeck/ui';
  import { tick } from 'svelte';

  import DailyBars from '#lib/components/DailyBars.svelte';
  import PaymentsSection from '#lib/components/PaymentsSection.svelte';
  import PaymentsTable from '#lib/components/PaymentsTable.svelte';
  import { formatDate, formatNumber, problemMessage } from '#lib/i18n.js';
  import type { PaymentsDashboard } from '#lib/operator/payments.svelte.js';
  import {
    currenciesOf,
    monthLabel,
    perMonth,
    refundRows,
    sum,
    type ManualRefund,
    type PaymentsSummary,
  } from '#lib/operator/payments.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    summary: PaymentsSummary;
    dashboard: PaymentsDashboard;
    money: (amount: number, currency: string | null | undefined) => string;
    locale: string;
  }
  let { summary, dashboard, money, locale }: Props = $props();

  const months = $derived(summary.months);
  const first = $derived(monthLabel(months[0] ?? '', locale));
  const last = $derived(monthLabel(months[months.length - 1] ?? '', locale));
  const currencies = $derived(currenciesOf(summary.refunds));
  const rows = $derived(refundRows(summary.refunds));

  let confirming = $state<string | null>(null);

  async function ask(refund: ManualRefund): Promise<void> {
    confirming = refund.id;
    await tick();
    // The question takes the focus's place: keyboard users land on the answer.
    document.querySelector<HTMLElement>(`[data-testid="settle-yes-${refund.id}"]`)?.focus();
  }

  async function settle(refund: ManualRefund): Promise<void> {
    if (await dashboard.settle(refund.id)) confirming = null;
  }
</script>

<PaymentsSection folio="02" label={m.op_pay_refunds()} testid="payments-refunds">
  <div class="block">
    {#each currencies as currency (currency)}
      {@const values = perMonth(summary.refunds, months, currency, (r) => r.amount)}
      <DailyBars
        label={m.op_pay_refunds_chart({ currency: currency.toUpperCase() })}
        {values}
        days={months}
        format={(value) => money(value, currency)}
        unit=""
        peakLabel={(value) => m.op_pay_peak({ value })}
        summary={m.op_pay_refunds_summary({
          currency: currency.toUpperCase(),
          first,
          last,
          total: money(sum(values), currency),
        })}
        firstLabel={first}
        lastLabel={last}
        testid="payments-refunds-chart-{currency}"
      />
    {/each}
    {#if rows.length}
      <PaymentsTable caption={m.op_pay_refunds()} fallback testid="payments-refunds-table">
        <thead>
          <tr>
            <th scope="col">{m.op_pay_col_month()}</th>
            <th scope="col">{m.op_pay_col_currency()}</th>
            <th scope="col" class="num">{m.op_pay_reason_erasure()}</th>
            <th scope="col" class="num">{m.op_pay_reason_other()}</th>
          </tr>
        </thead>
        <tbody>
          {#each rows as row (`${row.month}|${row.currency}`)}
            <tr>
              <th scope="row">{monthLabel(row.month, locale)}</th>
              <td>{row.currency.toUpperCase()}</td>
              <td class="num">
                {formatNumber(row.erasure.count)} · {money(row.erasure.amount, row.currency)}
              </td>
              <td class="num">
                {formatNumber(row.other.count)} · {money(row.other.amount, row.currency)}
              </td>
            </tr>
          {/each}
        </tbody>
      </PaymentsTable>
    {:else}
      <p class="empty" data-testid="payments-refunds-empty">{m.op_pay_refunds_none()}</p>
    {/if}
  </div>

  <div class="block">
    <h3>{m.op_pay_manual()}</h3>
    <p class="hint">{m.op_pay_manual_hint()}</p>
    {#if dashboard.refundsProblem}
      <ProblemNotice
        title={problemMessage(dashboard.refundsProblem)}
        reference={dashboard.refundsProblem.correlationId}
        referenceLabel={m.problem_reference()}
      />
    {:else if dashboard.openRefunds.length}
      <ul class="slips" data-testid="payments-manual-open">
        {#each dashboard.openRefunds as refund (refund.id)}
          {@const amount = money(refund.amount, refund.currency)}
          <li class="slip" data-testid="manual-refund-{refund.id}">
            <div class="slip-head">
              <span class="amount">{amount}</span>
              <span class="since">
                {m.op_pay_manual_since({ date: formatDate(refund.created_at) })}
              </span>
            </div>
            <dl>
              <dt>{m.op_pay_manual_payment()}</dt>
              <dd><code>{refund.payment_ref}</code></dd>
              {#if refund.invoice_ref}
                <dt>{m.op_pay_manual_invoice()}</dt>
                <dd><code>{refund.invoice_ref}</code></dd>
              {/if}
            </dl>
            <p class="reason">{refund.reason}</p>
            {#if confirming === refund.id}
              <div
                class="confirm"
                role="group"
                aria-label={m.op_pay_settle()}
                data-testid="settle-confirm-{refund.id}"
              >
                <p>{m.op_pay_settle_confirm({ amount, payment: refund.payment_ref })}</p>
                {#if dashboard.settleProblem}
                  <ProblemNotice
                    title={problemMessage(dashboard.settleProblem)}
                    reference={dashboard.settleProblem.correlationId}
                    referenceLabel={m.problem_reference()}
                  />
                {/if}
                <div class="actions">
                  <Button variant="ghost" onclick={() => (confirming = null)}>
                    {m.op_pay_settle_cancel()}
                  </Button>
                  <Button
                    variant="primary"
                    icon="check"
                    pending={dashboard.settling === refund.id}
                    onclick={() => void settle(refund)}
                    testid="settle-yes-{refund.id}"
                  >
                    {m.op_pay_settle_yes()}
                  </Button>
                </div>
              </div>
            {:else}
              <div class="actions">
                <Button icon="check" onclick={() => void ask(refund)} testid="settle-{refund.id}"
                  >{m.op_pay_settle()}</Button
                >
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {:else if dashboard.refundsLoaded}
      <p class="empty" data-testid="payments-manual-none">{m.op_pay_manual_none()}</p>
    {/if}

    <h3>{m.op_pay_settled()}</h3>
    {#if dashboard.settledRefunds.length}
      <ul class="settled" data-testid="payments-manual-settled">
        {#each dashboard.settledRefunds as refund (refund.id)}
          <li>
            <span class="amount">{money(refund.amount, refund.currency)}</span>
            <code>{refund.payment_ref}</code>
            <span class="since">
              {m.op_pay_settled_at({ date: formatDate(refund.settled_at ?? refund.created_at) })}
            </span>
          </li>
        {/each}
      </ul>
    {:else if dashboard.refundsLoaded}
      <p class="quiet">{m.op_pay_settled_none()}</p>
    {/if}
  </div>
</PaymentsSection>

<style>
  .block {
    display: grid;
    align-content: start;
    gap: var(--md-space-3);
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

  .hint,
  .quiet,
  .empty {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .empty {
    padding: var(--md-space-4);
    border: var(--md-hairline) dashed var(--md-color-line-strong);
  }

  .slips,
  .settled {
    display: grid;
    gap: var(--md-space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  /* A manual refund reads like a payment slip: perforated edge, amount first. */
  .slip {
    display: grid;
    gap: var(--md-space-2);
    padding: var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-inline-start: 4px dashed var(--md-color-ink);
    background: var(--md-color-surface);
  }

  .slip-head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--md-space-2);
  }

  .amount {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-20);
    font-variant-numeric: tabular-nums;
  }

  .since {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  dl {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    gap: var(--md-space-0_5) var(--md-space-3);
    margin: 0;
    font-size: var(--md-font-size-13);
  }

  dt {
    color: var(--md-color-ink-muted);
  }

  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }

  code {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
  }

  .reason {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .confirm {
    display: grid;
    gap: var(--md-space-2);
    padding: var(--md-space-3);
    background: var(--md-color-surface-sunken);
  }

  .confirm p {
    margin: 0;
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-semibold);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: var(--md-space-2);
  }

  .settled li {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--md-space-2);
    padding-bottom: var(--md-space-2);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .settled .amount {
    font-size: var(--md-font-size-14);
    text-decoration: line-through;
    text-decoration-thickness: 1px;
  }
</style>
