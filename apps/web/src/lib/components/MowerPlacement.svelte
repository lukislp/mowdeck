<!--
  Where a mower belongs: its site (moving needs `can_manage_mowers` on the mower and on the new
  site) and its groups (each change sends the whole set with `PUT /mowers/{id}/groups`). Shown
  on the mower settings page; the fleet's sites and groups pages offer the same moves.
-->
<script lang="ts">
  import { Button, Icon, Panel, ProblemNotice, Select } from '@mowdeck/ui';

  import { fleetApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import { displaySites, type Group, type Mower, type Site } from '#lib/fleet/data.js';
  import { sortSites, toggleGroup } from '#lib/fleet/places.js';
  import { problemMessage } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';
  import { toasts } from '#lib/stores/ui.svelte.js';
  import DemoLock from '#lib/demo/DemoLock.svelte';

  interface Props {
    mower: Mower;
    onchange: (saved: Mower) => void;
  }
  let { mower, onchange }: Props = $props();

  let sites = $state<Site[]>([]);
  let groups = $state<Group[]>([]);
  // The chosen site follows the mower until the user picks another one.
  let siteId = $derived(mower.site_id);
  let busy = $state(false);
  let toggling = $state<string | null>(null);
  let problem = $state<ApiProblem | null>(null);

  $effect(() => {
    void (async () => {
      const [foundSites, foundGroups] = await Promise.all([
        unwrap(
          fleetApi.GET('/api/v1/fleet/sites', { params: { query: { include_default: true } } }),
        ).catch(() => ({ items: [] as Site[] })),
        unwrap(fleetApi.GET('/api/v1/fleet/groups')).catch(() => ({ items: [] as Group[] })),
      ]);
      sites = sortSites(displaySites(foundSites.items));
      groups = [...foundGroups.items].sort((a, b) => a.name.localeCompare(b.name));
    })();
  });

  const siteName = $derived(new Map(sites.map((s) => [s.id, s.name])));
  const siteOptions = $derived.by(() => {
    const list = sites.map((s) => ({ value: s.id, label: s.name }));
    if (!sites.some((s) => s.id === mower.site_id)) {
      list.unshift({ value: mower.site_id, label: m.placement_unknown_site() });
    }
    return list;
  });

  async function move(): Promise<void> {
    busy = true;
    problem = null;
    try {
      const saved = await unwrap(
        fleetApi.PATCH('/api/v1/fleet/mowers/{mower_id}', {
          params: { path: { mower_id: mower.id } },
          body: { site_id: siteId },
        }),
      );
      onchange(saved);
      toasts.push('ok', m.sites_moved({ mower: saved.name, site: siteName.get(siteId) ?? '' }));
    } catch (error) {
      problem = asProblem(error);
    } finally {
      busy = false;
    }
  }

  async function toggle(group: Group, member: boolean): Promise<void> {
    toggling = group.id;
    problem = null;
    try {
      const saved = await unwrap(
        fleetApi.PUT('/api/v1/fleet/mowers/{mower_id}/groups', {
          params: { path: { mower_id: mower.id } },
          body: { group_ids: toggleGroup(mower.group_ids, group.id, member) },
        }),
      );
      onchange(saved);
    } catch (error) {
      problem = asProblem(error);
    } finally {
      toggling = null;
    }
  }
</script>

<Panel label={m.placement_title()} testid="mower-placement">
  <p class="hint">{m.placement_hint()}</p>
  <!-- Moving a mower to another site changes the mower, which the demo refuses (Fleet). -->
  <DemoLock testid="demo-lock-placement">
    <div class="row">
      <Select
        id="placement-site"
        label={m.placement_site()}
        options={siteOptions}
        bind:value={siteId}
      />
      <Button
        icon="chevron"
        pending={busy}
        disabled={siteId === mower.site_id}
        testid="placement-move"
        onclick={move}>{m.sites_move()}</Button
      >
    </div>
  </DemoLock>
  {#if groups.length}
    <h3 class="label">{m.placement_groups()}</h3>
    <ul class="toggles">
      {#each groups as group (group.id)}
        {@const member = mower.group_ids.includes(group.id)}
        <li>
          <button
            type="button"
            class="toggle"
            aria-pressed={member}
            disabled={toggling !== null}
            data-testid="placement-group-{group.id}"
            onclick={() => toggle(group, !member)}
          >
            <span class="box" aria-hidden="true"
              >{#if member}<Icon name="check" size={16} />{/if}</span
            >
            <span class="who">
              <span class="name">{group.name}</span>
              <span class="meta">
                {group.site_id
                  ? m.groups_at_site({ site: siteName.get(group.site_id) ?? '' })
                  : m.groups_cross_site()}
              </span>
            </span>
          </button>
        </li>
      {/each}
    </ul>
  {:else}
    <p class="hint">{m.placement_no_groups()}</p>
  {/if}
  {#if problem}
    <ProblemNotice
      tone={problem.status === 403 ? 'info' : 'error'}
      title={problem.status === 403 ? m.placement_forbidden() : problemMessage(problem)}
      detail={problem.detail}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  {/if}
</Panel>

<style>
  .hint {
    margin: 0 0 var(--md-space-3);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: end;
    gap: var(--md-space-2);
  }

  .label {
    margin: var(--md-space-4) 0 var(--md-space-1);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
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
    min-height: var(--md-target);
    padding: var(--md-space-1) 0;
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

  .meta {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }
</style>
