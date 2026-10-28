<!--
  A labelled value of the instrument panel: uppercase label, monospaced value with tabular
  figures, the unit in a smaller weight. A changed value flashes once in the state tint.
-->
<script lang="ts">
  import type { Snippet } from 'svelte';

  interface Props {
    label: string;
    /** The formatted value; null shows `emptyText`. */
    value: string | null;
    unit?: string;
    /** Shown instead of a value when there is none (e.g. "keine Daten"). */
    emptyText: string;
    size?: 'primary' | 'secondary' | 'inline';
    /** The value was commanded but not yet confirmed by the mower. */
    pending?: boolean;
    /** Optional state tone of the value (e.g. RTK quality). */
    tone?: 'mowing' | 'paused' | 'error' | 'charging' | 'returning' | null;
    /** Extra content under the value (a gauge, a hint). */
    children?: Snippet;
    testid?: string;
  }
  let {
    label,
    value,
    unit,
    emptyText,
    size = 'secondary',
    pending = false,
    tone = null,
    children,
    testid,
  }: Props = $props();

  let flash = $state(false);
  let previous: string | null | undefined;
  $effect(() => {
    const current = value;
    if (previous !== undefined && current !== previous && current !== null) {
      flash = true;
      const timer = setTimeout(() => (flash = false), 300);
      previous = current;
      return () => clearTimeout(timer);
    }
    previous = current;
  });
</script>

<div class="md-readout {size}" class:flash class:pending data-testid={testid}>
  <span class="label">{label}</span>
  {#if value === null}
    <span class="empty">{emptyText}</span>
  {:else}
    <span class="value" data-tone={tone}>
      <span class="number">{value}</span>{#if unit}<span class="unit">{unit}</span>{/if}
    </span>
  {/if}
  {#if children}{@render children()}{/if}
</div>

<style>
  .md-readout {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    min-width: 0;
    padding: var(--md-space-2) var(--md-space-3);
    background: var(--md-color-surface-sunken);
    border-radius: var(--md-radius-0);
    transition: background-color var(--md-motion-flash) var(--md-ease);
  }

  .md-readout.inline {
    padding: 0;
    background: none;
  }

  .md-readout.flash {
    background: var(--md-state-mowing-bg);
  }

  .label {
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    color: var(--md-color-ink-muted);

    /* Long labels ("Messer seit Wechsel (MowDeck)") wrap inside the readout; a label kept on one
       line ran past a narrow grid column and the phone page scrolled sideways. */
    overflow-wrap: anywhere;
  }

  .value {
    display: flex;
    align-items: baseline;
    gap: 0.25em;
    font-family: var(--md-font-mono);
    font-weight: var(--md-weight-medium);
    font-variant-numeric: tabular-nums slashed-zero;
    white-space: nowrap;
  }

  /* A long text value (a firmware id) breaks rather than widening the page; a value that fits stays on one line. */
  .number {
    min-width: 0;
    overflow-wrap: anywhere;
    white-space: normal;
  }

  .primary .value {
    font-size: var(--md-font-size-28);
    line-height: var(--md-line-32);
  }

  .secondary .value {
    font-size: var(--md-font-size-20);
    line-height: var(--md-line-24);
  }

  .inline .value {
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .unit {
    font-size: 0.6em;
    font-weight: var(--md-weight-regular);
    color: var(--md-color-ink-muted);
  }

  .empty {
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-24);
    color: var(--md-color-ink-muted);
  }

  .pending .number {
    color: var(--md-color-ink-muted);
    text-decoration: underline dotted;
    text-underline-offset: 4px;
  }

  [data-tone='mowing'] {
    color: var(--md-state-mowing);
  }

  [data-tone='paused'] {
    color: var(--md-state-paused);
  }

  [data-tone='error'] {
    color: var(--md-state-error);
  }

  [data-tone='charging'] {
    color: var(--md-state-charging);
  }

  [data-tone='returning'] {
    color: var(--md-state-returning);
  }

  @media (prefers-reduced-motion: reduce) {
    .md-readout.flash {
      background: var(--md-color-surface-sunken);
    }
  }
</style>
