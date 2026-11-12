<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  The payments dashboard of the operator console (ADR 0083): the readouts band, then four ledger
  sections (revenue and subscriptions, refunds, payment problems, account deletions) for the
  chosen window of months in Billing's report zone (named on the page). `billing` false (Billing does not run here; the console hides the
  tab then) shows a notice and loads nothing.
-->
<script lang="ts">
  import { ProblemNotice, SegmentedControl, TopBar } from '@mowdeck/ui';

  import PaymentsDeletions from '#lib/components/PaymentsDeletions.svelte';
  import PaymentsKpis from '#lib/components/PaymentsKpis.svelte';
  import PaymentsProblems from '#lib/components/PaymentsProblems.svelte';
  import PaymentsRefunds from '#lib/components/PaymentsRefunds.svelte';
  import PaymentsRevenue from '#lib/components/PaymentsRevenue.svelte';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import { formatDate, problemMessage } from '#lib/i18n.js';
  import type { PaymentsDashboard } from '#lib/operator/payments.svelte.js';
  import { moneyFormatter, WINDOWS, type WindowMonths } from '#lib/operator/payments.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    /** Billing runs here (the probe answered); null while the probe is out. */
    billing: boolean | null;
    dashboard: PaymentsDashboard;
    locale: string;
  }
  let { billing, dashboard, locale }: Props = $props();

  const summary = $derived(dashboard.summary);
  const money = $derived(moneyFormatter(summary?.currencies ?? [], locale));
  const windowOptions = $derived(
    WINDOWS.map((count) => ({ value: String(count), label: m.op_pay_window_option({ count }) })),
  );

  function choose(value: string): void {
    void dashboard.load(Number(value) as WindowMonths);
  }
</script>

<TopBar
  title={m.op_pay_title()}
  meta={billing && summary
    ? m.op_pay_window({ count: dashboard.months, zone: summary.time_zone })
    : undefined}
>
  {#snippet actions()}
    {#if billing}
      <SegmentedControl
        label={m.op_pay_window_label()}
        options={windowOptions}
        value={String(dashboard.months)}
        onchange={choose}
        testid="payments-window"
      />
    {/if}
  {/snippet}
</TopBar>

{#if billing === false}
  <div class="pad" data-testid="payments-billing-off">
    <ProblemNotice tone="info" title={m.op_billing_off_title()} detail={m.op_pay_billing_off()} />
  </div>
{:else if billing === null || (!summary && !dashboard.problem)}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{:else if dashboard.problem && !summary}
  <div class="pad">
    <ProblemNotice
      title={problemMessage(dashboard.problem)}
      reference={dashboard.problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if summary}
  <div class="dashboard" data-testid="payments-dashboard" aria-busy={dashboard.loading}>
    <PaymentsKpis {summary} openRefunds={dashboard.openRefunds.length} {money} />
    <p class="provenance" data-testid="payments-since">
      <span>
        {summary.since
          ? m.op_pay_since({ date: formatDate(summary.since) })
          : m.op_pay_since_none()}
      </span>
      <span data-testid="payments-zone">{m.op_pay_zone({ zone: summary.time_zone })}</span>
      <span>{m.op_pay_privacy()}</span>
    </p>
    <PaymentsRevenue {summary} {money} {locale} />
    <PaymentsRefunds {summary} {dashboard} {money} {locale} />
    <PaymentsProblems {summary} {money} {locale} />
    <PaymentsDeletions
      deletions={dashboard.deletions}
      problem={dashboard.deletionsProblem}
      {locale}
    />
  </div>
{/if}

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .quiet {
    margin: 0;
    color: var(--md-color-ink-muted);
  }

  .dashboard {
    display: grid;
    padding-bottom: var(--md-space-8);
  }

  .dashboard[aria-busy='true'] {
    opacity: 0.72;
    transition: opacity 120ms linear;
  }

  .provenance {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-1) var(--md-space-4);
    margin: 0;
    padding: var(--md-space-2) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  @media (prefers-reduced-motion: reduce) {
    .dashboard[aria-busy='true'] {
      transition: none;
    }
  }
</style>
