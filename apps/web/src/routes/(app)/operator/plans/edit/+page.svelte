<!--
  Operator console, plan editor: `?key=<key>` edits a plan (`GET /entitlements/plans/{key}`, also
  inactive ones), without it a new plan is created. Limits are whole numbers, empty means
  unlimited; features come from the catalogue (`GET /entitlements/features`, grouped by module),
  plus the keys the plan already has that no deployed module declares. Saving sends the whole plan
  (`PUT /entitlements/plans/{key}`); every service's cached snapshot is dropped at once.
-->
<script lang="ts">
  import { Button, Field, Icon, ProblemNotice, Switch, TopBar } from '@mowdeck/ui';

  import { goto } from '$app/navigation';
  import { page } from '$app/state';

  import { entitlementsApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import { problemMessage } from '#lib/i18n.js';
  import {
    planBody,
    planForm,
    toggleFeature,
    validatePlan,
    type PlanForm,
    type PlanIssue,
  } from '#lib/operator/plans.js';
  import { m } from '#lib/paraglide/messages.js';
  import { featureLabel, limitLabel } from '#lib/plan/labels.js';
  import { accountPlan } from '#lib/plan/plan.svelte.js';
  import { LIMIT_KEYS, type Feature, type PlanDetail } from '#lib/plan/types.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  const key = $derived(page.url.searchParams.get('key'));
  const creating = $derived(!key);

  let plan = $state<PlanDetail | null>(null);
  let catalogue = $state<Feature[]>([]);
  let form = $state<PlanForm>(planForm(null));
  let problem = $state<ApiProblem | null>(null);
  let loaded = $state(false);
  let tried = $state(false);
  let saving = $state(false);
  let saveProblem = $state<ApiProblem | null>(null);

  $effect(() => {
    const wanted = key;
    loaded = false;
    problem = null;
    void accountPlan.load();
    void Promise.all([
      wanted
        ? unwrap(
            entitlementsApi.GET('/api/v1/entitlements/plans/{key}', {
              params: { path: { key: wanted } },
            }),
          )
        : Promise.resolve(null),
      unwrap(entitlementsApi.GET('/api/v1/entitlements/features')).catch(() => [] as Feature[]),
    ])
      .then(([found, features]) => {
        plan = found;
        catalogue = features.filter((f) => !f.retired);
        form = planForm(found);
        loaded = true;
      })
      .catch((error: unknown) => (problem = asProblem(error)));
  });

  const issues = $derived(tried ? validatePlan(form) : {});
  const allFeatures = $derived(form.features.includes('*'));
  const modules = $derived.by(() => {
    const groups = new Map<string, Feature[]>();
    for (const feature of catalogue) {
      const list = groups.get(feature.module) ?? [];
      list.push(feature);
      groups.set(feature.module, list);
    }
    return [...groups.entries()]
      .map(([module, list]) => [module, list.sort((a, b) => a.key.localeCompare(b.key))] as const)
      .sort(([a], [b]) => a.localeCompare(b));
  });
  const known = $derived(new Set(catalogue.map((f) => f.key)));
  const extra = $derived(form.features.filter((f) => f !== '*' && !known.has(f)).sort());
  const billingOff = $derived(accountPlan.current?.billing_enabled === false);

  function issueText(issue: PlanIssue | undefined): string | null {
    switch (issue) {
      case 'required':
        return m.sched_err_required();
      case 'key':
        return m.op_plan_key_invalid();
      case 'number':
        return m.op_plan_limit_invalid();
      case 'trial':
        return m.op_plan_trial_invalid();
      default:
        return null;
    }
  }

  async function save(): Promise<void> {
    tried = true;
    saveProblem = null;
    if (Object.keys(validatePlan(form)).length) return;
    saving = true;
    const target = form.key.trim();
    try {
      await unwrap(
        entitlementsApi.PUT('/api/v1/entitlements/plans/{key}', {
          params: { path: { key: target } },
          body: planBody(form),
        }),
      );
      toasts.push('ok', m.op_plan_saved({ name: form.name.trim() }));
      await goto('/operator/plans');
    } catch (error) {
      saveProblem = asProblem(error);
    } finally {
      saving = false;
    }
  }
</script>

<svelte:head
  ><title>{creating ? m.op_plan_new() : (plan?.name ?? key)} · {m.app_name()}</title></svelte:head
>

<TopBar
  title={creating ? m.op_plan_new() : m.op_plan_edit_title({ name: plan?.name ?? key ?? '' })}
  meta={plan?.key}
  backHref="/operator/plans"
  backLabel={m.back()}
/>

{#if problem}
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if !loaded}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{:else}
  <form
    class="editor"
    novalidate
    data-testid="plan-editor"
    onsubmit={(event) => {
      event.preventDefault();
      void save();
    }}
  >
    {#if billingOff}
      <div class="span">
        <ProblemNotice
          tone="info"
          title={m.op_billing_off_title()}
          detail={m.op_plans_billing_off()}
        />
      </div>
    {/if}
    {#if plan?.from_plan_file}
      <div class="span">
        <ProblemNotice tone="info" title={m.op_plan_from_file()} />
      </div>
    {/if}

    <section class="card" aria-labelledby="plan-basics">
      <h2 id="plan-basics">{m.op_plan_basics()}</h2>
      <Field
        id="plan-key"
        label={m.op_plan_key()}
        hint={m.op_plan_key_hint()}
        mono
        required
        disabled={!creating}
        maxlength={64}
        bind:value={form.key}
        error={issueText(issues.key)}
        testid="plan-key"
      />
      <Field
        id="plan-name"
        label={m.op_plan_name()}
        required
        maxlength={200}
        bind:value={form.name}
        error={issueText(issues.name)}
        testid="plan-name-input"
      />
      <Field
        id="plan-description"
        label={m.op_plan_description()}
        maxlength={1000}
        bind:value={form.description}
      />
      <Field
        id="plan-trial"
        type="number"
        inputmode="numeric"
        label={m.op_plan_trial()}
        hint={m.op_plan_trial_hint()}
        bind:value={form.trialDays}
        error={issueText(issues.trialDays)}
      />
      <Switch
        id="plan-default"
        label={m.op_plan_default()}
        hint={m.op_plan_default_hint()}
        states={{ on: m.switch_on(), off: m.switch_off() }}
        checked={form.isDefault}
        onchange={(checked) => (form.isDefault = checked)}
      />
      <Switch
        id="plan-active"
        label={m.op_plan_active()}
        hint={m.op_plan_active_hint()}
        states={{ on: m.switch_on(), off: m.switch_off() }}
        checked={form.active}
        onchange={(checked) => (form.active = checked)}
      />
    </section>

    <section class="card" aria-labelledby="plan-limits">
      <h2 id="plan-limits">{m.plan_limits()}</h2>
      <p class="quiet small">{m.op_plan_limits_hint()}</p>
      <div class="limit-grid">
        {#each LIMIT_KEYS as limit (limit)}
          <Field
            id="plan-limit-{limit}"
            type="number"
            inputmode="numeric"
            label={limitLabel(limit)}
            placeholder={m.plan_unlimited()}
            mono
            bind:value={form.limits[limit]}
            error={issueText(issues[limit])}
            testid="plan-limit-input-{limit}"
          />
        {/each}
      </div>
    </section>

    <section class="card span" aria-labelledby="plan-features">
      <h2 id="plan-features">{m.plan_features()}</h2>
      <Switch
        id="plan-all-features"
        label={m.plan_feature_all()}
        hint={m.op_plan_all_hint()}
        states={{ on: m.switch_on(), off: m.switch_off() }}
        checked={allFeatures}
        onchange={(checked) => (form.features = toggleFeature(form.features, '*', checked))}
      />
      <div class="modules">
        {#each modules as [module, features] (module)}
          <fieldset class="module">
            <legend>{module}</legend>
            {#each features as feature (feature.key)}
              {@const on = form.features.includes(feature.key)}
              <button
                type="button"
                class="toggle"
                aria-pressed={on}
                disabled={allFeatures}
                data-testid="plan-feature-{feature.key}"
                onclick={() => (form.features = toggleFeature(form.features, feature.key, !on))}
              >
                <span class="box" aria-hidden="true"
                  >{#if on || allFeatures}<Icon name="check" size={16} />{/if}</span
                >
                <span class="who">
                  <span class="name">{featureLabel(feature.key, feature.description)}</span>
                  <span class="meta">{feature.key}</span>
                </span>
              </button>
            {/each}
          </fieldset>
        {/each}
        {#if extra.length}
          <fieldset class="module">
            <legend>{m.op_plan_unknown_features()}</legend>
            {#each extra as feature (feature)}
              <button
                type="button"
                class="toggle"
                aria-pressed="true"
                onclick={() => (form.features = toggleFeature(form.features, feature, false))}
              >
                <span class="box" aria-hidden="true"><Icon name="check" size={16} /></span>
                <span class="who">
                  <span class="name">{featureLabel(feature)}</span>
                  <span class="meta">{feature}</span>
                </span>
              </button>
            {/each}
          </fieldset>
        {/if}
      </div>
    </section>

    {#if saveProblem}
      <div class="span">
        <ProblemNotice
          title={problemMessage(saveProblem)}
          detail={saveProblem.detail}
          reference={saveProblem.correlationId}
          referenceLabel={m.problem_reference()}
        />
      </div>
    {/if}
    <div class="span footer">
      <Button variant="ghost" href="/operator/plans">{m.cancel()}</Button>
      <Button variant="primary" type="submit" icon="check" pending={saving} testid="plan-save"
        >{m.save()}</Button
      >
    </div>
  </form>
{/if}

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .quiet {
    margin: 0;
    color: var(--md-color-ink-muted);
  }

  .small {
    font-size: var(--md-font-size-13);
  }

  .editor {
    display: grid;
    gap: var(--md-space-4);
    padding: var(--md-space-4) var(--md-gutter) var(--md-space-8);
  }

  .card {
    display: grid;
    align-content: start;
    gap: var(--md-space-4);
    padding: var(--md-space-4);
    border: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  h2,
  legend {
    margin: 0;
    padding: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .limit-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--md-space-3);
  }

  .modules {
    display: grid;
    gap: var(--md-space-4);
  }

  .module {
    display: grid;
    align-content: start;
    margin: 0;
    padding: 0;
    border: 0;
  }

  .module legend {
    padding-bottom: var(--md-space-1);
    font-family: var(--md-font-mono);
  }

  .toggle {
    display: flex;
    align-items: center;
    gap: var(--md-space-3);
    width: 100%;
    min-height: var(--md-target);
    padding: 0 var(--md-space-1);
    border: 0;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    border-radius: var(--md-radius-0);
    background: none;
    color: var(--md-color-ink);
    text-align: start;
    cursor: pointer;
  }

  .toggle:disabled {
    cursor: default;
    opacity: 0.6;
  }

  .toggle:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .box {
    display: grid;
    place-items: center;
    flex: none;
    width: 24px;
    height: 24px;
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
  }

  .toggle[aria-pressed='true'] .box {
    border-color: var(--md-color-ink);
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .who {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .name {
    font-size: var(--md-font-size-14);
  }

  .meta {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
  }

  .footer {
    display: flex;
    justify-content: flex-end;
    gap: var(--md-space-2);
  }

  @media (min-width: 1024px) {
    .editor {
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    }

    .span {
      grid-column: 1 / -1;
    }

    .modules {
      grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    }
  }
</style>
