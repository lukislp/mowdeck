<!--
  Operator console, plans: the offered plans (`GET /entitlements/plans`, active ones) with their
  limits, trial days and features; edit one or create a new one in the editor. With billing
  disabled every account resolves to `unlimited`, which a warning says above the list.
-->
<script lang="ts">
  import { Button, Icon, ProblemNotice, TopBar } from '@mowdeck/ui';

  import { entitlementsApi, unwrap } from '$lib/api/client';
  import { asProblem, type ApiProblem } from '$lib/api/problem';
  import SlowLoading from '$lib/components/SlowLoading.svelte';
  import { formatNumber, problemMessage } from '$lib/i18n';
  import { m } from '$lib/paraglide/messages';
  import { featureLabel, limitLabel } from '$lib/plan/labels';
  import { accountPlan } from '$lib/plan/plan.svelte';
  import { LIMIT_KEYS, type Plan } from '$lib/plan/types';

  let plans = $state<Plan[] | null>(null);
  let problem = $state<ApiProblem | null>(null);

  $effect(() => {
    void unwrap(entitlementsApi.GET('/api/v1/entitlements/plans'))
      .then((list) => (plans = list))
      .catch((error: unknown) => (problem = asProblem(error)));
    void accountPlan.load();
  });

  const billingOff = $derived(accountPlan.current?.billing_enabled === false);
</script>

<svelte:head><title>{m.op_plans_title()} · {m.app_name()}</title></svelte:head>

<TopBar title={m.op_plans_title()} meta={plans ? String(plans.length) : undefined}>
  {#snippet actions()}
    <Button icon="plus" label={m.op_plan_new()} href="/operator/plans/edit" testid="plan-new" />
  {/snippet}
</TopBar>

{#if billingOff}
  <div class="pad tight">
    <ProblemNotice tone="info" title={m.op_billing_off_title()} detail={m.op_plans_billing_off()} />
  </div>
{/if}

{#if problem}
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if !plans}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{:else}
  <ul class="plans" data-testid="operator-plans">
    {#each plans as plan (plan.key)}
      <li class="plan" data-testid="operator-plan-{plan.key}">
        <div class="head">
          <div class="titles">
            <h2>{plan.name}</h2>
            <span class="key">{plan.key}</span>
          </div>
          <div class="marks">
            {#if plan.is_default}<span class="mark">{m.op_plan_default()}</span>{/if}
            {#if plan.trial_days > 0}
              <span class="mark quiet-mark"
                >{m.op_plan_trial_days({ days: formatNumber(plan.trial_days) })}</span
              >
            {/if}
          </div>
        </div>
        {#if plan.description}<p class="desc">{plan.description}</p>{/if}
        <dl class="limits">
          {#each LIMIT_KEYS as key (key)}
            <div>
              <dt>{limitLabel(key)}</dt>
              <dd class:open={plan.limits[key] === null || plan.limits[key] === undefined}>
                {plan.limits[key] === null || plan.limits[key] === undefined
                  ? m.plan_unlimited()
                  : formatNumber(plan.limits[key] ?? 0)}
              </dd>
            </div>
          {/each}
        </dl>
        <ul class="chips" aria-label={m.plan_features()}>
          {#each [...plan.features].sort() as feature (feature)}
            <li class="chip">{featureLabel(feature)}</li>
          {/each}
        </ul>
        <div class="actions">
          <Button
            variant="ghost"
            icon="settings"
            href="/operator/plans/edit?key={encodeURIComponent(plan.key)}"
            testid="plan-edit-{plan.key}">{m.op_plan_edit()}</Button
          >
        </div>
      </li>
    {/each}
  </ul>
  <p class="pad quiet fine"><Icon name="list" size={14} />{m.op_plans_inactive_hint()}</p>
{/if}

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .tight {
    padding-bottom: 0;
  }

  .quiet {
    margin: 0;
    color: var(--md-color-ink-muted);
  }

  .fine {
    display: flex;
    align-items: center;
    gap: var(--md-space-1);
    font-size: var(--md-font-size-13);
  }

  .plans {
    display: grid;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .plan {
    display: grid;
    align-content: start;
    gap: var(--md-space-3);
    padding: var(--md-space-4) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--md-space-2);
  }

  .titles {
    display: flex;
    align-items: baseline;
    gap: var(--md-space-2);
  }

  h2 {
    margin: 0;
    color: var(--md-color-ink);
    font-size: var(--md-font-size-24);
    font-weight: var(--md-weight-semibold);
    letter-spacing: normal;
    line-height: var(--md-line-32);
    text-transform: none;
  }

  .key {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
  }

  .marks {
    display: flex;
    gap: var(--md-space-2);
  }

  .mark {
    padding: 0 var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-ink);
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    line-height: var(--md-line-24);
    text-transform: uppercase;
  }

  .quiet-mark {
    background: none;
    color: var(--md-color-ink);
  }

  .desc {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .limits {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    margin: 0;
    border: var(--md-hairline) solid var(--md-color-line);
  }

  .limits div {
    display: grid;
    gap: var(--md-space-0_5);
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-end: var(--md-hairline) solid var(--md-color-line);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  dt {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  dd {
    margin: 0;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-20);
    font-variant-numeric: tabular-nums;
  }

  dd.open {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-sans);
    font-size: var(--md-font-size-13);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    align-self: end;
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-1);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .chip {
    padding: 0 var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-24);
  }

  .actions {
    display: flex;
    justify-content: flex-end;
  }

  @media (min-width: 1024px) {
    .plans {
      grid-template-columns: repeat(auto-fill, minmax(440px, 1fr));
      gap: var(--md-space-4);
      padding: var(--md-space-4) var(--md-gutter);
    }

    .plan {
      border: var(--md-hairline) solid var(--md-color-line);
    }
  }
</style>
