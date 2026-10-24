<!--
  Sites of the account: the default site first (marked), each with time zone, address,
  coordinates, its mowers and groups. Create, edit and delete sites (Fleet answers 409 for the
  default site and for sites that still hold mowers or groups), move mowers here from other
  sites, and send "pause all" or "all to dock" to a site. Rights are asked per site
  (`can_manage_mowers`, `can_bulk_command`); Fleet decides again on every request.
-->
<script lang="ts">
  import { Button, Field, Icon, Panel, ProblemNotice, Select, Sheet, TopBar } from '@mowdeck/ui';

  import { fleetApi, telemetryApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { session } from '#lib/auth/session.svelte.js';
  import BulkSheet from '#lib/components/BulkSheet.svelte';
  import FleetTabs from '#lib/components/FleetTabs.svelte';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import type { BulkKind } from '#lib/fleet/bulk.js';
  import { loadPlaces, type Places, type Site } from '#lib/fleet/data.js';
  import { createSite } from '#lib/fleet/sites.js';
  import {
    addressLine,
    siteBody,
    siteForm,
    sortSites,
    validateSite,
    type SiteField,
    type SiteForm,
    type SiteIssue,
  } from '#lib/fleet/places.js';
  import { formatNumber, problemMessage } from '#lib/i18n.js';
  import { permissions } from '#lib/live.svelte.js';
  import { m } from '#lib/paraglide/messages.js';
  import { notifyPlanProblem } from '#lib/plan/problems.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  const deviceZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

  let places = $state<Places | null>(null);
  let problem = $state<ApiProblem | null>(null);
  let rights = $state<Record<string, { manage: boolean; bulk: boolean }>>({});

  // The edit sheet: `editing` is null for a new site.
  let sheetOpen = $state(false);
  let editing = $state<Site | null>(null);
  let form = $state<SiteForm>(siteForm(null, deviceZone));
  let tried = $state(false);
  let saving = $state(false);
  let saveProblem = $state<ApiProblem | null>(null);
  let origins = $state<{ id: string; name: string; lat: number; lon: number }[]>([]);
  let originId = $state('');

  let confirmDelete = $state<string | null>(null);
  let moveSite = $state<Site | null>(null);
  let moving = $state<string | null>(null);
  let bulk = $state<{ kind: BulkKind; site: Site } | null>(null);

  async function load(): Promise<void> {
    try {
      places = await loadPlaces();
    } catch (error) {
      problem = asProblem(error);
      return;
    }
    try {
      const objects = places.sites.map((s) => `site:${s.id}` as const);
      const found = await permissions.checkObjects(objects, [
        'can_manage_mowers',
        'can_bulk_command',
      ]);
      rights = Object.fromEntries(
        places.sites.map((s) => [
          s.id,
          {
            manage: found[`site:${s.id}`]?.can_manage_mowers ?? false,
            bulk: found[`site:${s.id}`]?.can_bulk_command ?? false,
          },
        ]),
      );
    } catch {
      rights = {};
    }
  }

  $effect(() => {
    void load();
  });

  const sites = $derived(sortSites(places?.sites ?? []));
  const mowersOf = (siteId: string) => (places?.mowers ?? []).filter((x) => x.site_id === siteId);
  const groupsOf = (siteId: string) => (places?.groups ?? []).filter((g) => g.site_id === siteId);
  const canCreate = $derived(session.has('fleet.manage'));
  const issues = $derived(tried ? validateSite(form, editing?.is_default ?? false) : {});

  function issueText(field: SiteField): string | null {
    const issue: SiteIssue | undefined = issues[field];
    if (!issue) return saveProblem?.fieldError(field) ?? null;
    switch (issue.code) {
      case 'required':
        return m.sched_err_required();
      case 'timezone':
        return m.sched_err_timezone();
      case 'country':
        return m.sites_err_country();
      case 'range':
        return m.sched_err_range({ min: String(issue.min), max: String(issue.max) });
      case 'both':
        return m.site_err_both();
    }
  }

  async function loadOrigins(site: Site | null): Promise<void> {
    origins = [];
    originId = '';
    const candidates = (places?.mowers ?? [])
      .filter((x) => !site || x.site_id === site.id)
      .slice(0, 12);
    const found = await Promise.all(
      candidates.map(async (mower) => {
        try {
          const snapshot = await unwrap(
            telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/map', {
              params: { path: { mower_id: mower.id } },
            }),
          );
          const ref = snapshot.map.reference;
          return { id: mower.id, name: mower.name, lat: ref.origin_lat, lon: ref.origin_lon };
        } catch {
          return null;
        }
      }),
    );
    origins = found.filter((o) => o !== null);
    originId = origins[0]?.id ?? '';
  }

  function openSheet(site: Site | null): void {
    editing = site;
    form = siteForm(site, deviceZone);
    tried = false;
    saveProblem = null;
    sheetOpen = true;
    void loadOrigins(site);
  }

  function useOrigin(): void {
    const origin = origins.find((o) => o.id === originId);
    if (!origin) return;
    form.latitude = origin.lat.toFixed(6);
    form.longitude = origin.lon.toFixed(6);
  }

  async function save(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    tried = true;
    saveProblem = null;
    const isDefault = editing?.is_default ?? false;
    if (Object.keys(validateSite(form, isDefault)).length) return;
    saving = true;
    try {
      const body = siteBody(form, isDefault);
      if (editing) {
        await unwrap(
          fleetApi.PATCH('/api/v1/fleet/sites/{site_id}', {
            params: { path: { site_id: editing.id } },
            body,
          }),
        );
      } else {
        await createSite({ ...body, name: form.name.trim() });
      }
      toasts.push('ok', editing ? m.saved() : m.sites_created({ name: form.name.trim() }));
      sheetOpen = false;
      permissions.clear();
      await load();
    } catch (error) {
      saveProblem = asProblem(error);
      notifyPlanProblem(saveProblem);
    } finally {
      saving = false;
    }
  }

  async function remove(site: Site): Promise<void> {
    if (confirmDelete !== site.id) {
      confirmDelete = site.id;
      return;
    }
    confirmDelete = null;
    try {
      await unwrap(
        fleetApi.DELETE('/api/v1/fleet/sites/{site_id}', {
          params: { path: { site_id: site.id } },
        }),
      );
      toasts.push('ok', m.sites_deleted({ name: site.name }));
      await load();
    } catch (error) {
      const found = asProblem(error);
      toasts.push(
        'error',
        found.status === 409 ? m.sites_delete_conflict() : problemMessage(found),
        undefined,
        found.correlationId ?? undefined,
      );
    }
  }

  async function move(mowerId: string, site: Site): Promise<void> {
    moving = mowerId;
    try {
      await unwrap(
        fleetApi.PATCH('/api/v1/fleet/mowers/{mower_id}', {
          params: { path: { mower_id: mowerId } },
          body: { site_id: site.id },
        }),
      );
      const name = places?.mowers.find((x) => x.id === mowerId)?.name ?? '';
      toasts.push('ok', m.sites_moved({ mower: name, site: site.name }));
      await load();
    } catch (error) {
      const found = asProblem(error);
      toasts.push('error', problemMessage(found), found.detail ?? undefined);
    } finally {
      moving = null;
    }
  }
