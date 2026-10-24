<!--
  The plan of the account (`GET /entitlements/me`): the plan's name on a plate with its status and
  the end of a trial or grace period, the limits as gauges with the current use where the app
  knows it, the features as chips. With billing disabled the installation has no subscription
  limits: a calm note says so and the gauges show unlimited.
-->
<script lang="ts">
  import { Icon, ProblemNotice } from '@mowdeck/ui';

  import { formatDate } from '#lib/i18n.js';
  import { featureLabel } from '#lib/plan/labels.js';
  import type { LimitKey, ResolvedPlan } from '#lib/plan/types.js';
  import { isRestricted, limitRows, statusUntil } from '#lib/plan/usage.js';
  import { m } from '#lib/paraglide/messages.js';

  import LimitMeter from './LimitMeter.svelte';
  import PlanStatusBadge from './PlanStatusBadge.svelte';

  interface Props {
    plan: ResolvedPlan;
    usage: Partial<Record<LimitKey, number | null>>;
  }
  let { plan, usage }: Props = $props();

  const rows = $derived(limitRows(plan.limits, usage));
  const until = $derived(statusUntil(plan));
  const restricted = $derived(isRestricted(plan.status));
  const features = $derived([...plan.features].sort((a, b) => a.localeCompare(b)));
</script>

<div
  class="plan"
  data-testid="account-plan"
  data-plan={plan.plan}
  data-status={plan.status}
  data-billing={String(plan.billing_enabled)}
>
  <div class="plate">
    <span class="glyph"><Icon name="shield" size={22} /></span>
    <div class="titles">
      <span class="eyebrow">{m.plan_current()}</span>
      <span class="name" data-testid="plan-name">{plan.plan_name}</span>
      <span class="key">{plan.plan}</span>
    </div>
    <div class="state">
      <PlanStatusBadge status={plan.status} testid="plan-status" />
      {#if until}
        <span class="until"
          >{plan.status === 'trial'
            ? m.plan_trial_until({ date: formatDate(until) })
            : m.plan_grace_until({ date: formatDate(until) })}</span
        >
      {:else if plan.since}
        <span class="until">{m.plan_since({ date: formatDate(plan.since) })}</span>
      {/if}
    </div>
  </div>

  {#if !plan.billing_enabled}
    <p class="note" data-testid="plan-no-limits">
      <Icon name="check" size={18} />
      <span>{m.plan_billing_disabled()}</span>
    </p>
  {/if}

  {#if restricted}
    <ProblemNotice
      title={plan.status === 'grace' ? m.plan_grace_title() : m.plan_expired_title()}
      detail={m.plan_restricted_body()}
    />
  {/if}

  <div class="grid">
    <section aria-labelledby="plan-limits-head">
      <h3 id="plan-limits-head">{m.plan_limits()}</h3>
      <ul class="limits">
        {#each rows as row (row.key)}
          <LimitMeter {row} unit={row.key === 'history_days' ? m.plan_days() : undefined} />
        {/each}
      </ul>
      <p class="fine">{m.plan_usage_hint()}</p>
    </section>
    <section aria-labelledby="plan-features-head">
      <h3 id="plan-features-head">{m.plan_features()}</h3>
      <ul class="chips" data-testid="plan-features">
        {#each features as feature (feature)}
          <li class="chip" data-feature={feature}>
            <Icon name="check" size={14} />{featureLabel(feature)}
          </li>
        {/each}
      </ul>
    </section>
  </div>
</div>

<style>
  .plan {
    display: grid;
    gap: var(--md-space-4);
  }

  .plate {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-3);
    padding: var(--md-space-3) var(--md-space-4);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-inline-start: 4px solid var(--md-color-ink);
    background: var(--md-color-surface-sunken);
  }

  .glyph {
    display: grid;
    place-items: center;
    width: 40px;
    height: 40px;
    border: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface);
  }

  .titles {
    display: flex;
    flex: 1;
    flex-direction: column;
    min-width: 0;
  }

  .eyebrow,
  h3 {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .name {
    font-size: var(--md-font-size-24);
    font-weight: var(--md-weight-semibold);
    line-height: var(--md-line-28);
  }

  .key {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
  }

  .state {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: var(--md-space-1);
  }

  .until {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
  }

  .note {
    display: flex;
    align-items: flex-start;
    gap: var(--md-space-2);
    margin: 0;
    padding: var(--md-space-3);
    border: var(--md-hairline) dashed var(--md-color-line-strong);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .note > :global(svg) {
    flex: none;
    margin-top: 1px;
    color: var(--md-state-mowing);
  }

  .grid {
    display: grid;
    gap: var(--md-space-6);
  }

  .limits,
  .chips {
    margin: var(--md-space-2) 0 0;
    padding: 0;
    list-style: none;
  }

  .fine {
    margin: var(--md-space-2) 0 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    min-height: 32px;
    padding: 0 var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
    font-size: var(--md-font-size-13);
  }

  .chip > :global(svg) {
    color: var(--md-state-mowing);
  }

  @media (min-width: 1024px) {
    .grid {
      grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
    }
  }
</style>
