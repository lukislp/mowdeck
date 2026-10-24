<script lang="ts">
  import {
    Button,
    Icon,
    MowerCard,
    ProblemNotice,
    TopBar,
    type Activity,
    type RtkFix,
  } from '@mowdeck/ui';

  import { fleetApi, telemetryApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { session } from '#lib/auth/session.svelte.js';
  import { clock } from '#lib/clock.svelte.js';
  import LiveStatus from '#lib/components/LiveStatus.svelte';
  import PlanBanner from '#lib/components/PlanBanner.svelte';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import type { components } from '#lib/generated/fleet.js';
  import {
    activityLabel,
    connectionLabel,
    formatAge,
    formatNumber,
    problemMessage,
    rtkLabel,
  } from '#lib/i18n.js';
  import { realtime } from '#lib/live.svelte.js';
  import { displayModel } from '#lib/mower/limits.js';
  import { m } from '#lib/paraglide/messages.js';
  import { lastLoad } from '#lib/stores/last-load.js';
  import { mowers as states } from '#lib/stores/mowers.svelte.js';
  import { install } from '#lib/stores/ui.svelte.js';
  import { displaySites } from '#lib/fleet/data.js';
  import {
    QUICK_FILTERS,
    passesQuick,
    sectionsBySite,
    type QuickFilter,
  } from '#lib/fleet/places.js';

  type Mower = components['schemas']['MowerOut'];
  type Site = components['schemas']['SiteOut'];

  // The last list of this tab shows at once while the fresh one loads (or fails).
  let list = $state<Mower[] | null>(lastLoad.get<Mower[]>('mowers') ?? null);
  let sites = $state<Site[]>([]);
  let models = $state<Record<string, string>>({});
  let problem = $state<ApiProblem | null>(null);
  let quick = $state<QuickFilter>('all');
  let collapsed = $state<Record<string, boolean>>({});

  // The real model names come from the capabilities; a failure only leaves Fleet's names.
  async function loadModels(): Promise<Record<string, string>> {
    try {
      const found = await unwrap(
        telemetryApi.GET('/api/v1/telemetry/accounts/me/mowers/capabilities'),
      );
      const names: Record<string, string> = {};
      for (const item of found.items) {
        const model = displayModel(item.capabilities, null);
        if (model) names[String(item.mower_id)] = model;
      }
      return names;
    } catch {
      return {};
    }
  }

  async function load(): Promise<void> {
    problem = null;
    try {
      const [fleet, telemetry, names] = await Promise.all([
        unwrap(fleetApi.GET('/api/v1/fleet/mowers')),
        unwrap(telemetryApi.GET('/api/v1/telemetry/accounts/me/mowers/states')),
        loadModels(),
      ]);
      for (const item of telemetry.items) states.upsert(String(item.mower_id), item.state, 'rest');
      models = names;
      list = fleet.items;
      lastLoad.set('mowers', fleet.items);
    } catch (error) {
      problem = asProblem(error);
      return;
    }
    // Sections per site only when the mowers live on more than one site.
    if (new Set(list.map((mower) => mower.site_id)).size > 1) {
      try {
        const found = await unwrap(
          fleetApi.GET('/api/v1/fleet/sites', { params: { query: { include_default: true } } }),
        );
        sites = displaySites(found.items);
      } catch {
        sites = [];
      }
    }
  }

  $effect(() => {
    void load();
  });
  $effect(() => clock.hold(5_000));

  const ids = $derived((list ?? []).map((mower) => mower.id).slice(0, 200));
  $effect(() => {
    if (ids.length) return realtime.watch(ids);
  });

  const RANK: Record<string, number> = { error: 0, mowing: 1, returning: 2, paused: 3 };
  const rows = $derived(
    (list ?? [])
      .map((mower) => ({ mower, snap: states.get(mower.id) }))
      .sort((a, b) => {
        const ra = RANK[a.snap?.state.activity ?? ''] ?? 9;
        const rb = RANK[b.snap?.state.activity ?? ''] ?? 9;
        return ra - rb || a.mower.name.localeCompare(b.mower.name);
      }),
  );
  const QUICK_LABELS: Record<QuickFilter, () => string> = {
    all: () => m.mowers_filter_all(),
    active: () => m.mowers_filter_active(),
    attention: () => m.mowers_filter_attention(),
    docked: () => m.mowers_filter_docked(),
  };
  const passes = (filter: QuickFilter, row: (typeof rows)[number]) =>
    passesQuick(filter, row.snap?.state.activity, states.isOffline(row.mower.id, clock.now));
  const counts = $derived(
    Object.fromEntries(
      QUICK_FILTERS.map((f) => [f, rows.filter((row) => passes(f, row)).length]),
    ) as Record<QuickFilter, number>,
  );
  const filtered = $derived(rows.filter((row) => passes(quick, row)));
  const sections = $derived(
    sectionsBySite(
      filtered.map((row) => ({ ...row, site_id: row.mower.site_id })),
      sites,
    ),
  );
  const grouped = $derived(new Set((list ?? []).map((mower) => mower.site_id)).size > 1);
  const canManage = $derived(session.has('fleet.manage'));
  // Never inside the iOS/Android app or an installed web app (`install.offer` is null there).
  const showInstall = $derived(install.offer !== null && !install.hintDismissed);
</script>

<svelte:head><title>{m.mowers_title()} · {m.app_name()}</title></svelte:head>

<TopBar title={m.mowers_title()} meta={list ? m.mowers_count({ count: list.length }) : undefined}>
  {#snippet status()}
    <LiveStatus phase={realtime.phase} tooMany={realtime.tooMany} />
  {/snippet}
  {#snippet actions()}
    {#if canManage}
      <Button href="/mowers/new" icon="plus" label={m.mowers_add()} testid="add-mower" />
    {/if}
  {/snippet}
</TopBar>
<PlanBanner />

{#if showInstall}
  <div class="install" data-testid="install-hint" data-offer={install.offer}>
    <Icon name="download" size={18} />
    <span class="text">{install.offer === 'prompt' ? m.install_hint() : m.install_ios_hint()}</span>
    {#if install.offer === 'prompt'}
      <Button variant="ghost" onclick={() => install.prompt()}>{m.install_action()}</Button>
    {/if}
    <Button
      variant="ghost"
      icon="close"
      label={m.dismiss()}
      onclick={() => install.dismissHint()}
    />
  </div>
{/if}

{#if problem}
  <div class="pad problem">
    <ProblemNotice
      title={problemMessage(problem)}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
    <Button icon="play" testid="mowers-retry" onclick={() => void load()}
      >{m.loading_retry()}</Button
    >
  </div>
{/if}
{#if list === null && problem}
  <!-- nothing loaded yet: the notice above says why -->
{:else if list === null}
  <p class="quiet pad" role="status"><SlowLoading /></p>
{:else if list.length === 0}
  <section class="empty" data-testid="mowers-empty">
    <h2>{m.mowers_empty_title()}</h2>
    <p>{canManage ? m.mowers_empty_owner() : m.mowers_empty_viewer()}</p>
    {#if canManage}
      <Button href="/mowers/new" variant="primary" icon="plus">{m.mowers_add()}</Button>
    {/if}
  </section>
{:else}
  {#if list.length > 3}
    <div class="quick" role="group" aria-label={m.mowers_filter()}>
      {#each QUICK_FILTERS as f (f)}
        <button
          type="button"
          class="chip"
          aria-pressed={quick === f}
          data-testid="mowers-filter-{f}"
          onclick={() => (quick = f)}
        >
          {QUICK_LABELS[f]()}<span class="n">{counts[f]}</span>
        </button>
      {/each}
    </div>
  {/if}
  <div class="head" aria-hidden="true">
    <span>{m.col_mower()}</span>
    <span>{m.col_state()}</span>
    <span class="num">{m.col_battery()}</span>
    <span class="num">{m.col_rtk()}</span>
    <span class="num">{m.col_updated()}</span>
    <span></span>
  </div>
  {#snippet card({ mower, snap }: (typeof rows)[number])}
    {@const st = snap?.state}
    <MowerCard
      href="/mowers/{mower.id}"
      testid="mower-row-{mower.id}"
      name={mower.name}
      detail={[
        connectionLabel(mower.connection_kind),
        displayModel({ model: models[mower.id] }, mower),
      ]
        .filter(Boolean)
        .join(' · ')}
      activity={(st?.activity ?? 'unknown') as Activity}
      activityLabel={st ? activityLabel(st.activity as Activity) : m.activity_no_telemetry()}
      battery={st ? formatNumber(st.battery_percent) : null}
      charging={st?.charging ?? false}
      rtk={(st?.rtk_fix ?? 'unknown') as RtkFix}
      rtkLabel={rtkLabel((st?.rtk_fix ?? 'unknown') as RtkFix)}
      age={st ? formatAge(st.observed_at, clock.now) : m.no_data()}
      stale={states.isOffline(mower.id, clock.now)}
      error={st?.error_code ? `${st.error_code} ${st.error_message ?? ''}`.trim() : null}
    />
  {/snippet}
  <div data-testid="mower-list">
    {#if grouped}
      {#each sections as section (section.id)}
        {@const open = !collapsed[section.id]}
        <section class="section" data-testid="mower-section-{section.id || 'other'}">
          <button
            type="button"
            class="section-head"
            aria-expanded={open}
            onclick={() => (collapsed = { ...collapsed, [section.id]: open })}
          >
            <Icon name={open ? 'collapse' : 'expand'} size={16} />
            <span class="section-name">{section.site?.name ?? m.mowers_other_site()}</span>
            <span class="section-count">{m.mowers_count({ count: section.items.length })}</span>
          </button>
          {#if open}
            <ul class="rows">
              {#each section.items as row (row.mower.id)}
                <li>{@render card(row)}</li>
              {/each}
            </ul>
          {/if}
        </section>
      {/each}
    {:else}
      <ul class="rows">
        {#each filtered as row (row.mower.id)}
          <li>{@render card(row)}</li>
        {/each}
      </ul>
    {/if}
    {#if filtered.length === 0}
      <p class="quiet pad" data-testid="mowers-filter-empty">{m.mowers_filter_empty()}</p>
    {/if}
  </div>
  {#if (list?.length ?? 0) > 200}
    <p class="quiet pad">{m.mowers_live_limited()}</p>
  {/if}
{/if}

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .problem {
    display: grid;
    justify-items: start;
    gap: var(--md-space-3);
  }

  .quiet {
    color: var(--md-color-ink-muted);
  }

  .install {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    padding: 0 var(--md-space-2) 0 var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface-sunken);
    font-size: var(--md-font-size-14);
  }

  .install .text {
    flex: 1;
  }

  .rows {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .head {
    display: none;
  }

  .quick {
    display: flex;
    gap: var(--md-space-2);
    padding: var(--md-space-2) var(--md-gutter);
    overflow-x: auto;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .chip {
    display: inline-flex;
    flex: none;
    align-items: center;
    gap: var(--md-space-2);
    min-height: var(--md-target);
    padding: 0 var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    cursor: pointer;
  }

  .chip[aria-pressed='true'] {
    border-color: var(--md-color-ink);
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .chip:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .n {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
    letter-spacing: 0;
  }

  .section-head {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    width: 100%;
    min-height: var(--md-target);
    padding: 0 var(--md-gutter);
    border: 0;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    border-radius: var(--md-radius-0);
    background: var(--md-color-surface-sunken);
    color: var(--md-color-ink);
    text-align: start;
    cursor: pointer;
  }

  .section-head:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .section-name {
    flex: 1;
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .section-count {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
  }

  .empty {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--md-space-3);
    max-width: 520px;
    padding: var(--md-space-8) var(--md-gutter);
  }

  .empty p {
    color: var(--md-color-ink-muted);
  }

  @media (min-width: 768px) {
    .head {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 150px 80px 120px 110px 20px;
      column-gap: var(--md-space-3);
      padding: var(--md-space-2) var(--md-gutter);
      border-bottom: var(--md-hairline) solid var(--md-color-line);
      color: var(--md-color-ink-muted);
      font-size: var(--md-font-size-11);
      font-weight: var(--md-weight-semibold);
      letter-spacing: var(--md-label-tracking);
      text-transform: uppercase;
    }

    .head .num {
      text-align: end;
    }
  }
</style>
