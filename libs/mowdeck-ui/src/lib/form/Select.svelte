<!-- A labelled native select (native pickers work best with gloves and on phones). -->
<script lang="ts">
  interface Option {
    value: string;
    label: string;
  }
  interface Props {
    id: string;
    label: string;
    options: Option[];
    value?: string;
    hint?: string;
  }
  let { id, label, options, value = $bindable(''), hint }: Props = $props();
</script>

<div class="md-select">
  <label for={id}>{label}</label>
  <select {id} name={id} bind:value aria-describedby={hint ? `${id}-hint` : undefined}>
    {#each options as option (option.value)}
      <option value={option.value}>{option.label}</option>
    {/each}
  </select>
  {#if hint}<span class="hint" id="{id}-hint">{hint}</span>{/if}
</div>

<style>
  .md-select {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
  }

  label {
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    color: var(--md-color-ink-muted);
  }

  select {
    min-height: var(--md-target);
    padding: 0 var(--md-space-3);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
  }

  .hint {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }
</style>
