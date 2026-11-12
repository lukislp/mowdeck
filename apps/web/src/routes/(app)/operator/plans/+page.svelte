<!-- SPDX-FileCopyrightText: 2026 Lukas Körber -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->

<!--
  Operator console, plans: the offered plans (`GET /entitlements/plans`, active ones) with their
  limits, trial days and features; edit one or create a new one in the editor. With billing
  disabled every account resolves to `unlimited`, which a warning says above the list.
-->
<script lang="ts">
  import { Button, Icon, ProblemNotice, TopBar } from '@mowdeck/ui';

  import { billingApi, entitlementsApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { intervalLabel, probeBilling } from '#lib/billing/billing.js';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import { formatNumber, problemMessage } from '#lib/i18n.js';
  import {
    checkPlanPrices,
    exponentOf,
    formatMoney,
    type OperatorPrice,
  } from '#lib/operator/payments.js';
  import { m } from '#lib/paraglide/messages.js';
  import { getLocale } from '#lib/paraglide/runtime.js';
  import { featureLabel, limitLabel } from '#lib/plan/labels.js';
  import { accountPlan } from '#lib/plan/plan.svelte.js';
  import { LIMIT_KEYS, type Plan } from '#lib/plan/types.js';

  let plans = $state<Plan[] | null>(null);
  let problem = $state<ApiProblem | null>(null);
  /** The Stripe prices of the price file, where Billing runs (read only). */
  let prices = $state<OperatorPrice[] | null>(null);

  $effect(() => {
    void unwrap(entitlementsApi.GET('/api/v1/entitlements/plans'))
      .then((list) => (plans = list))
      .catch((error: unknown) => (problem = asProblem(error)));
    void accountPlan.load();
    void probeBilling().then((status) => {
      if (!status) return;
      void unwrap(billingApi.GET('/api/v1/billing/operator/prices'))
        .then((answer) => (prices = answer.prices))
        .catch(() => (prices = null));
    });
  });

  const billingOff = $derived(accountPlan.current?.billing_enabled === false);
  const priceCheck = $derived(plans && prices ? checkPlanPrices(plans, prices) : null);

  function priceText(price: OperatorPrice): { amount: string; monthly: string | null } {
    const currency = price.currency ?? '';
    const exponent = price.exponent ?? exponentOf(currency);
    const locale = getLocale();
    const amount = formatMoney(price.unit_amount ?? 0, currency, exponent, locale);
    const labelled = intervalLabel(amount, price.interval, price.interval_count) ?? amount;
    const monthly =
      price.monthly_amount !== null && !(price.interval === 'month' && price.interval_count === 1)
        ? m.op_plan_price_monthly({
            amount: formatMoney(price.monthly_amount, currency, exponent, locale),
          })
        : null;
    return { amount: labelled, monthly };
  }
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
  {#if priceCheck?.orphans.length}
    <div class="pad tight" data-testid="plan-price-orphans">
      <ProblemNotice
        tone="info"
        title={m.op_plan_price_orphans()}
        detail={priceCheck.orphans
          .map((p) => m.op_plan_price_orphan({ price: p.price_id, plan: p.plan }))
          .join(' ')}
      />
    </div>
  {/if}
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
        {#if priceCheck}
          {@const entry = priceCheck.byPlan[plan.key]}
          {#if entry}
            <div class="price" data-testid="plan-price-{plan.key}" data-state={entry.state}>
              <span class="price-label">{m.op_plan_price()}</span>
              {#if entry.state === 'priced'}
                {@const text = priceText(entry.price)}
                <span class="price-amount">{text.amount}</span>
                {#if text.monthly}<span class="price-monthly">{text.monthly}</span>{/if}
                <code class="price-id">{entry.price.price_id}</code>
              {:else if entry.state === 'unreadable'}
                <span class="price-warn">
                  <Icon name="alert" size={14} />
                  {m.op_plan_price_unreadable({ price: entry.price.price_id })}
                </span>
              {:else if entry.state === 'missing'}
                <span class="price-warn">
                  <Icon name="alert" size={14} />
                  {m.op_plan_price_missing()}
                </span>
              {:else}
                <span class="price-free">{m.op_plan_price_free()}</span>
              {/if}
            </div>
          {/if}
        {/if}
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
  {#if priceCheck}
    <p class="pad quiet fine"><Icon name="key" size={14} />{m.op_plan_price_readonly()}</p>
  {/if}
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

  .price {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--md-space-1) var(--md-space-3);
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-start: 3px solid var(--md-color-ink);
    background: var(--md-color-surface-sunken);
    font-size: var(--md-font-size-13);
  }

  .price[data-state='missing'],
  .price[data-state='unreadable'] {
    border-inline-start-style: dashed;
  }

  .price-label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .price-amount {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-16);
    font-variant-numeric: tabular-nums;
  }

  .price-monthly,
  .price-free,
  .price-id {
    color: var(--md-color-ink-muted);
  }

  .price-id {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    overflow-wrap: anywhere;
  }

  .price-warn {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    font-weight: var(--md-weight-semibold);
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
