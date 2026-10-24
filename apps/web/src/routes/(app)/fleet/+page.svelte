<!--
  The fleet map: every mower the user may view on one map, live (positions from Telemetry's
  account states plus the Realtime subscription of the shown mowers), filtered by site and
  group, with a legend of counts per state, the mowers without a position, and the bulk
  commands "pause all" and "all to dock" for the filter (a confirm sheet, then the outcome per
  mower). Each mower's map areas are loaded lazily once it is shown.
-->
<script lang="ts">
  import {
    Button,
    FleetMap,
    Icon,
    ProblemNotice,
    Readout,
    Select,
    StatusBadge,
    TopBar,
    fleetLegend,
    unplaced,
    type Activity,
    type FleetMower,
    type FleetTone,
    type MapSnapshot,
  } from '@mowdeck/ui';
  import { SvelteMap } from 'svelte/reactivity';

  import { telemetryApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { session } from '#lib/auth/session.svelte.js';
  import { clock } from '#lib/clock.svelte.js';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import { loadConfig, type MapTilesConfig } from '#lib/config.js';
  import BulkSheet from '#lib/components/BulkSheet.svelte';
  import FleetTabs from '#lib/components/FleetTabs.svelte';
  import LiveStatus from '#lib/components/LiveStatus.svelte';
  import { bulkTargets, type BulkKind } from '#lib/fleet/bulk.js';
  import { loadPlaces, type Places } from '#lib/fleet/data.js';
  import { filterMowers, sortSites, type FleetFilter } from '#lib/fleet/places.js';
  import { activityLabel, formatAge, formatNumber, problemMessage } from '#lib/i18n.js';
  import { realtime } from '#lib/live.svelte.js';
  import { fleetAreaNamer } from '#lib/mower/map-labels.js';
  import { m } from '#lib/paraglide/messages.js';
  import { mowers as states } from '#lib/stores/mowers.svelte.js';

  let places = $state<Places | null>(null);
  let problem = $state<ApiProblem | null>(null);
  let tiles = $state.raw<MapTilesConfig | null>(null);
  let filter = $state<FleetFilter>({ siteId: '', groupId: '' });
  let selected = $state<string | null>(null);
  let bulk = $state<BulkKind | null>(null);
  const maps = new SvelteMap<string, MapSnapshot | null>();

  async function load(): Promise<void> {
    try {
      const [found, telemetry] = await Promise.all([
        loadPlaces(),
        unwrap(telemetryApi.GET('/api/v1/telemetry/accounts/me/mowers/states')),
      ]);
      for (const item of telemetry.items) states.upsert(String(item.mower_id), item.state, 'rest');
      places = found;
    } catch (error) {
      problem = asProblem(error);
    }
  }

  $effect(() => {
    void load();
    void loadConfig().then((config) => (tiles = config.mapTiles));
  });
  $effect(() => clock.hold(5_000));

  const sites = $derived(sortSites(places?.sites ?? []));
  const groups = $derived(
    (places?.groups ?? []).filter(
      (g) => !filter.siteId || !g.site_id || g.site_id === filter.siteId,
    ),
  );
  const shown = $derived(filterMowers(places?.mowers ?? [], filter));
  const ids = $derived(shown.map((mower) => mower.id).slice(0, 200));
  $effect(() => {
    if (ids.length) return realtime.watch(ids);
  });

  // The map snapshots of the shown mowers, one request each, at most four at a time.
  $effect(() => {
    const wanted = ids.filter((id) => !maps.has(id)).slice(0, 100);
    if (!wanted.length) return;
    for (const id of wanted) maps.set(id, null);
    let index = 0;
    const next = async (): Promise<void> => {
      const id = wanted[index++];
      if (!id) return;
      try {
        const found = await unwrap(
          telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/map', {
            params: { path: { mower_id: id } },
          }),
        );
        maps.set(id, found.map as MapSnapshot);
      } catch {
        // No map yet or not visible any more: the marker alone is enough.
      }
      await next();
    };
    void Promise.all([next(), next(), next(), next()]);
  });

  const fleet = $derived<FleetMower[]>(
    shown.map((mower) => {
      const snap = states.get(mower.id);
      return {
        id: mower.id,
        name: mower.name,
        state: snap?.state ?? null,
        stale: states.isOffline(mower.id, clock.now),
        map: maps.get(mower.id) ?? null,
      };
    }),
  );
  // Each area named against its own mower's map and custom names ("Bereich N" in map order).
  const fleetAreaName = $derived(
    fleetAreaNamer(
      shown.map((mower) => ({ map: maps.get(mower.id) ?? null, area_names: mower.area_names })),
    ),
  );
  const legend = $derived(fleetLegend(fleet));
  const withoutPosition = $derived(unplaced(fleet));
  const siteName = $derived(new Map(sites.map((s) => [s.id, s.name])));
  const chosen = $derived(shown.find((mower) => mower.id === selected) ?? null);
  const chosenState = $derived(chosen ? (states.get(chosen.id)?.state ?? null) : null);
  const fitKey = $derived(`${filter.siteId}|${filter.groupId}`);

  const canBulk = $derived(session.has('fleet.bulk_command'));
  const targets = $derived(
    bulkTargets(
      filter,
      sites.map((s) => s.id),
    ),
  );
  const scope = $derived(
    filter.groupId
      ? m.bulk_scope_group({ name: groups.find((g) => g.id === filter.groupId)?.name ?? '' })
      : filter.siteId
        ? m.bulk_scope_site({ name: siteName.get(filter.siteId) ?? '' })
        : m.bulk_scope_all(),
  );

  const labels = $derived({
    region: m.fleet_map_region(),
    zoomIn: m.map_zoom_in(),
    zoomOut: m.map_zoom_out(),
    fit: m.map_fit(),
    areas: m.fleet_map_areas(),
    north: m.map_north(),
    marker: (name: string, state: string) => m.fleet_marker({ name, state }),
  });
  const toneLabel = (tone: FleetTone) => activityLabel(tone as Activity);

  function setSite(value: string): void {
    filter = { siteId: value, groupId: '' };
    selected = null;
  }

  function setGroup(value: string): void {
    filter = { ...filter, groupId: value };
    selected = null;
  }
