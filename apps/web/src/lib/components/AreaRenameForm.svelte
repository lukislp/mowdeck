<!--
  Rename one map area in MowDeck: the name field, the mower's own name as a hint (the mower keeps
  it; MowDeck never writes to the mower), save, "Zurücksetzen" (drop the custom name) and cancel.
  Used inline in the area lists and inside the rename sheet the map labels open.
-->
<script lang="ts">
  import { Button, Field } from '@mowdeck/ui';
  import { untrack } from 'svelte';

  import { asProblem } from '$lib/api/problem';
  import { problemMessage } from '$lib/i18n';
  import {
    AREA_NAME_MAX,
    initialRenameValue,
    renameIntent,
    saveAreaName,
  } from '$lib/mower/area-names';
  import { areaLabel, isPlaceholderName, type AreaNames } from '$lib/mower/map-labels';
  import { m } from '$lib/paraglide/messages';
  import { toasts } from '$lib/stores/ui.svelte';

  interface Props {
    mowerId: string;
    area: { id: string; name?: string | null };
    /** 0-based position in the map's area order (for the "Area N" fallback). */
    index: number | null;
    names: AreaNames;
    onsaved: (names: Record<string, string>) => void;
    oncancel: () => void;
    /** Inline in a list row: tighter spacing, no autofocus jump of the page. */
    compact?: boolean;
  }
  let { mowerId, area, index, names, onsaved, oncancel, compact = false }: Props = $props();

  // The form starts from the names it was opened with; later prop changes do not overwrite typing.
  let value = $state(untrack(() => initialRenameValue(area, names)));
  let busy = $state(false);

  const fallback = $derived(areaLabel({ id: area.id }, { index }));
  const own = $derived(isPlaceholderName(area.name) ? null : (area.name?.trim() ?? null));
  const hasOverride = $derived(!!names?.[area.id]?.trim());
  const intent = $derived(renameIntent(value, area, names));
  const fieldId = $derived(`area-rename-${compact ? 'inline' : 'sheet'}-${area.id}`);

  async function apply(name: string | null): Promise<void> {
    busy = true;
    try {
      const saved = await saveAreaName(mowerId, area.id, name);
      const shown = areaLabel(area, { overrides: saved, index });
      toasts.push(
        'ok',
        name === null
          ? m.area_rename_reset_done({ name: shown })
          : m.area_rename_saved({ name: shown }),
      );
      onsaved(saved);
    } catch (error) {
      const p = asProblem(error);
      toasts.push('error', problemMessage(p), undefined, p.correlationId ?? undefined);
    } finally {
      busy = false;
    }
  }

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (busy) return;
    const next = intent;
    if (next.kind === 'too_long') return;
    if (next.kind === 'none') oncancel();
    else await apply(next.kind === 'set' ? next.name : null);
  }

  function focusOnMount(node: HTMLElement): void {
    node.querySelector('input')?.focus({ preventScroll: compact });
  }
</script>

<form
  class="rename"
  class:compact
  onsubmit={submit}
  data-testid="area-rename-form"
  data-rename-area={area.id}
  use:focusOnMount
>
  <Field
    id={fieldId}
    label={m.area_rename_field()}
    bind:value
    placeholder={fallback}
    maxlength={AREA_NAME_MAX + 20}
    autocomplete="off"
    testid="area-rename-input"
    error={intent.kind === 'too_long' ? m.area_rename_too_long({ max: AREA_NAME_MAX }) : null}
  />
  <p class="hint" data-testid="area-rename-hint">
    {#if own}
      {m.area_rename_hint_mower({ name: own })}
    {:else}
      {m.area_rename_hint_none({ fallback })}
    {/if}
  </p>
  <div class="actions">
    <Button
      type="submit"
      variant="primary"
      icon="check"
      pending={busy}
      disabled={busy || intent.kind === 'too_long'}
      testid="area-rename-save">{m.area_rename_save()}</Button
    >
    {#if hasOverride}
      <Button
        variant="secondary"
        disabled={busy}
        onclick={() => void apply(null)}
        testid="area-rename-reset">{m.area_rename_reset()}</Button
      >
    {/if}
    <Button variant="ghost" disabled={busy} onclick={oncancel} testid="area-rename-cancel"
      >{m.area_rename_cancel()}</Button
    >
  </div>
</form>

<style>
  .rename {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
    padding-block: var(--md-space-2);
  }

  .rename.compact {
    padding: var(--md-space-2) var(--md-space-3) var(--md-space-3);
    border-inline-start: 3px solid var(--md-color-focus);
    background: var(--md-color-surface-sunken);
  }

  .hint {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
    line-height: var(--md-line-20);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }
</style>
