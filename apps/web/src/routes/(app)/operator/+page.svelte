<!--
  Operator console, accounts: every account of the installation (Identity) with its plan
  (Entitlements), paged oldest first, with a search over the whole installation (name, owner, id,
  kind, status; Identity's full list is read, it has no search parameter). A row opens the account
  with its plan history and the "assign plan" sheet. No mower data: neither service returns any.
-->
<script lang="ts">
  import { Button, Field, Icon, ProblemNotice, TopBar } from '@mowdeck/ui';

  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import PlanStatusBadge from '#lib/components/PlanStatusBadge.svelte';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import { formatDate, formatNumber, problemMessage } from '#lib/i18n.js';
  import {
    loadAccountPage,
    PAGE_SIZE,
    searchAccounts,
    type AccountPage,
  } from '#lib/operator/accounts.js';
  import { accountKindLabel, accountStatusLabel } from '#lib/operator/labels.js';
  import { m } from '#lib/paraglide/messages.js';

  let offset = $state(0);
  let data = $state<AccountPage | null>(null);
  let problem = $state<ApiProblem | null>(null);
  let loading = $state(false);
  let query = $state('');

  async function load(at: number): Promise<void> {
    loading = true;
    problem = null;
    try {
      data = await loadAccountPage(at, PAGE_SIZE);
      offset = at;
    } catch (error) {
      problem = asProblem(error);
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    void load(0);
  });

  // The search runs 300 ms after the last keystroke; an older answer never replaces a newer one.
  let found = $state<AccountPage | null>(null);
  let searching = $state(false);
  let sequence = 0;

  $effect(() => {
    const text = query.trim();
    const mine = ++sequence;
    if (!text) {
      found = null;
      searching = false;
      return;
    }
    searching = true;
    problem = null;
    const timer = setTimeout(() => {
      searchAccounts(text, PAGE_SIZE)
        .then((page) => {
          if (mine === sequence) found = page;
        })
        .catch((error: unknown) => {
          if (mine === sequence) problem = asProblem(error);
        })
        .finally(() => {
          if (mine === sequence) searching = false;
        });
    }, 300);
    return () => clearTimeout(timer);
  });

  const searchMode = $derived(query.trim() !== '');
  const shown = $derived(searchMode ? found : data);
  const rows = $derived(shown?.rows ?? []);
  const last = $derived(data ? Math.min(data.total, offset + data.rows.length) : 0);
</script>

<svelte:head><title>{m.op_accounts_title()} · {m.app_name()}</title></svelte:head>

<TopBar
  title={m.op_accounts_title()}
  meta={searchMode
    ? found
      ? m.op_matches({ count: found.total })
      : undefined
    : data
      ? m.op_accounts_total({ count: data.total })
      : undefined}
/>

{#if (data ?? found)?.billingEnabled === false}
  <div class="pad tight">
    <ProblemNotice tone="info" title={m.op_billing_off_title()} detail={m.op_billing_off_body()} />
  </div>
{/if}

<div class="tools">
  <div class="search">
    <Field
      id="operator-search"
      type="search"
      label={m.op_search()}
      hint={m.op_search_hint()}
      bind:value={query}
      testid="operator-search"
    />
  </div>
  {#if !searchMode && data && data.total > PAGE_SIZE}
    <div class="pager">
      <span class="range"
        >{m.op_page_range({
          from: formatNumber(offset + 1),
          to: formatNumber(last),
          total: formatNumber(data.total),
        })}</span
      >
      <Button
        variant="ghost"
        icon="back"
        label={m.op_page_prev()}
        disabled={offset === 0 || loading}
        onclick={() => load(Math.max(0, offset - PAGE_SIZE))}
      />
      <Button
        variant="ghost"
        icon="chevron"
        label={m.op_page_next()}
        disabled={last >= data.total || loading}
        onclick={() => load(offset + PAGE_SIZE)}
      />
    </div>
  {/if}
</div>

{#if problem}
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if !shown || (searchMode && searching)}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{:else if rows.length === 0}
  <p class="pad quiet" data-testid="operator-accounts-empty">{m.op_accounts_none()}</p>
{:else}
  <div
    class="table"
    role="table"
    aria-label={m.op_accounts_title()}
    data-testid="operator-accounts"
  >
    <div class="row head" role="row">
      <span role="columnheader">{m.op_col_account()}</span>
      <span role="columnheader">{m.op_col_owner()}</span>
      <span role="columnheader">{m.op_col_plan()}</span>
      <span role="columnheader">{m.op_col_plan_status()}</span>
      <span role="columnheader">{m.op_col_applies()}</span>
      <span role="columnheader">{m.op_col_created()}</span>
    </div>
    {#each rows as row (row.id)}
      <a
        class="row"
        role="row"
        href="/operator/accounts/{row.id}"
        data-testid="operator-account-{row.id}"
        data-name={row.name}
        data-plan={row.plan?.assigned?.plan ?? ''}
      >
        <span class="cell account" role="cell">
          <span class="glyph"><Icon name="user" size={18} /></span>
          <span class="who">
            <span class="name">{row.name}</span>
            <span class="meta"
              >{accountKindLabel(row.kind)} ·
              <span class:warn={row.status !== 'active'}>{accountStatusLabel(row.status)}</span
              ></span
            >
          </span>
        </span>
        <span class="cell mono" role="cell" data-label={m.op_col_owner()}
          >{row.owner_username ?? m.no_data()}</span
        >
        <span class="cell" role="cell" data-label={m.op_col_plan()}>
          {#if row.plan?.assigned}
            <span class="plan-key">{row.plan.assigned.plan}</span>
          {:else}
            <span class="quiet">{m.op_plan_none()}</span>
          {/if}
        </span>
        <span class="cell" role="cell" data-label={m.op_col_plan_status()}>
          {#if row.plan?.assigned}
            <PlanStatusBadge status={row.plan.assigned.status} />
          {:else}
            <span class="quiet">{m.no_data()}</span>
          {/if}
        </span>
        <span class="cell" role="cell" data-label={m.op_col_applies()}>
          {#if row.plan}
            <span class="applies">{row.plan.effective.plan_name}</span>
          {:else}
            <span class="quiet">{m.op_plan_unknown()}</span>
          {/if}
        </span>
        <span class="cell mono" role="cell" data-label={m.op_col_created()}
          >{formatDate(row.created_at)}</span
        >
      </a>
    {/each}
  </div>
  {#if searchMode && found && found.total > found.rows.length}
    <p class="pad quiet">
      {m.op_matches_more({ shown: found.rows.length, total: found.total })}
    </p>
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

  .mono {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
  }

  .tools {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    justify-content: space-between;
    gap: var(--md-space-3);
    padding: var(--md-space-4) var(--md-gutter);
  }

  .search {
    flex: 1;
    min-width: 240px;
    max-width: 480px;
  }

  .pager {
    display: flex;
    align-items: center;
    gap: var(--md-space-1);
  }

  .range {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
  }

  .table {
    display: grid;
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: var(--md-space-2) var(--md-space-3);
    padding: var(--md-space-3) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    text-decoration: none;
  }

  a.row:hover {
    background: var(--md-color-surface-sunken);
  }

  a.row:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .head {
    display: none;
  }

  .cell {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    justify-content: center;
    gap: var(--md-space-0_5);
    min-width: 0;
  }

  .cell[data-label]::before {
    content: attr(data-label);
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-sans);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .account {
    grid-column: 1 / -1;
    flex-direction: row;
    align-items: center;
    justify-content: flex-start;
    gap: var(--md-space-3);
  }

  .glyph {
    display: grid;
    place-items: center;
    flex: none;
    width: 36px;
    height: 36px;
    border: var(--md-hairline) solid var(--md-color-line-strong);
  }

  .who {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .name {
    overflow: hidden;
    font-weight: var(--md-weight-semibold);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .meta {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .warn {
    color: var(--md-state-paused);
    font-weight: var(--md-weight-semibold);
  }

  .plan-key {
    align-self: flex-start;
    padding: 0 var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-ink);
    border-radius: var(--md-radius-1);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-24);
  }

  .applies {
    font-size: var(--md-font-size-14);
  }

  @media (min-width: 1024px) {
    .row {
      grid-template-columns:
        minmax(220px, 2.2fr) minmax(120px, 1.2fr) minmax(90px, 0.9fr) minmax(110px, 1fr)
        minmax(100px, 1fr) minmax(100px, 0.9fr);
      align-items: center;
      min-height: 56px;
      padding-block: var(--md-space-2);
    }

    .head {
      display: grid;
      min-height: 40px;
      background: var(--md-color-surface-sunken);
      color: var(--md-color-ink-muted);
      font-size: var(--md-font-size-11);
      font-weight: var(--md-weight-semibold);
      letter-spacing: var(--md-label-tracking);
      text-transform: uppercase;
    }

    .account {
      grid-column: auto;
    }

    .cell[data-label]::before {
      content: none;
    }
  }
</style>