</script>

<svelte:head><title>{m.fleet_title()} · {m.app_name()}</title></svelte:head>

<div class="screen" data-testid="fleet-page">
  <TopBar
    title={m.fleet_title()}
    meta={places ? m.mowers_count({ count: shown.length }) : undefined}
  >
    {#snippet status()}
      <LiveStatus phase={realtime.phase} tooMany={realtime.tooMany} />
    {/snippet}
  </TopBar>
  <FleetTabs />

  {#if problem}
    <div class="pad">
      <ProblemNotice
        title={problemMessage(problem)}
        reference={problem.correlationId}
        referenceLabel={m.problem_reference()}
      />
    </div>
  {:else if !places}
    <p class="pad quiet" role="status"><SlowLoading /></p>
  {:else}
    <div class="body">
      <aside class="side">
        {#if chosen}
          <div class="card" data-testid="fleet-card" data-mower={chosen.id}>
            <div class="card-head">
              <span class="card-name">{chosen.name}</span>
              <button
                type="button"
                class="card-close"
                aria-label={m.close()}
                onclick={() => (selected = null)}
              >
                <Icon name="close" size={18} />
              </button>
            </div>
            <div class="card-row">
              <StatusBadge
                activity={(chosenState?.activity ?? 'unknown') as Activity}
                label={chosenState
                  ? activityLabel(chosenState.activity as Activity)
                  : m.activity_no_telemetry()}
                stale={states.isOffline(chosen.id, clock.now)}
              />
              <span class="quiet mono">{siteName.get(chosen.site_id) ?? ''}</span>
            </div>
            <div class="card-readouts">
              <Readout
                label={m.col_battery()}
                value={chosenState ? formatNumber(chosenState.battery_percent) : null}
                unit="%"
                size="inline"
                emptyText={m.no_data()}
                testid="fleet-card-battery"
              />
              <Readout
                label={m.col_updated()}
                value={chosenState ? formatAge(chosenState.observed_at, clock.now) : null}
                size="inline"
                emptyText={m.no_data()}
              />
            </div>
            <Button href="/mowers/{chosen.id}" icon="chevron" full testid="fleet-card-open"
              >{m.fleet_open_mower()}</Button
            >
          </div>
        {/if}
        <div class="filters">
          <Select
            id="fleet-site"
            label={m.fleet_filter_site()}
            options={[
              { value: '', label: m.fleet_filter_all_sites() },
              ...sites.map((s) => ({ value: s.id, label: s.name })),
            ]}
            bind:value={() => filter.siteId, setSite}
          />
          <Select
            id="fleet-group"
            label={m.fleet_filter_group()}
            options={[
              { value: '', label: m.fleet_filter_all_groups() },
              ...groups.map((g) => ({ value: g.id, label: g.name })),
            ]}
            bind:value={() => filter.groupId, setGroup}
          />
        </div>

        <section class="legend" aria-label={m.fleet_legend()} data-testid="fleet-legend">
          <h2 class="label">{m.fleet_legend()}</h2>
          {#if legend.length}
            <ul>
              {#each legend as entry (entry.tone)}
                <li data-tone={entry.tone} data-count={entry.count}>
                  <StatusBadge activity={entry.tone} label={activityLabel(entry.tone)} />
                  <span class="count">{formatNumber(entry.count)}</span>
                </li>
              {/each}
            </ul>
          {:else}
            <p class="quiet">{m.fleet_empty()}</p>
          {/if}
        </section>

        {#if canBulk && shown.length}
          <section class="bulk" aria-label={m.bulk_title()}>
            <h2 class="label">{m.bulk_title()}</h2>
            <p class="scope">{scope}</p>
            <div class="bulk-row">
              <Button icon="pause" testid="fleet-pause-all" onclick={() => (bulk = 'pause_mow')}
                >{m.bulk_pause_all()}</Button
              >
              <Button icon="dock" testid="fleet-dock-all" onclick={() => (bulk = 'return_to_dock')}
                >{m.bulk_dock_all()}</Button
              >
            </div>
          </section>
        {/if}

        {#if withoutPosition.length}
          <section class="unplaced" data-testid="fleet-unplaced">
            <h2 class="label">{m.fleet_no_position({ count: withoutPosition.length })}</h2>
            <ul>
              {#each withoutPosition as mower (mower.id)}
                <li><a href="/mowers/{mower.id}">{mower.name}</a></li>
              {/each}
            </ul>
          </section>
        {/if}
      </aside>

      <div class="map" data-testid="fleet-map-wrap">
        <FleetMap
          mowers={fleet}
          {tiles}
          {labels}
          {toneLabel}
          {fitKey}
          bind:selected
          areaName={fleetAreaName}
        />
      </div>
    </div>
  {/if}
</div>

<BulkSheet kind={bulk} {targets} mowers={shown} {scope} onclose={() => (bulk = null)} />

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .quiet {
    color: var(--md-color-ink-muted);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
  }

  .body {
    display: flex;
    flex-direction: column;
  }

  .side {
    display: flex;
    flex-direction: column;
    order: 2;
  }

  .side > * {
    padding: var(--md-space-3) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  .filters {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--md-space-3);
  }

  .label {
    margin: 0 0 var(--md-space-2);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .legend ul,
  .unplaced ul {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2) var(--md-space-4);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .legend li {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-2);
  }

  .count {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-16);
    font-variant-numeric: tabular-nums;
  }

  .scope {
    margin: 0 0 var(--md-space-2);
    font-size: var(--md-font-size-14);
  }

  .bulk-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--md-space-2);
  }

  .unplaced a {
    display: inline-flex;
    align-items: center;
    min-height: var(--md-target);
    color: var(--md-color-ink);
  }

  .map {
    position: relative;
    order: 1;
    height: 58dvh;
    min-height: 320px;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .card {
    display: grid;
    gap: var(--md-space-2);
    border-inline-start: 3px solid var(--md-color-ink);
  }

  .card-head,
  .card-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-2);
  }

  .card-name {
    font-size: var(--md-font-size-16);
    font-weight: var(--md-weight-semibold);
  }

  .card-close {
    display: grid;
    place-items: center;
    width: var(--md-target);
    height: var(--md-target);
    margin: calc(var(--md-space-2) * -1);
    padding: 0;
    border: 0;
    border-radius: var(--md-radius-0);
    background: none;
    color: var(--md-color-ink);
    cursor: pointer;
  }

  .card-close:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .card-readouts {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--md-space-2);
  }

  @media (min-width: 1024px) {
    .screen {
      display: flex;
      flex-direction: column;
      height: var(--md-viewport-height);
    }

    .body {
      flex: 1;
      flex-direction: row;
      min-height: 0;
    }

    .side {
      order: 0;
      flex: none;
      width: 340px;
      overflow-y: auto;
      border-inline-end: var(--md-hairline) solid var(--md-color-line);
    }

    .filters {
      grid-template-columns: 1fr;
    }

    .bulk-row {
      grid-template-columns: 1fr;
    }

    .map {
      flex: 1;
      height: auto;
      border-bottom: 0;
    }
  }
</style>
