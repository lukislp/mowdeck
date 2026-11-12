<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  Section 04 of the payments dashboard: account deletions per month from Identity (scheduled,
  restored, completed; counts only, ADR 0082) and where the open ones stand now. Identity answers
  on its own: when it fails, this section says so and the rest of the dashboard stays.
-->
<script lang="ts">
  import { ProblemNotice } from '@mowdeck/ui';

  import type { ApiProblem } from '#lib/api/problem.js';
  import DailyBars from '#lib/components/DailyBars.svelte';
  import PaymentsSection from '#lib/components/PaymentsSection.svelte';
  import PaymentsTable from '#lib/components/PaymentsTable.svelte';
  import { formatNumber, problemMessage } from '#lib/i18n.js';
  import { monthLabel, sum, type DeletionStats } from '#lib/operator/payments.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    deletions: DeletionStats | null;
    problem: ApiProblem | null;
    locale: string;
  }
  let { deletions, problem, locale }: Props = $props();
</script>

<PaymentsSection folio="04" label={m.op_pay_deletions()} testid="payments-deletions">
  {#if problem}
    <ProblemNotice
      title={problemMessage(problem)}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  {:else if deletions}
    {@const months = deletions.months.map((d) => d.month)}
    {@const first = monthLabel(months[0] ?? '', locale)}
    {@const last = monthLabel(months[months.length - 1] ?? '', locale)}
    {@const completed = deletions.months.map((d) => d.completed)}
    <div class="block">
      <DailyBars
        label={m.op_pay_del_completed()}
        values={completed}
        days={months}
        format={(value) => formatNumber(value)}
        unit=""
        peakLabel={(value) => m.op_pay_peak({ value })}
        summary={m.op_pay_del_summary({ first, last, total: formatNumber(sum(completed)) })}
        firstLabel={first}
        lastLabel={last}
        testid="payments-deletions-chart"
      />
      <p class="now" data-testid="payments-deletions-now">
        {m.op_pay_del_now({
          grace: formatNumber(deletions.in_grace),
          purging: formatNumber(deletions.purging),
        })}
      </p>
      <p class="rule">{m.op_pay_del_rule()}</p>
    </div>
    <div class="block">
      <PaymentsTable caption={m.op_pay_deletions()} testid="payments-deletions-table">
        <thead>
          <tr>
            <th scope="col">{m.op_pay_col_month()}</th>
            <th scope="col" class="num">{m.op_pay_del_scheduled()}</th>
            <th scope="col" class="num">{m.op_pay_del_restored()}</th>
            <th scope="col" class="num">{m.op_pay_del_completed()}</th>
          </tr>
        </thead>
        <tbody>
          {#each [...deletions.months].reverse() as row (row.month)}
            <tr>
              <th scope="row">{monthLabel(row.month, locale)}</th>
              <td class="num">{formatNumber(row.scheduled)}</td>
              <td class="num">{formatNumber(row.restored)}</td>
              <td class="num">{formatNumber(row.completed)}</td>
            </tr>
          {/each}
        </tbody>
      </PaymentsTable>
    </div>
  {/if}
</PaymentsSection>

<style>
  .block {
    display: grid;
    align-content: start;
    gap: var(--md-space-3);
    min-width: 0;
  }

  .now {
    margin: 0;
    padding-inline-start: var(--md-space-2);
    border-inline-start: 3px solid var(--md-color-ink);
    font-size: var(--md-font-size-14);
  }

  .rule {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }
</style>
