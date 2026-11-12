<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  Section 03 of the payments dashboard: past-due subscriptions and open (overdue) invoices as
  readouts, failed payment attempts per month (bars per currency, table, the latest attempts),
  and disputes: the open ones with the end of their evidence period first, the recently decided
  ones, and how many were opened and lost per month. No row names an account or a payment.
-->
<script lang="ts">
  import DailyBars from '#lib/components/DailyBars.svelte';
  import PaymentsSection from '#lib/components/PaymentsSection.svelte';
  import PaymentsTable from '#lib/components/PaymentsTable.svelte';
  import ToneBadge from '#lib/components/ToneBadge.svelte';
  import { formatDate, formatNumber } from '#lib/i18n.js';
  import {
    currenciesOf,
    disputeStatusLabel,
    disputeTone,
    monthLabel,
    perMonth,
    sum,
    type Dispute,
    type PaymentsSummary,
  } from '#lib/operator/payments.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    summary: PaymentsSummary;
    money: (amount: number, currency: string | null | undefined) => string;
    locale: string;
  }
  let { summary, money, locale }: Props = $props();

  const problems = $derived(summary.problems);
  const months = $derived(summary.months);
  const first = $derived(monthLabel(months[0] ?? '', locale));
  const last = $derived(monthLabel(months[months.length - 1] ?? '', locale));
  const failureCurrencies = $derived(currenciesOf(problems.failures));
</script>

