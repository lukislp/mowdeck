<!--
  Where a role applies: the whole account, a site, a group or one mower (Identity's assignment
  levels; a right on a level covers everything below it). The object list comes from the
  sites, groups and mowers the manager can see.
-->
<script lang="ts">
  import { SegmentedControl, Select } from '@mowdeck/ui';

  import { LEVEL_TYPES, levelLabel, type LevelType, type Places } from '$lib/fleet/data';
  import { sortSites } from '$lib/fleet/places';
  import { m } from '$lib/paraglide/messages';

  interface Props {
    id: string;
    places: Places | null;
    type: LevelType;
    objectId: string;
    testid?: string;
  }
  let {
    id,
    places,
    type = $bindable('account'),
    objectId = $bindable(''),
    testid,
  }: Props = $props();

  const options = $derived.by(() => {
    switch (type) {
      case 'site':
        return sortSites(places?.sites ?? []).map((s) => ({ value: s.id, label: s.name }));
      case 'group':
        return [...(places?.groups ?? [])]
          .sort((a, b) => a.name.localeCompare(b.name))
          .map((g) => ({ value: g.id, label: g.name }));
      case 'mower':
        return [...(places?.mowers ?? [])]
          .sort((a, b) => a.name.localeCompare(b.name))
          .map((x) => ({ value: x.id, label: x.name }));
      default:
        return [];
    }
  });

  // A new level type preselects its first object.
  $effect(() => {
    if (type === 'account') objectId = '';
    else if (!options.some((o) => o.value === objectId)) objectId = options[0]?.value ?? '';
  });
</script>

<div class="level" data-testid={testid}>
  <SegmentedControl
    label={m.level_label()}
    options={LEVEL_TYPES.map((t) => ({ value: t, label: levelLabel(t) }))}
    value={type}
    onchange={(value) => (type = value as LevelType)}
    testid={testid ? `${testid}-type` : undefined}
  />
  {#if type !== 'account'}
    {#if options.length}
      <Select id="{id}-object" label={levelLabel(type)} {options} bind:value={objectId} />
    {:else}
      <p class="hint">{m.level_none()}</p>
    {/if}
  {/if}
</div>

<style>
  .level {
    display: grid;
    gap: var(--md-space-3);
  }

  .hint {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }
</style>