</script>

<svelte:head><title>{m.sites_title()} · {m.app_name()}</title></svelte:head>

<TopBar title={m.sites_title()} meta={places ? String(sites.length) : undefined}>
  {#snippet actions()}
    {#if canCreate}
      <Button icon="plus" label={m.sites_add()} testid="site-add" onclick={() => openSheet(null)} />
    {/if}
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
  <ul class="sites" data-testid="sites">
    {#each sites as site (site.id)}
      {@const mowers = mowersOf(site.id)}
      {@const groups = groupsOf(site.id)}
      {@const right = rights[site.id] ?? { manage: false, bulk: false }}
      {@const address = addressLine(site.address)}
      <li class="site" data-testid="site-{site.id}" data-name={site.name}>
        <div class="head">
          <span class="glyph"><Icon name="site" size={20} /></span>
          <div class="titles">
            <h2>
              {site.name}
              {#if site.is_default}<span class="tag" data-testid="site-default"
                  >{m.sites_default()}</span
                >{/if}
            </h2>
            {#if address}<span class="meta">{address}</span>{/if}
            <span class="meta mono">{site.timezone}</span>
            <span class="meta mono" data-testid="site-coordinates-{site.id}">
              {site.latitude != null && site.longitude != null
                ? `${formatNumber(site.latitude, 5)}, ${formatNumber(site.longitude, 5)}`
                : m.site_no_coordinates()}
            </span>
          </div>
          {#if right.manage}
            <Button
              variant="ghost"
              icon="settings"
              label={m.sites_edit({ name: site.name })}
              testid="site-edit-{site.id}"
              onclick={() => openSheet(site)}
            />
          {/if}
        </div>

        <div class="counts">
          <span class="readout"
            ><span class="value mono">{formatNumber(mowers.length)}</span>
            <span class="unit">{m.sites_mowers()}</span></span
          >
          <span class="readout"
            ><span class="value mono">{formatNumber(groups.length)}</span>
            <span class="unit">{m.sites_groups()}</span></span
          >
        </div>

        {#if mowers.length}
          <ul class="chips" aria-label={m.sites_mowers()}>
            {#each mowers as mower (mower.id)}
              <li>
                <a href="/mowers/{mower.id}" class="chip"
                  ><Icon name="mower" size={16} />{mower.name}</a
                >
              </li>
            {/each}
          </ul>
        {/if}

        <div class="actions">
          {#if right.bulk && mowers.length}
            <Button
              icon="pause"
              testid="site-pause-{site.id}"
              onclick={() => (bulk = { kind: 'pause_mow', site })}>{m.bulk_pause_all()}</Button
            >
            <Button
              icon="dock"
              testid="site-dock-{site.id}"
              onclick={() => (bulk = { kind: 'return_to_dock', site })}>{m.bulk_dock_all()}</Button
            >
          {/if}
          {#if right.manage}
            <Button
              variant="ghost"
              icon="mower"
              testid="site-move-{site.id}"
              onclick={() => (moveSite = site)}>{m.sites_move_here()}</Button
            >
          {/if}
          {#if right.manage && !site.is_default}
            <Button
              variant="danger"
              icon="trash"
              testid="site-delete-{site.id}"
              onclick={() => remove(site)}
              >{confirmDelete === site.id ? m.sites_delete_confirm() : m.sites_delete()}</Button
            >
          {/if}
        </div>
      </li>
    {/each}
  </ul>
  {#if !canCreate}
    <p class="pad quiet">{m.sites_viewer_hint()}</p>
  {/if}
{/if}

<Sheet
  open={sheetOpen}
  title={editing ? m.sites_edit({ name: editing.name }) : m.sites_add()}
  closeLabel={m.close()}
  onclose={() => (sheetOpen = false)}
  testid="site-sheet"
>
  <form id="site-form" class="form" onsubmit={save} novalidate>
    <Field
      id="site-form-name"
      label={m.site_name()}
      required
      maxlength={100}
      disabled={editing?.is_default ?? false}
      hint={editing?.is_default ? m.site_default_hint() : undefined}
      bind:value={form.name}
      error={issueText('name')}
      testid="site-form-name"
    />
    <div class="tz">
      <Field
        id="site-form-timezone"
        label={m.site_timezone()}
        mono
        bind:value={form.timezone}
        error={issueText('timezone')}
      />
      {#if form.timezone !== deviceZone}
        <button type="button" class="preset" onclick={() => (form.timezone = deviceZone)}
          >{m.sched_timezone_device({ zone: deviceZone })}</button
        >
      {/if}
    </div>
    <Panel label={m.sites_address()} flush>
      <div class="grid2">
        <Field
          id="site-form-street"
          label={m.sites_street()}
          hint={m.optional()}
          bind:value={form.street}
        />
        <Field
          id="site-form-postal"
          label={m.sites_postal_code()}
          hint={m.optional()}
          mono
          bind:value={form.postalCode}
        />
        <Field
          id="site-form-city"
          label={m.sites_city()}
          hint={m.optional()}
          bind:value={form.city}
        />
        <Field
          id="site-form-country"
          label={m.sites_country()}
          hint={m.sites_country_hint()}
          mono
          maxlength={2}
          bind:value={form.country}
          error={issueText('country')}
        />
      </div>
    </Panel>
    <Panel label={m.site_coordinates()} flush>
      <p class="hint">{m.site_hint()}</p>
      <div class="grid2">
        <Field
          id="site-form-latitude"
          label={m.site_latitude()}
          inputmode="decimal"
          mono
          hint="°"
          bind:value={form.latitude}
          error={issueText('latitude')}
        />
        <Field
          id="site-form-longitude"
          label={m.site_longitude()}
          inputmode="decimal"
          mono
          hint="°"
          bind:value={form.longitude}
          error={issueText('longitude')}
        />
      </div>
      {#if origins.length}
        <div class="origin">
          {#if origins.length > 1}
            <Select
              id="site-form-origin"
              label={m.sites_origin_of()}
              options={origins.map((o) => ({ value: o.id, label: o.name }))}
              bind:value={originId}
            />
          {/if}
          <Button icon="pin" testid="site-form-origin" onclick={useOrigin}
            >{origins.length > 1
              ? m.site_use_origin()
              : m.sites_use_origin_of({ name: origins[0]?.name ?? '' })}</Button
          >
        </div>
      {/if}
    </Panel>
    {#if saveProblem}
      <ProblemNotice
        title={problemMessage(saveProblem)}
        detail={saveProblem.detail}
        reference={saveProblem.correlationId}
        referenceLabel={m.problem_reference()}
      />
    {/if}
  </form>
  {#snippet footer()}
    <Button
      variant="primary"
      full
      icon="check"
      pending={saving}
      testid="site-form-save"
      onclick={() => document.querySelector<HTMLFormElement>('#site-form')?.requestSubmit()}
      >{m.save()}</Button
    >
  {/snippet}
</Sheet>

<Sheet
  open={moveSite !== null}
  title={moveSite ? m.sites_move_title({ site: moveSite.name }) : ''}
  closeLabel={m.close()}
  onclose={() => (moveSite = null)}
  testid="site-move-sheet"
>
  {#if moveSite}
    {@const target = moveSite}
    {@const others = (places?.mowers ?? []).filter((x) => x.site_id !== target.id)}
    {#if others.length}
      <p class="hint">{m.sites_move_hint()}</p>
      <ul class="moves">
        {#each others as mower (mower.id)}
          <li>
            <span class="who">
              <span class="name">{mower.name}</span>
              <span class="meta"
                >{places?.sites.find((s) => s.id === mower.site_id)?.name ?? ''}</span
              >
            </span>
            <Button
              icon="chevron"
              pending={moving === mower.id}
              testid="move-{mower.id}"
              onclick={() => move(mower.id, target)}>{m.sites_move()}</Button
            >
          </li>
        {/each}
      </ul>
    {:else}
      <p class="hint">{m.sites_move_none()}</p>
    {/if}
  {/if}
</Sheet>

<BulkSheet
  kind={bulk?.kind ?? null}
  targets={bulk ? [{ type: 'site', id: bulk.site.id }] : []}
  mowers={bulk ? mowersOf(bulk.site.id) : []}
  scope={bulk ? m.bulk_scope_site({ name: bulk.site.name }) : ''}
  onclose={() => (bulk = null)}
/>

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .quiet {
    color: var(--md-color-ink-muted);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .sites {
    display: grid;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .site {
    display: grid;
    gap: var(--md-space-3);
    padding: var(--md-space-4) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  .head {
    display: flex;
    align-items: flex-start;
    gap: var(--md-space-3);
  }

  .glyph {
    display: grid;
    place-items: center;
    width: 40px;
    height: 40px;
    border: var(--md-hairline) solid var(--md-color-line-strong);
    color: var(--md-color-ink);
  }

  .titles {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: var(--md-space-0_5);
    min-width: 0;
  }

  h2 {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2);
    margin: 0;
    font-size: var(--md-font-size-20);
    font-weight: var(--md-weight-semibold);
  }

  .tag {
    padding: 0 var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-ink);
    border-radius: var(--md-radius-1);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .meta {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .counts {
    display: flex;
    gap: var(--md-space-6);
  }

  .readout {
    display: inline-flex;
    align-items: baseline;
    gap: var(--md-space-1);
  }

  .value {
    font-size: var(--md-font-size-24);
    font-weight: var(--md-weight-medium);
  }

  .unit {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    min-height: 40px;
    padding: 0 var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
    text-decoration: none;
  }

  .chip:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  .form {
    display: grid;
    gap: var(--md-space-4);
  }

  .grid2 {
    display: grid;
    gap: var(--md-space-3);
  }

  .tz {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--md-space-2);
  }

  .tz > :global(.md-field) {
    align-self: stretch;
  }

  .preset {
    min-height: 40px;
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

  .preset:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .hint {
    margin: 0 0 var(--md-space-3);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .origin {
    display: grid;
    gap: var(--md-space-2);
    margin-top: var(--md-space-3);
  }

  .moves {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .moves li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-3);
    min-height: var(--md-target-command);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .who {
    display: flex;
    flex-direction: column;
  }

  .name {
    font-weight: var(--md-weight-semibold);
  }

  @media (min-width: 768px) {
    .grid2 {
      grid-template-columns: 1fr 1fr;
    }
  }

  @media (min-width: 1024px) {
    .sites {
      grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
      gap: var(--md-space-4);
      padding: var(--md-space-4) var(--md-gutter);
    }

    .site {
      align-content: start;
      border: var(--md-hairline) solid var(--md-color-line);
    }
  }
</style>
