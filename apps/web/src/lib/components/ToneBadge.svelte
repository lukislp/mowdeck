<!--
  A state word with a glyph in the state colour language (rain verdicts, run outcomes): colour
  never alone, the offline tone with a dashed outline. `value` lands in `data-value` for tests.
-->
<script lang="ts">
  import type { Glyph, Tone } from '#lib/schedules/labels.js';

  interface Props {
    tone: Tone;
    glyph: Glyph;
    label: string;
    value?: string;
    testid?: string;
  }
  let { tone, glyph, label, value, testid }: Props = $props();
</script>

<span
  class="badge"
  data-tone={tone}
  data-value={value}
  data-testid={testid}
  class:dashed={tone === 'offline'}
>
  <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
    {#if glyph === 'check'}
      <path d="M2 6.5l2.5 2.5L10 3.5" class="stroke" />
    {:else if glyph === 'rain'}
      <path d="M6 1.5C6 1.5 2.8 5.4 2.8 7.5a3.2 3.2 0 0 0 6.4 0C9.2 5.4 6 1.5 6 1.5Z" />
    {:else if glyph === 'pin'}
      <path d="M6 10.5S2.8 7.6 2.8 5a3.2 3.2 0 0 1 6.4 0c0 2.6-3.2 5.5-3.2 5.5Z" class="stroke" />
    {:else if glyph === 'question'}
      <path d="M4.2 4.4a1.9 1.9 0 1 1 2.6 1.8c-.5.2-.8.6-.8 1.1v.4M6 9.4v.6" class="stroke" />
    {:else if glyph === 'skip'}
      <path d="M2.5 2.5l5 3.5-5 3.5zM9.5 2.5v7" />
    {:else if glyph === 'clock'}
      <path d="M6 1.8a4.2 4.2 0 1 1 0 8.4 4.2 4.2 0 0 1 0-8.4ZM6 3.8V6l1.6 1" class="stroke" />
    {:else if glyph === 'alert'}
      <path d="M5 2h2v5H5zM5 8.5h2v2H5z" />
    {:else}
      <path d="M2.5 6h7" class="stroke" />
    {/if}
  </svg>
  <span>{label}</span>
</span>

<style>
  .badge {
    --tone: var(--md-state-unknown);
    --tone-bg: var(--md-state-unknown-bg);

    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    min-height: 24px;
    padding: 0 var(--md-space-2) 0 var(--md-space-1);
    border: var(--md-hairline) solid var(--tone);
    border-radius: var(--md-radius-1);
    background: var(--tone-bg);
    color: var(--tone);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    white-space: nowrap;
  }

  .dashed {
    border-style: dashed;
  }

  [data-tone='mowing'] {
    --tone: var(--md-state-mowing);
    --tone-bg: var(--md-state-mowing-bg);
  }

  [data-tone='returning'] {
    --tone: var(--md-state-returning);
    --tone-bg: var(--md-state-returning-bg);
  }

  [data-tone='docked'] {
    --tone: var(--md-state-docked);
    --tone-bg: var(--md-state-docked-bg);
  }

  [data-tone='paused'] {
    --tone: var(--md-state-paused);
    --tone-bg: var(--md-state-paused-bg);
  }

  [data-tone='error'] {
    --tone: var(--md-state-error);
    --tone-bg: var(--md-state-error-bg);
  }

  [data-tone='offline'] {
    --tone: var(--md-state-offline);
    --tone-bg: var(--md-state-offline-bg);
  }

  svg {
    flex: none;
    width: 12px;
    height: 12px;
    fill: currentcolor;
  }

  .stroke {
    fill: none;
    stroke: currentcolor;
    stroke-width: 1.5;
  }
</style>
