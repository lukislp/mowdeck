<!--
  Virtual zones of one mower (Telemetry, ADR 0070): for a mower whose vendor shares no map but
  that reports positions, the owner draws zones over the map (and the aerial photo, when the
  installation offers one), names them and sees which zone the mower is in and how long it spent
  in each over the last seven days. A zone can be linked to one of the mower's own zone starts
  when its connection declares them; then it gets a start button. Drawing: tap corners on the
  map, undo, done, name. Drawing, editing and deleting need `can_control` (checked first, hidden
  after a 403); everyone who sees the mower sees the zones. Whether zones are offered comes from
  the server (`availability`), never from the vendor.
-->
<script lang="ts">
  import {
    Button,
    Field,
    Icon,
    MowerMap,
    ProblemNotice,
    Select,
    Sheet,
    TopBar,
    draftProblem,
    draftToRing,
    type LocalPoint,
    type MapSnapshot,
  } from '@mowdeck/ui';

  import { page } from '$app/state';

  import { apiFetch, fleetApi, telemetryApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { clock } from '#lib/clock.svelte.js';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import type { components as Fleet } from '#lib/generated/fleet.js';
  import { formatNumber, problemMessage } from '#lib/i18n.js';
  import { permissions, revoked, sendCommand } from '#lib/live.svelte.js';
  import {
    clampOpacity,
    fetchAerialInfo,
    loadAerialPrefs,
    mapAerial,
    saveAerialPrefs,
    tileLoader,
    type AerialInfo,
    type AerialPrefs,
  } from '#lib/map/aerial.js';
  import { mapLabels } from '#lib/mower/map-labels.js';
  import { m } from '#lib/paraglide/messages.js';
  import { getLocale } from '#lib/paraglide/runtime.js';
  import { mowers as states } from '#lib/stores/mowers.svelte.js';
  import { toasts } from '#lib/stores/ui.svelte.js';
  import {
    createZone,
    currentZoneName,
    deleteZone,
    draftProblemText,
    formatZoneTime,
    getZoneStats,
    getZones,
    timeShares,
    undoCorner,
    updateZone,
    zoneMap,
    type VirtualZone,
    type ZoneStats,
    type ZonesOverview,
  } from '#lib/zones/zones.js';

  type Mower = Fleet['schemas']['MowerOut'];
  /** What the sheet shows: a new zone after drawing, an existing one, or a delete question. */
  type SheetState =
    | { kind: 'new' }
    | { kind: 'edit'; zone: VirtualZone }
    | { kind: 'delete'; zone: VirtualZone }
    | null;

  const POLL_MS = 10_000;

  const id = $derived(page.params.id ?? '');
  const locale = $derived(getLocale());

  let mower = $state.raw<Mower | null>(null);
  let overview = $state.raw<ZonesOverview | null>(null);
  let stats = $state.raw<ZoneStats | null>(null);
  let snapshot = $state.raw<MapSnapshot | null>(null);
  let loaded = $state(false);
  let notFound = $state(false);
  let problem = $state<ApiProblem | null>(null);
  let canControl = $state(false);
  let mapFailed = $state(false);
  let aerialInfo = $state.raw<AerialInfo | null>(null);
  let aerialPrefs = $state<AerialPrefs>(loadAerialPrefs());

  /** Drawing: the corners so far (local metres), and the zone being redrawn, if any. */
  let draft = $state.raw<LocalPoint[] | null>(null);
  let redrawing = $state<string | null>(null);
  let selected = $state<string | null>(null);
  let sheet = $state.raw<SheetState>(null);
  let name = $state('');
  let link = $state('');
  let busy = $state(false);
  let actionProblem = $state<ApiProblem | null>(null);

  const ms = $derived(states.get(id)?.state ?? null);
  const map = $derived(zoneMap(snapshot, overview?.anchor));
  const zones = $derived(overview?.zones ?? []);
  const current = $derived(overview?.presence?.zone_id ?? null);
  const available = $derived(overview?.availability === 'available');
  const canDraw = $derived(canControl && available);
  const limits = $derived(overview?.limits);
  const reached = $derived(!!limits && zones.length >= limits.max_zones);
  const problemOfDraft = $derived(
    draft
      ? draftProblem(draft, {
          min_corners: limits?.min_corners ?? 3,
          max_corners: limits?.max_corners ?? 64,
          min_area_m2: limits?.min_area_m2 ?? 1,
          max_area_m2: limits?.max_area_m2 ?? 100_000,
        })
      : null,
  );
  const shares = $derived(timeShares(stats?.items ?? []));
  const totals = $derived(new Map((stats?.items ?? []).map((item) => [item.zone_id, item])));
  const plans = $derived(overview?.zone_plans ?? []);
  const planOptions = $derived([
    { value: '', label: m.zones_link_none() },
    ...plans.map((plan) => ({ value: plan.ref, label: plan.name ?? plan.ref })),
  ]);
  const labels = $derived(mapLabels());
  const aerialLoad = tileLoader(apiFetch);
  const aerial = $derived(mapAerial(aerialInfo, aerialPrefs, aerialLoad));

  $effect(() => clock.hold(30_000));

  async function refresh(mowerId: string): Promise<void> {
    const [next, nextStats] = await Promise.all([
      getZones(mowerId),
      getZoneStats(mowerId, 7).catch(() => null),
    ]);
    if (mowerId !== id) return;
    overview = next;
    if (nextStats) stats = nextStats;
  }

  async function load(mowerId: string): Promise<void> {
    loaded = false;
    notFound = false;
    problem = null;
    overview = null;
    stats = null;
    snapshot = null;
    draft = null;
    canControl = false;
    try {
      mower = await unwrap(
        fleetApi.GET('/api/v1/fleet/mowers/{mower_id}', {
          params: { path: { mower_id: mowerId } },
        }),
      );
      await refresh(mowerId);
    } catch (error) {
      const p = asProblem(error);
      if (p.status === 404) notFound = true;
      else problem = p;
      return;
    } finally {
      loaded = true;
    }
    const path = { params: { path: { mower_id: mowerId } } };
    void unwrap(telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/map', path))
      .then((data) => (snapshot = data.map as MapSnapshot))
      .catch(() => (snapshot = null));
    void unwrap(telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/state', path))
      .then((snap) => states.upsert(mowerId, snap.state, 'rest'))
      .catch(() => undefined);
    void fetchAerialInfo(apiFetch, mowerId)
      .then((info) => (aerialInfo = info))
      .catch(() => (aerialInfo = null));
    void permissions
      .check(mowerId, ['can_control'])
      .then((result) => (canControl = result.can_control))
      .catch(() => (canControl = false));
  }

  $effect(() => {
    void load(id);
  });

  // The current zone follows the mower: poll the overview (and the state for the marker).
  $effect(() => {
    const mowerId = id;
    if (!loaded || problem || notFound) return;
    const timer = setInterval(() => {
      void refresh(mowerId).catch(() => undefined);
      void unwrap(
        telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/state', {
          params: { path: { mower_id: mowerId } },
        }),
      )
        .then((snap) => states.upsert(mowerId, snap.state, 'rest'))
        .catch(() => undefined);
    }, POLL_MS);
    return () => clearInterval(timer);
  });

  function setAerial(next: Partial<AerialPrefs>): void {
    aerialPrefs = { ...aerialPrefs, ...next };
    saveAerialPrefs(aerialPrefs);
  }

  // --- drawing -------------------------------------------------------------------------------

  function startDrawing(zoneId: string | null = null): void {
    sheet = null;
    actionProblem = null;
    redrawing = zoneId;
    selected = null;
    draft = [];
  }

  function cancelDrawing(): void {
    draft = null;
    redrawing = null;
    actionProblem = null;
  }

  function addCorner(local: [number, number]): void {
    if (draft) draft = [...draft, local];
  }

  async function finishDrawing(): Promise<void> {
    if (!draft || problemOfDraft) return;
    if (redrawing) {
      const zone = zones.find((z) => z.id === redrawing);
      if (zone) await saveShape(zone);
      return;
    }
    name = '';
    link = '';
    actionProblem = null;
    sheet = { kind: 'new' };
  }

  async function run<T>(action: () => Promise<T>): Promise<T | null> {
    busy = true;
    actionProblem = null;
    try {
      return await action();
    } catch (error) {
      const p = asProblem(error);
      if (p.status === 403) canControl = false;
      actionProblem = p;
      return null;
    } finally {
      busy = false;
    }
  }

  async function saveNew(): Promise<void> {
    const ref = map?.reference;
    if (!draft || !ref) return;
    const ring = draftToRing(ref, draft);
    const created = await run(() =>
      createZone(id, { name: name.trim(), ring, start_plan_ref: link || null }),
    );
    if (!created) return;
    toasts.push('ok', m.zones_saved());
    sheet = null;
    draft = null;
    selected = created.id;
    await refresh(id).catch(() => undefined);
  }

  async function saveShape(zone: VirtualZone): Promise<void> {
    const ref = map?.reference;
    if (!draft || !ref) return;
    const ring = draftToRing(ref, draft);
    const changed = await run(() => updateZone(id, zone.id, { ring }));
    if (!changed) return;
    toasts.push('ok', m.zones_saved());
    draft = null;
    redrawing = null;
    selected = zone.id;
    await refresh(id).catch(() => undefined);
  }

  function openEdit(zone: VirtualZone): void {
    name = zone.name;
    link = zone.start_plan_ref ?? '';
    actionProblem = null;
    selected = zone.id;
    sheet = { kind: 'edit', zone };
  }

  async function saveEdit(zone: VirtualZone): Promise<void> {
    const body = link
      ? { name: name.trim(), start_plan_ref: link }
      : { name: name.trim(), unlink_start_plan: true };
    const changed = await run(() => updateZone(id, zone.id, body));
    if (!changed) return;
    toasts.push('ok', m.zones_saved());
    sheet = null;
    await refresh(id).catch(() => undefined);
  }

  async function remove(zone: VirtualZone): Promise<void> {
    const done = await run(() => deleteZone(id, zone.id).then(() => true));
    if (!done) return;
    toasts.push('ok', m.zones_deleted());
    sheet = null;
    selected = null;
    await refresh(id).catch(() => undefined);
  }

  async function startIn(zone: VirtualZone): Promise<void> {
    if (!zone.start_plan_ref) return;
    await sendCommand(id, { kind: 'start_mow', plan_ref: zone.start_plan_ref });
  }

  const shortTime = (value: string) =>
    new Intl.DateTimeFormat(locale, { timeStyle: 'short' }).format(new Date(value));
  const planName = (ref: string | null | undefined) =>
    plans.find((plan) => plan.ref === ref)?.name ?? ref ?? '';
  const sheetTitle = $derived(
    sheet?.kind === 'new'
      ? m.zones_sheet_new()
      : sheet?.kind === 'edit'
        ? m.zones_sheet_edit()
        : sheet?.kind === 'delete'
          ? m.zones_delete()
          : '',
  );
</script>

<svelte:head><title>{m.zones_title()} · {mower?.name ?? m.app_name()}</title></svelte:head>

{#if notFound || revoked.has(id)}
  <TopBar title={m.detail_not_found_title()} backHref="/mowers" backLabel={m.back()} />
  <div class="pad"><ProblemNotice tone="info" title={m.detail_not_found_title()} /></div>
{:else if problem}
  <TopBar title={m.zones_title()} backHref="/mowers/{id}" backLabel={m.back()} />
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if !loaded || !overview}
  <TopBar title={m.zones_title()} backHref="/mowers/{id}" backLabel={m.back()} />
  <p class="pad" role="status"><SlowLoading /></p>
{:else}
  <TopBar
    title={m.zones_title()}
    meta={mower?.name ?? ''}
    backHref="/mowers/{id}"
    backLabel={m.back()}
  />

  <div class="view" data-testid="zones-page" data-availability={overview.availability}>
    <div class="map">
      {#if map && !mapFailed}
        <MowerMap
          {map}
          state={ms}
          {aerial}
          {labels}
          areaName={(area) => area.name ?? area.id}
          virtualZones={zones}
          currentZone={current}
          selectedZone={selected}
          hiddenZone={redrawing}
          zoneDraft={draft}
          onmaptap={draft ? addCorner : undefined}
          onzonetap={(zoneId) => (selected = selected === zoneId ? null : zoneId)}
          onfail={() => (mapFailed = true)}
          testid="zones-map"
        />
        {#if draft}
          <div
            class="drawbar"
            data-testid="zones-drawbar"
            role="toolbar"
            aria-label={m.zones_draw()}
          >
            <span class="drawbar-count mono" data-testid="zones-corner-count"
              >{m.zones_corners({ n: draft.length })}</span
            >
            <span class="drawbar-hint" role="status" aria-live="polite">
              {#if draft.length === 0}
                {m.zones_drawing_hint()}
              {:else if problemOfDraft && problemOfDraft !== 'corners'}
                {draftProblemText(problemOfDraft)}
              {:else}
                {m.zones_drawing_hint()}
              {/if}
            </span>
            <span class="drawbar-actions">
              <Button
                variant="ghost"
                icon="back"
                disabled={!draft.length}
                testid="zones-undo"
                onclick={() => (draft = draft ? undoCorner(draft) : null)}
                label={m.zones_undo()}
              />
              <Button variant="ghost" icon="close" testid="zones-cancel" onclick={cancelDrawing}
                >{m.cancel()}</Button
              >
              <Button
                variant="primary"
                icon="check"
                disabled={!!problemOfDraft}
                pending={busy && !!redrawing}
                testid="zones-finish"
                onclick={finishDrawing}>{m.zones_finish()}</Button
              >
            </span>
          </div>
        {/if}
        {#if aerialInfo?.enabled && !draft}
          <div class="aerial">
            <label class="aerial-toggle">
              <input
                type="checkbox"
                checked={aerialPrefs.shown}
                data-testid="zones-aerial"
                onchange={(event) => setAerial({ shown: event.currentTarget.checked })}
              />
              <span>{m.zones_aerial()}</span>
            </label>
            {#if aerialPrefs.shown}
              <input
                type="range"
                min="0.2"
                max="1"
                step="0.05"
                value={aerialPrefs.opacity}
                aria-label={m.zones_aerial()}
                oninput={(event) =>
                  setAerial({ opacity: clampOpacity(Number(event.currentTarget.value)) })}
              />
            {/if}
          </div>
        {/if}
      {:else if mapFailed}
        <div class="pad"><ProblemNotice tone="info" title={m.zones_map_failed()} /></div>
      {:else}
        <div class="pad map-wait" data-testid="zones-no-map">{m.zones_no_map()}</div>
      {/if}
    </div>

    <div class="side">
      <section class="now" data-testid="zones-now" data-zone={current ?? ''}>
        {#if overview.presence?.zone_id}
          <span class="now-badge is-in"><Icon name="pin" size={16} /></span>
          <span class="now-text">
            <strong>{m.zones_now_in({ name: currentZoneName(overview) ?? '' })}</strong>
            <span class="now-since"
              >{m.zones_since({ time: shortTime(overview.presence.since) })}</span
            >
          </span>
        {:else if overview.presence}
          <span class="now-badge"><Icon name="pin" size={16} /></span>
          <span class="now-text">
            <strong>{m.zones_now_outside()}</strong>
            <span class="now-since"
              >{m.zones_since({ time: shortTime(overview.presence.since) })}</span
            >
          </span>
        {:else}
          <span class="now-badge"><Icon name="pin" size={16} /></span>
          <span class="now-text"><strong>{m.zones_now_unknown()}</strong></span>
        {/if}
      </section>

      {#if overview.availability === 'vendor_map'}
        <ProblemNotice tone="info" title={m.zones_unavailable_vendor_map()} />
      {:else if overview.availability === 'no_position'}
        <ProblemNotice tone="info" title={m.zones_unavailable_no_position()} />
      {:else}
        <p class="intro">{m.zones_intro()}</p>
      {/if}

      {#if canDraw && !draft}
        {#if reached}
          <p class="hint">{m.zones_limit({ max: limits?.max_zones ?? 32 })}</p>
        {:else}
          <Button
            variant="primary"
            icon="plus"
            full
            disabled={!map || mapFailed}
            testid="zones-draw"
            onclick={() => startDrawing()}>{m.zones_draw()}</Button
          >
        {/if}
      {/if}
      {#if actionProblem && !sheet}
        <ProblemNotice title={problemMessage(actionProblem)} />
      {/if}

      <section class="list" aria-label={m.zones_title()}>
        <header class="list-head">
          <h2>{m.zones_title()}</h2>
          <span class="list-period">{m.zones_week()}</span>
        </header>
        {#if zones.length === 0}
          <p class="hint" data-testid="zones-empty">{m.zones_empty()}</p>
        {:else}
          <ol class="zones">
            {#each zones as zone (zone.id)}
              {@const total = totals.get(zone.id)}
              <li
                class="zone"
                class:is-current={zone.id === current}
                class:is-selected={zone.id === selected}
                data-testid="zone-row"
                data-zone-id={zone.id}
              >
                <button
                  type="button"
                  class="zone-pick"
                  aria-pressed={zone.id === selected}
                  onclick={() => (selected = selected === zone.id ? null : zone.id)}
                >
                  <span class="zone-name" data-testid="zone-name">{zone.name}</span>
                  <span class="zone-area mono"
                    >{m.zones_area({
                      area: formatNumber(zone.area_m2, zone.area_m2 < 100 ? 1 : 0),
                    })}</span
                  >
                </button>
                <span class="zone-bar" aria-hidden="true"
                  ><span style:inline-size="{Math.round((shares.get(zone.id) ?? 0) * 100)}%"
                  ></span></span
                >
                <span class="zone-facts" data-testid="zone-time">
                  {m.zones_time({ time: formatZoneTime(total?.seconds ?? 0) })}
                  {#if total && total.mowing_seconds > 0}
                    · {m.zones_mowing({ time: formatZoneTime(total.mowing_seconds) })}
                  {/if}
                  {#if total && total.entries > 0}
                    · {m.zones_entries({ count: formatNumber(total.entries) })}
                  {/if}
                </span>
                {#if zone.start_plan_ref}
                  <span class="zone-link"
                    >{m.zones_linked({ name: planName(zone.start_plan_ref) })}</span
                  >
                {/if}
                {#if canControl}
                  <span class="zone-actions">
                    {#if zone.start_plan_ref}
                      <Button icon="play" testid="zone-start" onclick={() => startIn(zone)}
                        >{m.zones_start()}</Button
                      >
                    {/if}
                    <Button
                      variant="ghost"
                      icon="edit"
                      testid="zone-edit"
                      disabled={!!draft}
                      onclick={() => openEdit(zone)}>{m.zones_edit()}</Button
                    >
                  </span>
                {/if}
              </li>
            {/each}
          </ol>
        {/if}
      </section>
    </div>
  </div>

  <Sheet
    open={sheet !== null}
    title={sheetTitle}
    closeLabel={m.cancel()}
    onclose={() => (sheet = null)}
    testid="zones-sheet"
  >
    {#if sheet?.kind === 'delete'}
      <p>{m.zones_delete_confirm({ name: sheet.zone.name })}</p>
    {:else if sheet}
      <form
        id="zone-form"
        class="form"
        onsubmit={(event) => {
          event.preventDefault();
          if (sheet?.kind === 'new') void saveNew();
          else if (sheet?.kind === 'edit') void saveEdit(sheet.zone);
        }}
      >
        <Field
          id="zone-name"
          label={m.zones_name_label()}
          placeholder={m.zones_name_placeholder()}
          bind:value={name}
          required
          maxlength={limits?.max_name_length ?? 60}
          testid="zone-name-input"
        />
        {#if plans.length}
          <Select
            id="zone-link"
            label={m.zones_link_label()}
            options={planOptions}
            bind:value={link}
            hint={m.zones_link_hint()}
          />
        {/if}
      </form>
    {/if}
    {#if actionProblem}
      <ProblemNotice title={problemMessage(actionProblem)} />
    {/if}
    {#snippet footer()}
      {#if sheet?.kind === 'delete'}
        {@const zone = sheet.zone}
        <Button
          variant="danger"
          icon="trash"
          full
          pending={busy}
          testid="zone-delete-confirm"
          onclick={() => remove(zone)}>{m.zones_delete()}</Button
        >
      {:else if sheet?.kind === 'edit'}
        {@const zone = sheet.zone}
        <div class="sheet-row">
          <Button
            variant="ghost"
            icon="trash"
            testid="zone-delete"
            onclick={() => (sheet = { kind: 'delete', zone })}>{m.zones_delete()}</Button
          >
          <Button
            variant="ghost"
            icon="crosshair"
            testid="zone-redraw"
            onclick={() => startDrawing(zone.id)}>{m.zones_redraw()}</Button
          >
        </div>
        <Button
          variant="primary"
          full
          pending={busy}
          disabled={!name.trim()}
          testid="zone-save"
          onclick={() => saveEdit(zone)}>{m.zones_save()}</Button
        >
      {:else if sheet?.kind === 'new'}
        <Button
          variant="primary"
          full
          pending={busy}
          disabled={!name.trim()}
          testid="zone-save"
          onclick={saveNew}>{m.zones_save()}</Button
        >
      {/if}
    {/snippet}
  </Sheet>
{/if}

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
    color: var(--md-color-ink-muted);
  }

  .view {
    display: grid;
  }

  .map {
    position: relative;
    block-size: 56dvh;
    min-block-size: 300px;
    border-block-end: var(--md-hairline) solid var(--md-color-line-strong);
  }

  .map-wait {
    display: grid;
    place-items: center;
    block-size: 100%;
  }

  .side {
    display: grid;
    gap: var(--md-space-4);
    align-content: start;
    padding: var(--md-space-4) var(--md-gutter) var(--md-space-12);
  }

  @media (min-width: 900px) {
    .view {
      grid-template-columns: minmax(0, 1fr) 400px;
      min-block-size: calc(100dvh - var(--md-sticky-top));
    }

    .map {
      block-size: auto;
      min-block-size: 0;
      border-block-end: 0;
      border-inline-end: var(--md-hairline) solid var(--md-color-line-strong);
    }
  }

  /* The drawing bar sits on the map's top edge like a surveyor's clipboard. */
  .drawbar {
    position: absolute;
    inset-block-start: var(--md-space-3);
    inset-inline: var(--md-space-3);
    z-index: 1000;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: var(--md-space-1) var(--md-space-3);
    align-items: center;
    padding: var(--md-space-2) var(--md-space-3);
    border: var(--md-border-control) solid var(--md-color-focus);
    border-inline-start-width: 6px;
    background: var(--md-color-surface);
    color: var(--md-color-ink);
  }

  .drawbar-count {
    font-weight: var(--md-weight-semibold);
  }

  .drawbar-hint {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .drawbar-actions {
    display: flex;
    flex-wrap: wrap;
    grid-column: 1 / -1;
    gap: var(--md-space-2);
    justify-content: flex-end;
  }

  .aerial {
    position: absolute;
    inset-block-start: var(--md-space-3);
    inset-inline-start: var(--md-space-3);
    z-index: 1000;
    display: flex;
    gap: var(--md-space-2);
    align-items: center;
    padding: var(--md-space-1) var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface);
    font-size: var(--md-font-size-14);
  }

  .aerial-toggle {
    display: inline-flex;
    gap: var(--md-space-2);
    align-items: center;
    min-block-size: var(--md-target);
  }

  .now {
    display: flex;
    gap: var(--md-space-3);
    align-items: center;
    padding: var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface);
  }

  .now-badge {
    display: grid;
    place-items: center;
    inline-size: 36px;
    block-size: 36px;
    border: var(--md-border-control) dashed var(--md-state-returning);
    color: var(--md-state-returning);
  }

  .now-badge.is-in {
    border-style: solid;
    border-color: var(--md-state-mowing);
    background: var(--md-state-mowing);
    color: var(--md-color-ink-inverse);
  }

  .now-text {
    display: grid;
    gap: var(--md-space-0_5);
  }

  .now-text strong {
    font-size: var(--md-font-size-18);
  }

  .now-since,
  .intro,
  .hint,
  .list-period,
  .zone-facts,
  .zone-link {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  .intro,
  .hint {
    margin: 0;
  }

  .list {
    display: grid;
    gap: var(--md-space-2);
  }

  .list-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    padding-block-end: var(--md-space-1);
    border-block-end: var(--md-hairline) solid var(--md-color-line-strong);
  }

  .list-head h2 {
    margin: 0;
    font-size: var(--md-font-size-14);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .zones {
    display: grid;
    gap: 0;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .zone {
    display: grid;
    gap: var(--md-space-1);
    padding: var(--md-space-3) 0 var(--md-space-3) var(--md-space-3);
    border-block-end: var(--md-hairline) solid var(--md-color-line);
    border-inline-start: 3px dashed var(--md-state-returning);
  }

  .zone.is-current {
    border-inline-start-style: solid;
    border-inline-start-color: var(--md-state-mowing);
  }

  .zone.is-selected {
    background: var(--md-color-surface-sunken);
  }

  .zone-pick {
    display: flex;
    gap: var(--md-space-3);
    align-items: baseline;
    justify-content: space-between;
    min-block-size: var(--md-target);
    padding: 0;
    border: 0;
    background: none;
    color: var(--md-color-ink);
    font: inherit;
    text-align: start;
    cursor: pointer;
  }

  .zone-pick:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .zone-name {
    font-weight: var(--md-weight-semibold);
  }

  .zone-area {
    padding-inline-end: var(--md-space-3);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
  }

  .zone-bar {
    display: block;
    block-size: 6px;
    margin-inline-end: var(--md-space-3);
    background: var(--md-color-surface-sunken);
  }

  .zone-bar span {
    display: block;
    block-size: 100%;
    background: var(--md-state-returning);
    transition: inline-size var(--md-motion-base) var(--md-ease);
  }

  .zone.is-current .zone-bar span {
    background: var(--md-state-mowing);
  }

  .zone-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
    padding-inline-end: var(--md-space-3);
  }

  .form {
    display: grid;
    gap: var(--md-space-4);
  }

  .sheet-row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
    justify-content: space-between;
    margin-block-end: var(--md-space-2);
  }

  @media (prefers-reduced-motion: reduce) {
    .zone-bar span {
      transition: none;
    }
  }
</style>