{#snippet dispute(item: Dispute, testid: string)}
  <li class="dispute" data-testid={testid} data-status={item.status}>
    <div class="row">
      <span class="amount">{money(item.amount, item.currency)}</span>
      <ToneBadge
        tone={disputeTone(item.status)}
        glyph={disputeTone(item.status) === 'error' ? 'alert' : 'clock'}
        label={disputeStatusLabel(item.status)}
        value={item.status ?? ''}
      />
    </div>
    <div class="row quiet">
      {#if item.reason}<code>{item.reason}</code>{/if}
      <span>{m.op_pay_dispute_opened({ date: formatDate(item.opened_at) })}</span>
      {#if item.due_by && !item.closed_at}
        <strong>{m.op_pay_dispute_due({ date: formatDate(item.due_by) })}</strong>
      {/if}
    </div>
  </li>
{/snippet}

<PaymentsSection folio="03" label={m.op_pay_problems()} testid="payments-problems">
  <div class="block">
    <dl class="readouts">
      <div data-testid="payments-past-due" data-value={problems.past_due_subscriptions}>
        <dt>{m.op_pay_past_due()}</dt>
        <dd class="big">{formatNumber(problems.past_due_subscriptions)}</dd>
      </div>
      <div data-testid="payments-open-invoices">
        <dt>{m.op_pay_open_invoices()}</dt>
        {#each problems.invoices as backlog (backlog.currency)}
          <dd>
            <span class="big">{formatNumber(backlog.open_count)}</span>
            <span>{money(backlog.open_amount, backlog.currency)}</span>
            {#if backlog.overdue_count}
              <span class="overdue">
                {m.op_pay_overdue({
                  count: formatNumber(backlog.overdue_count),
                  amount: money(backlog.overdue_amount, backlog.currency),
                })}
              </span>
            {/if}
          </dd>
        {:else}
          <dd class="none">{m.op_pay_invoices_none()}</dd>
        {/each}
      </div>
    </dl>

    <h3>{m.op_pay_failed()}</h3>
    {#each failureCurrencies as currency (currency)}
      {@const counts = perMonth(problems.failures, months, currency, (f) => f.count)}
      <DailyBars
        label={m.op_pay_failed_chart({ currency: currency.toUpperCase() })}
        values={counts}
        days={months}
        format={(value) => formatNumber(value)}
        unit=""
        peakLabel={(value) => m.op_pay_peak({ value })}
        summary={m.op_pay_failed_summary({
          currency: currency.toUpperCase(),
          first,
          last,
          total: formatNumber(sum(counts)),
        })}
        firstLabel={first}
        lastLabel={last}
        testid="payments-failed-chart-{currency}"
      />
    {/each}
    {#if problems.failures.length}
      <PaymentsTable caption={m.op_pay_failed()} fallback testid="payments-failed-table">
        <thead>
          <tr>
            <th scope="col">{m.op_pay_col_month()}</th>
            <th scope="col">{m.op_pay_col_currency()}</th>
            <th scope="col" class="num">{m.op_pay_col_count()}</th>
            <th scope="col" class="num">{m.op_pay_col_amount()}</th>
          </tr>
        </thead>
        <tbody>
          {#each [...problems.failures].reverse() as row (`${row.month}|${row.currency}`)}
            <tr>
              <th scope="row">{monthLabel(row.month, locale)}</th>
              <td>{row.currency.toUpperCase()}</td>
              <td class="num">{formatNumber(row.count)}</td>
              <td class="num">{money(row.amount, row.currency)}</td>
            </tr>
          {/each}
        </tbody>
      </PaymentsTable>
      <ul class="feed" data-testid="payments-failed-recent">
        {#each problems.recent_failures as failure, i (i)}
          <li>
            <span class="when">{formatDate(failure.at)}</span>
            <span class="amount">{money(failure.amount, failure.currency)}</span>
            {#if failure.plan}<span>{failure.plan}</span>{/if}
            {#if failure.attempt}
              <span class="quiet">{m.op_pay_failed_attempt({ attempt: failure.attempt })}</span>
            {/if}
          </li>
        {/each}
      </ul>
    {:else}
      <p class="empty" data-testid="payments-failed-none">{m.op_pay_failed_none()}</p>
    {/if}
  </div>

  <div class="block">
    <h3>{m.op_pay_disputes_open()}</h3>
    {#if problems.open_disputes.length}
      <ul class="disputes" data-testid="payments-disputes-open">
        {#each problems.open_disputes as item, i (i)}
          {@render dispute(item, `dispute-open-${i}`)}
        {/each}
      </ul>
    {:else}
      <p class="empty" data-testid="payments-disputes-none">{m.op_pay_disputes_none()}</p>
    {/if}

    {#if problems.closed_disputes.length}
      <h3>{m.op_pay_disputes_closed()}</h3>
      <ul class="disputes" data-testid="payments-disputes-closed">
        {#each problems.closed_disputes as item, i (i)}
          {@render dispute(item, `dispute-closed-${i}`)}
        {/each}
      </ul>
    {/if}

    {#if problems.disputes.length}
      <PaymentsTable caption={m.op_pay_disputes()} testid="payments-disputes-table">
        <thead>
          <tr>
            <th scope="col">{m.op_pay_col_month()}</th>
            <th scope="col">{m.op_pay_col_currency()}</th>
            <th scope="col" class="num">{m.op_pay_col_opened()}</th>
            <th scope="col" class="num">{m.op_pay_col_amount()}</th>
            <th scope="col" class="num">{m.op_pay_col_lost()}</th>
          </tr>
        </thead>
        <tbody>
          {#each [...problems.disputes].reverse() as row (`${row.month}|${row.currency}`)}
            <tr>
              <th scope="row">{monthLabel(row.month, locale)}</th>
              <td>{row.currency.toUpperCase()}</td>
              <td class="num">{formatNumber(row.opened)}</td>
              <td class="num">{money(row.amount, row.currency)}</td>
              <td class="num">{formatNumber(row.lost)}</td>
            </tr>
          {/each}
        </tbody>
      </PaymentsTable>
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

  .readouts {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    margin: 0;
    border: var(--md-hairline) solid var(--md-color-line-strong);
  }

  .readouts > div {
    display: grid;
    align-content: start;
    gap: var(--md-space-1);
    min-width: 0;
    padding: var(--md-space-3);
  }

  .readouts > div + div {
    border-inline-start: var(--md-hairline) solid var(--md-color-line-strong);
  }

  dt {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  dd {
    display: grid;
    margin: 0;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }

  .big {
    font-size: var(--md-font-size-28);
    line-height: var(--md-line-32);
  }

  .overdue {
    justify-self: start;
    padding: 0 var(--md-space-1);
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .none {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-sans);
  }

  .empty {
    margin: 0;
    padding: var(--md-space-4);
    border: var(--md-hairline) dashed var(--md-color-line-strong);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .feed,
  .disputes {
    display: grid;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .feed li {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--md-space-2);
    padding: var(--md-space-1) 0;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    font-size: var(--md-font-size-13);
  }

  .when {
    min-width: 6.5em;
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .amount {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .dispute {
    display: grid;
    gap: var(--md-space-1);
    padding: var(--md-space-2) 0;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .dispute .amount {
    font-size: var(--md-font-size-16);
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-2);
  }

  .quiet {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .row.quiet {
    justify-content: flex-start;
  }

  code {
    font-family: var(--md-font-mono);
  }
</style>
