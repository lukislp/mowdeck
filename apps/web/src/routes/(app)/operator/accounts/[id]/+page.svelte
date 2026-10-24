<!--
  Operator console, one account: Identity's record (name, kind, status, owner, created), the plan
  Entitlements stores for it and the plan that applies now, and the plan history (newest first:
  plan, status, source, reason, who). "Assign plan" opens a sheet with the plan picker, the
  status, an optional end date and the required reason (`PUT /entitlements/accounts/{id}/plan`).
  No mower data is read or shown.
-->
<script lang="ts">
  import { Button, Field, Icon, ProblemNotice, Select, Sheet, TopBar } from '@mowdeck/ui';

  import { page } from '$app/state';

  import { entitlementsApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import PlanStatusBadge from '#lib/components/PlanStatusBadge.svelte';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import { formatDate, formatDateTime, problemMessage } from '#lib/i18n.js';
  import { findPlatformAccount, type PlatformAccount } from '#lib/operator/accounts.js';
  import {
    assignForm,
    assignmentBody,
    takesUntil,
    validateAssignment,
    type AssignForm,
  } from '#lib/operator/assign.js';
  import { accountKindLabel, accountStatusLabel } from '#lib/operator/labels.js';
  import { m } from '#lib/paraglide/messages.js';
  import { sourceLabel, statusLabel } from '#lib/plan/labels.js';
  import {
    PLAN_STATUSES,
    type AccountDetail,
    type Plan,
    type PlanStatus,
  } from '#lib/plan/types.js';
  import { statusUntil } from '#lib/plan/usage.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  const id = $derived(page.params.id ?? '');

  let detail = $state<AccountDetail | null>(null);
  let identity = $state<PlatformAccount | null | undefined>(undefined);
  let plans = $state<Plan[]>([]);
  let problem = $state<ApiProblem | null>(null);

  let sheetOpen = $state(false);
  let form = $state<AssignForm>(assignForm(''));
  let tried = $state(false);
  let saving = $state(false);
  let saveProblem = $state<ApiProblem | null>(null);

  async function loadDetail(accountId: string): Promise<void> {
    try {
      detail = await unwrap(
        entitlementsApi.GET('/api/v1/entitlements/accounts/{account_id}', {
          params: { path: { account_id: accountId } },
        }),
      );
    } catch (error) {
      problem = asProblem(error);
    }
  }

  $effect(() => {
    const accountId = id;
    problem = null;
    detail = null;
    identity = undefined;
    void loadDetail(accountId);
    void findPlatformAccount(accountId)
      .then((found) => (identity = found))
      .catch(() => (identity = null));
    void unwrap(entitlementsApi.GET('/api/v1/entitlements/plans'))
      .then((list) => (plans = list))
      .catch(() => (plans = []));
  });

  const name = $derived(identity?.name ?? detail?.account_name ?? id);
  const issues = $derived(tried ? validateAssignment(form) : []);

  function openSheet(): void {
    const current = detail?.assigned;
    form = assignForm(current?.plan ?? plans[0]?.key ?? '', current?.status ?? 'active');
    tried = false;
    saveProblem = null;
    sheetOpen = true;
  }

  async function save(): Promise<void> {
    tried = true;
    saveProblem = null;
    if (validateAssignment(form).length) return;
    saving = true;
    try {
      await unwrap(
        entitlementsApi.PUT('/api/v1/entitlements/accounts/{account_id}/plan', {
          params: { path: { account_id: id } },
          body: assignmentBody(form, identity?.name ?? detail?.account_name ?? null),
        }),
      );
      const planName = plans.find((p) => p.key === form.plan)?.name ?? form.plan;
      toasts.push('ok', m.op_assigned({ plan: planName, account: name }));
      sheetOpen = false;
      await loadDetail(id);
    } catch (error) {
      saveProblem = asProblem(error);
    } finally {
      saving = false;
    }
  }
</script>

<svelte:head><title>{name} · {m.op_accounts_title()} · {m.app_name()}</title></svelte:head>

<TopBar
  title={name}
  meta={identity?.owner_username ?? undefined}
  backHref="/operator"
  backLabel={m.back()}
>
  {#snippet actions()}
    {#if detail}
      <Button variant="primary" icon="shield" testid="operator-assign" onclick={openSheet}
        >{m.op_assign()}</Button
      >
    {/if}
  {/snippet}
</TopBar>

{#if problem}
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if !detail}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{:else}
  {@const assigned = detail.assigned}
  {@const effective = detail.effective}
  <div class="layout" data-testid="operator-account">
    <section class="card" aria-labelledby="facts-head">
      <h2 id="facts-head">{m.op_account_facts()}</h2>
      <dl class="facts">
        <div>
          <dt>{m.op_col_owner()}</dt>
          <dd class="mono">{identity?.owner_username ?? m.no_data()}</dd>
        </div>
        <div>
          <dt>{m.op_kind()}</dt>
          <dd>{identity ? accountKindLabel(identity.kind) : m.no_data()}</dd>
        </div>
        <div>
          <dt>{m.op_status()}</dt>
          <dd>{identity ? accountStatusLabel(identity.status) : m.no_data()}</dd>
        </div>
        <div>
          <dt>{m.op_col_created()}</dt>
          <dd class="mono">{identity ? formatDate(identity.created_at) : m.no_data()}</dd>
        </div>
        <div class="wide">
          <dt>{m.op_account_id()}</dt>
          <dd class="mono id">{detail.account_id}</dd>
        </div>
      </dl>
      <p class="fine"><Icon name="shield" size={14} />{m.op_no_mower_data()}</p>
    </section>

    <section class="card" aria-labelledby="plan-head" data-testid="operator-account-plan">
      <h2 id="plan-head">{m.op_plan_section()}</h2>
      <div class="plates">
        <div class="plate" data-testid="operator-assigned" data-plan={assigned?.plan ?? ''}>
          <span class="eyebrow">{m.op_plan_assigned()}</span>
          {#if assigned}
            <span class="big mono">{assigned.plan}</span>
            <PlanStatusBadge status={assigned.status} />
            <span class="small">
              {sourceLabel(assigned.source)} · {m.plan_since({ date: formatDate(assigned.since) })}
              {#if statusUntil(assigned)}
                · {m.op_until({ date: formatDate(statusUntil(assigned) ?? '') })}
              {/if}
            </span>
          {:else}
            <span class="big quiet">{m.op_plan_none()}</span>
          {/if}
        </div>
        <div class="plate applies" data-testid="operator-effective" data-plan={effective.plan}>
          <span class="eyebrow">{m.op_plan_effective()}</span>
          <span class="big">{effective.plan_name}</span>
          <PlanStatusBadge status={effective.status} />
          {#if !effective.billing_enabled}
            <span class="small">{m.op_effective_billing_off()}</span>
          {/if}
        </div>
      </div>
    </section>

    <section class="card history" aria-labelledby="history-head">
      <h2 id="history-head">{m.op_history()}</h2>
      {#if detail.history.length === 0}
        <p class="quiet">{m.op_history_none()}</p>
      {:else}
        <ol class="timeline" data-testid="operator-history">
          {#each detail.history as entry, index (index)}
            <li class="entry" data-plan={entry.plan} data-status={entry.status}>
              <span class="dot" aria-hidden="true"></span>
              <div class="entry-head">
                <span class="plan-key">{entry.plan}</span>
                <PlanStatusBadge status={entry.status} />
                <span class="when">{formatDateTime(entry.at)}</span>
              </div>
              <span class="reason">{entry.reason ?? m.op_no_reason()}</span>
              <span class="small">
                {sourceLabel(entry.source)}
                {#if entry.actor}· {m.op_actor({ actor: entry.actor })}{/if}
              </span>
            </li>
          {/each}
        </ol>
      {/if}
    </section>
  </div>
{/if}

<Sheet
  open={sheetOpen}
  title={m.op_assign_title({ account: name })}
  closeLabel={m.close()}
  onclose={() => (sheetOpen = false)}
  testid="operator-assign-sheet"
>
  <div class="form">
    <div class="picker" role="radiogroup" aria-label={m.op_assign_plan()}>
      <span class="eyebrow">{m.op_assign_plan()}</span>
      {#each plans as plan (plan.key)}
        <button
          type="button"
          class="option"
          role="radio"
          aria-checked={form.plan === plan.key}
          data-testid="assign-plan-{plan.key}"
          onclick={() => (form.plan = plan.key)}
        >
          <span class="radio" aria-hidden="true"
            >{#if form.plan === plan.key}<Icon name="check" size={16} />{/if}</span
          >
          <span class="option-text">
            <span class="option-name">{plan.name} <span class="mono quiet">{plan.key}</span></span>
            {#if plan.description}<span class="small">{plan.description}</span>{/if}
          </span>
        </button>
      {/each}
      {#if issues.includes('plan')}<span class="issue">{m.op_assign_plan_required()}</span>{/if}
    </div>
    <Select
      id="assign-status"
      label={m.op_status()}
      options={PLAN_STATUSES.map((s) => ({ value: s, label: statusLabel(s) }))}
      bind:value={() => form.status, (value) => (form.status = value as PlanStatus)}
    />
    {#if takesUntil(form.status)}
      <Field
        id="assign-until"
        type="date"
        label={form.status === 'trial' ? m.op_assign_trial_until() : m.op_assign_grace_until()}
        hint={m.op_assign_until_hint()}
        bind:value={form.until}
        error={issues.includes('until') ? m.op_assign_until_invalid() : null}
        testid="assign-until"
      />
    {/if}
    {#if form.status === 'grace' || form.status === 'expired'}
      <ProblemNotice tone="info" title={m.op_assign_restricted()} />
    {/if}
    <Field
      id="assign-reason"
      label={m.op_assign_reason()}
      hint={m.op_assign_reason_hint()}
      required
      maxlength={500}
      bind:value={form.reason}
      error={issues.includes('reason') ? m.op_assign_reason_required() : null}
      testid="assign-reason"
    />
    {#if saveProblem}
      <ProblemNotice
        title={problemMessage(saveProblem)}
        detail={saveProblem.detail}
        reference={saveProblem.correlationId}
        referenceLabel={m.problem_reference()}
      />
    {/if}
  </div>
  {#snippet footer()}
    <Button variant="primary" full icon="check" pending={saving} testid="assign-save" onclick={save}
      >{m.op_assign_save()}</Button
    >
  {/snippet}
</Sheet>

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .quiet {
    margin: 0;
    color: var(--md-color-ink-muted);
  }

  .mono {
    font-family: var(--md-font-mono);
  }

  .layout {
    display: grid;
    gap: var(--md-space-4);
    padding: var(--md-space-4) var(--md-gutter) var(--md-space-8);
  }

  .card {
    display: grid;
    align-content: start;
    gap: var(--md-space-3);
    padding: var(--md-space-4);
    border: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  h2,
  .eyebrow,
  dt {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .facts {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--md-space-3);
    margin: 0;
  }

  .facts div {
    display: grid;
    gap: var(--md-space-0_5);
  }

  .facts .wide {
    grid-column: 1 / -1;
  }

  dd {
    margin: 0;
    font-size: var(--md-font-size-14);
  }

  .id {
    font-size: var(--md-font-size-12);
    overflow-wrap: anywhere;
  }

  .fine {
    display: flex;
    align-items: center;
    gap: var(--md-space-1);
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .plates {
    display: grid;
    gap: var(--md-space-3);
  }

  .plate {
    display: grid;
    justify-items: start;
    gap: var(--md-space-1);
    padding: var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-inline-start: 4px solid var(--md-color-ink);
    background: var(--md-color-surface-sunken);
  }

  .plate.applies {
    border-inline-start-color: var(--md-state-mowing);
  }

  .big {
    font-size: var(--md-font-size-24);
    font-weight: var(--md-weight-semibold);
    line-height: var(--md-line-28);
  }

  .small {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .timeline {
    display: grid;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .entry {
    position: relative;
    display: grid;
    gap: var(--md-space-1);
    padding: 0 0 var(--md-space-4) var(--md-space-6);
    border-inline-start: var(--md-hairline) solid var(--md-color-line-strong);
    margin-inline-start: var(--md-space-2);
  }

  .entry:last-child {
    border-inline-start-color: transparent;
  }

  .dot {
    position: absolute;
    inset-inline-start: -6px;
    top: 4px;
    width: 11px;
    height: 11px;
    border: var(--md-border-control) solid var(--md-color-ink);
    background: var(--md-color-surface);
  }

  .entry:first-child .dot {
    background: var(--md-color-ink);
  }

  .entry-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2);
  }

  .plan-key {
    padding: 0 var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-ink);
    border-radius: var(--md-radius-1);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-24);
  }

  .when {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
  }

  .reason {
    font-size: var(--md-font-size-14);
  }

  .form {
    display: grid;
    gap: var(--md-space-4);
  }

  .picker {
    display: grid;
    gap: var(--md-space-1);
  }

  .option {
    display: flex;
    align-items: center;
    gap: var(--md-space-3);
    width: 100%;
    min-height: var(--md-target-command);
    padding: var(--md-space-2) var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-0);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    text-align: start;
    cursor: pointer;
  }

  .option[aria-checked='true'] {
    border-color: var(--md-color-ink);
    box-shadow: inset 4px 0 0 var(--md-color-ink);
  }

  .option:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .radio {
    display: grid;
    place-items: center;
    flex: none;
    width: 24px;
    height: 24px;
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-round);
  }

  .option[aria-checked='true'] .radio {
    border-color: var(--md-color-ink);
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .option-text {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .option-name {
    font-weight: var(--md-weight-semibold);
  }

  .option-name .mono {
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-regular);
  }

  .issue {
    color: var(--md-state-error);
    font-size: var(--md-font-size-13);
  }

  @media (min-width: 1024px) {
    .layout {
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    }

    .history {
      grid-column: 1 / -1;
    }

    .plates {
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    }
  }
</style>
