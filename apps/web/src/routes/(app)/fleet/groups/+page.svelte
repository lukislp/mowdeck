<!--
  Mower groups: per group its site (or cross-site), its members, and the actions: edit name and
  site, add or remove members (each change is `PUT /mowers/{id}/groups` with the mower's whole
  set), delete (409 while it has mowers), "pause all" and "all to dock" for the group, and a new
  schedule for the group. Rights are asked per group (`can_manage_mowers`, `can_bulk_command`);
  Fleet decides again on every request.
-->
<script lang="ts">
  import { Button, Field, Icon, ProblemNotice, Select, Sheet, TopBar } from '@mowdeck/ui';

  import { fleetApi, unwrap } from '$lib/api/client';
  import { asProblem, type ApiProblem } from '$lib/api/problem';
  import { session } from '$lib/auth/session.svelte';
  import BulkSheet from '$lib/components/BulkSheet.svelte';
  import FleetTabs from '$lib/components/FleetTabs.svelte';
  import SlowLoading from '$lib/components/SlowLoading.svelte';
  import type { BulkKind } from '$lib/fleet/bulk';
  import { loadPlaces, type Group, type Places } from '$lib/fleet/data';
  import { groupBody, groupForm, sortSites, toggleGroup, type GroupForm } from '$lib/fleet/places';
  import { formatNumber, problemMessage } from '$lib/i18n';
  import { permissions } from '$lib/live.svelte';
  import { m } from '$lib/paraglide/messages';
  import { notifyPlanProblem } from '$lib/plan/problems';
  import { toasts } from '$lib/stores/ui.svelte';

  let places = $state<Places | null>(null);
  let problem = $state<ApiProblem | null>(null);
  let rights = $state<Record<string, { manage: boolean; bulk: boolean }>>({});

  let sheetOpen = $state(false);
  let editing = $state<Group | null>(null);
  let form = $state<GroupForm>(groupForm(null));
  let tried = $state(false);
  let saving = $state(false);
  let saveProblem = $state<ApiProblem | null>(null);

  let membersOf = $state<Group | null>(null);
  let toggling = $state<string | null>(null);
  let memberProblem = $state<ApiProblem | null>(null);
  let confirmDelete = $state<string | null>(null);
  let bulk = $state<{ kind: BulkKind; group: Group } | null>(null);

  async function load(): Promise<void> {
    try {
      places = await loadPlaces();
    } catch (error) {
      problem = asProblem(error);
      return;
    }
    try {
      const objects = places.groups.map((g) => `group:${g.id}` as const);
      const found = await permissions.checkObjects(objects, [
        'can_manage_mowers',
        'can_bulk_command',
      ]);
      rights = Object.fromEntries(
        places.groups.map((g) => [
          g.id,
          {
            manage: found[`group:${g.id}`]?.can_manage_mowers ?? false,
            bulk: found[`group:${g.id}`]?.can_bulk_command ?? false,
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

  const groups = $derived([...(places?.groups ?? [])].sort((a, b) => a.name.localeCompare(b.name)));
  const sites = $derived(sortSites(places?.sites ?? []));
  const siteName = $derived(new Map(sites.map((s) => [s.id, s.name])));
  const members = (groupId: string) =>
    (places?.mowers ?? []).filter((x) => x.group_ids.includes(groupId));
  const canCreate = $derived(session.has('fleet.manage'));
  const canSchedule = $derived(session.has('schedule.edit'));
  const nameIssue = $derived(tried && !form.name.trim() ? m.sched_err_required() : null);

  function openSheet(group: Group | null): void {
    editing = group;
    form = groupForm(group);
    tried = false;
    saveProblem = null;
    sheetOpen = true;
  }

  async function save(): Promise<void> {
    tried = true;
    saveProblem = null;
    if (!form.name.trim()) return;
    saving = true;
    try {
      const body = groupBody(form);
      if (editing) {
        await unwrap(
          fleetApi.PATCH('/api/v1/fleet/groups/{group_id}', {
            params: { path: { group_id: editing.id } },
            body,
          }),
        );
      } else {
        await unwrap(fleetApi.POST('/api/v1/fleet/groups', { body }));
      }
      toasts.push('ok', editing ? m.saved() : m.groups_created({ name: body.name }));
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

  async function remove(group: Group): Promise<void> {
    if (confirmDelete !== group.id) {
      confirmDelete = group.id;
      return;
    }
    confirmDelete = null;
    try {
      await unwrap(
        fleetApi.DELETE('/api/v1/fleet/groups/{group_id}', {
          params: { path: { group_id: group.id } },
        }),
      );
      toasts.push('ok', m.groups_deleted({ name: group.name }));
      await load();
    } catch (error) {
      const found = asProblem(error);
      toasts.push(
        'error',
        found.status === 409 ? m.groups_delete_conflict() : problemMessage(found),
        undefined,
        found.correlationId ?? undefined,
      );
    }
  }

  async function toggle(mowerId: string, group: Group, member: boolean): Promise<void> {
    const mower = places?.mowers.find((x) => x.id === mowerId);
    if (!mower || !places) return;
    toggling = mowerId;
    memberProblem = null;
    try {
      const saved = await unwrap(
        fleetApi.PUT('/api/v1/fleet/mowers/{mower_id}/groups', {
          params: { path: { mower_id: mowerId } },
          body: { group_ids: toggleGroup(mower.group_ids, group.id, member) },
        }),
      );
      places = {
        ...places,
        mowers: places.mowers.map((x) => (x.id === saved.id ? saved : x)),
      };
    } catch (error) {
      memberProblem = asProblem(error);
    } finally {
      toggling = null;
    }
  }
</script>

<svelte:head><title>{m.groups_title()} · {m.app_name()}</title></svelte:head>

<TopBar title={m.groups_title()} meta={places ? String(groups.length) : undefined}>
  {#snippet actions()}
    {#if canCreate}
      <Button
        icon="plus"
        label={m.groups_add()}
        testid="group-add"
        onclick={() => openSheet(null)}
      />
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
{:else if groups.length === 0}
  <section class="empty" data-testid="groups-empty">
    <h2>{m.groups_empty_title()}</h2>
    <p class="quiet">{m.groups_empty_body()}</p>
    {#if canCreate}
      <Button variant="primary" icon="plus" onclick={() => openSheet(null)}>{m.groups_add()}</Button
      >
    {/if}
  </section>
{:else}
  <ul class="groups" data-testid="groups">
    {#each groups as group (group.id)}
      {@const list = members(group.id)}
      {@const right = rights[group.id] ?? { manage: false, bulk: false }}
      <li class="group" data-testid="group-{group.id}" data-name={group.name}>
        <div class="head">
          <span class="glyph"><Icon name="group" size={20} /></span>
          <div class="titles">
            <h2>{group.name}</h2>
            <span class="meta">
              {group.site_id
                ? m.groups_at_site({ site: siteName.get(group.site_id) ?? '' })
                : m.groups_cross_site()}
            </span>
          </div>
          <span class="readout"
            ><span class="value mono">{formatNumber(list.length)}</span>
            <span class="unit">{m.sites_mowers()}</span></span
          >
        </div>
        {#if list.length}
          <ul class="chips" aria-label={m.groups_members()} data-testid="group-members-{group.id}">
            {#each list as mower (mower.id)}
              <li>
                <a href="/mowers/{mower.id}" class="chip"
                  ><Icon name="mower" size={16} />{mower.name}
                  <span class="chip-site">{siteName.get(mower.site_id) ?? ''}</span></a
                >
              </li>
            {/each}
          </ul>
        {:else}
          <p class="quiet">{m.groups_no_members()}</p>
        {/if}
        <div class="actions">
          {#if right.bulk && list.length}
            <Button
              icon="pause"
              testid="group-pause-{group.id}"
              onclick={() => (bulk = { kind: 'pause_mow', group })}>{m.bulk_pause_all()}</Button
            >
            <Button
              icon="dock"
              testid="group-dock-{group.id}"
              onclick={() => (bulk = { kind: 'return_to_dock', group })}>{m.bulk_dock_all()}</Button
            >
          {/if}
          {#if right.manage}
            <Button
              variant="ghost"
              icon="users"
              testid="group-members-edit-{group.id}"
              onclick={() => (membersOf = group)}>{m.groups_edit_members()}</Button
            >
            <Button
              variant="ghost"
              icon="settings"
              label={m.groups_edit({ name: group.name })}
              testid="group-edit-{group.id}"
              onclick={() => openSheet(group)}
            />
          {/if}
          {#if canSchedule}
            <Button variant="ghost" icon="calendar" href="/schedules/new?group={group.id}"
              >{m.groups_schedule()}</Button
            >
          {/if}
          {#if right.manage}
            <Button
              variant="danger"
              icon="trash"
              testid="group-delete-{group.id}"
              onclick={() => remove(group)}
              >{confirmDelete === group.id ? m.sites_delete_confirm() : m.sites_delete()}</Button
            >
          {/if}
        </div>
      </li>
    {/each}
  </ul>
{/if}

<Sheet
  open={sheetOpen}
  title={editing ? m.groups_edit({ name: editing.name }) : m.groups_add()}
  closeLabel={m.close()}
  onclose={() => (sheetOpen = false)}
  testid="group-sheet"
>
  <div class="form">
    <Field
      id="group-form-name"
      label={m.groups_name()}
      required
      maxlength={100}
      bind:value={form.name}
      error={nameIssue ?? saveProblem?.fieldError('name') ?? null}
      testid="group-form-name"
    />
    <Select
      id="group-form-site"
      label={m.groups_site()}
      hint={m.groups_site_hint()}
      options={[
        { value: '', label: m.groups_cross_site() },
        ...sites.map((s) => ({ value: s.id, label: s.name })),
      ]}
      bind:value={form.siteId}
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
    <Button
      variant="primary"
      full
      icon="check"
      pending={saving}
      testid="group-form-save"
      onclick={save}>{m.save()}</Button
    >
  {/snippet}
</Sheet>

<Sheet
  open={membersOf !== null}
  title={membersOf ? m.groups_members_title({ name: membersOf.name }) : ''}
  closeLabel={m.close()}
  onclose={() => (membersOf = null)}
  testid="group-members-sheet"
>
  {#if membersOf}
    {@const group = membersOf}
    <ul class="toggles">
      {#each places?.mowers ?? [] as mower (mower.id)}
        {@const member = mower.group_ids.includes(group.id)}
        <li>
          <button
            type="button"
            class="toggle"
            aria-pressed={member}
            disabled={toggling !== null}
            data-testid="member-{mower.id}"
            onclick={() => toggle(mower.id, group, !member)}
          >
            <span class="box" aria-hidden="true"
              >{#if member}<Icon name="check" size={16} />{/if}</span
            >
            <span class="who">
              <span class="name">{mower.name}</span>
              <span class="meta">{siteName.get(mower.site_id) ?? ''}</span>
            </span>
          </button>
        </li>
      {/each}
    </ul>
    {#if memberProblem}
      <ProblemNotice
        title={problemMessage(memberProblem)}
        detail={memberProblem.detail}
        reference={memberProblem.correlationId}
        referenceLabel={m.problem_reference()}
      />
    {/if}
  {/if}
</Sheet>

<BulkSheet
  kind={bulk?.kind ?? null}
  targets={bulk ? [{ type: 'group', id: bulk.group.id }] : []}
  mowers={bulk ? members(bulk.group.id) : []}
  scope={bulk ? m.bulk_scope_group({ name: bulk.group.name }) : ''}
  onclose={() => (bulk = null)}
/>

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
    font-variant-numeric: tabular-nums;
  }

  .empty {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--md-space-3);
    max-width: 520px;
    padding: var(--md-space-8) var(--md-gutter);
  }

  .groups {
    display: grid;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .group {
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
  }

  .titles {
    display: flex;
    flex: 1;
    flex-direction: column;
    gap: var(--md-space-0_5);
    min-width: 0;
  }

  h2 {
    margin: 0;
    font-size: var(--md-font-size-20);
    font-weight: var(--md-weight-semibold);
  }

  .meta {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
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

  .chip-site {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
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

  .toggles {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .toggle {
    display: flex;
    align-items: center;
    gap: var(--md-space-3);
    width: 100%;
    min-height: var(--md-target-command);
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
    cursor: progress;
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
  }

  .name {
    font-weight: var(--md-weight-semibold);
  }

  @media (min-width: 1024px) {
    .groups {
      grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
      gap: var(--md-space-4);
      padding: var(--md-space-4) var(--md-gutter);
    }

    .group {
      align-content: start;
      border: var(--md-hairline) solid var(--md-color-line);
    }
  }
</style>
