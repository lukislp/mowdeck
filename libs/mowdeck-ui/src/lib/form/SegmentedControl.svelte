<!-- A small set of exclusive options (theme, language) as one row of 48 px segments. -->
<script lang="ts">
  interface Option {
    value: string;
    label: string;
  }
  interface Props {
    label: string;
    options: Option[];
    value: string;
    onchange: (value: string) => void;
    testid?: string;
  }
  let { label, options, value, onchange, testid }: Props = $props();
</script>

<div class="md-segmented" role="radiogroup" aria-label={label} data-testid={testid}>
  {#each options as option (option.value)}
    <button
      type="button"
      role="radio"
      aria-checked={option.value === value}
      data-value={option.value}
      onclick={() => onchange(option.value)}>{option.label}</button
    >
  {/each}
</div>

<style>
  .md-segmented {
    display: inline-grid;
    grid-auto-columns: minmax(0, 1fr);
    grid-auto-flow: column;
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    overflow: hidden;
  }

  button {
    min-height: var(--md-target);
    padding: 0 var(--md-space-4);
    border: 0;
    border-inline-start: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-weight: var(--md-weight-medium);
    cursor: pointer;
  }

  button:first-child {
    border-inline-start: 0;
  }

  button[aria-checked='true'] {
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }
</style>
