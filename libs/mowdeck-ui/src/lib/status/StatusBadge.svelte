<!--
  The activity of a mower: a word, a glyph and the state colour (never colour alone).
  Offline and stale states get a dashed outline.
-->
<script lang="ts">
  import { activityTone, type Activity } from '../types.js';

  interface Props {
    activity: Activity;
    /** The translated activity word, e.g. "Mäht". */
    label: string;
    /** The state is older than expected (shown dashed like offline). */
    stale?: boolean;
    size?: 'regular' | 'large';
  }
  let { activity, label, stale = false, size = 'regular' }: Props = $props();
  const tone = $derived(activityTone(activity));
</script>

<span
  class="md-badge"
  class:large={size === 'large'}
  class:dashed={stale || activity === 'offline'}
  data-tone={tone}
  data-activity={activity}
>
  <svg class="glyph" viewBox="0 0 12 12" aria-hidden="true" focusable="false">
    {#if activity === 'mowing'}
      <path d="M3 2l7 4-7 4z" />
    {:else if activity === 'paused'}
      <path d="M3 2h2.2v8H3zM6.8 2H9v8H6.8z" />
    {:else if activity === 'returning'}
      <path d="M9.5 9.5V5.5H3.5M5.5 3L3 5.5 5.5 8" class="stroke" />
    {:else if activity === 'charging'}
      <path d="M7 1L2.5 7h3l-.5 4L9.5 5h-3z" />
    {:else if activity === 'error'}
      <path d="M5 2h2v5H5zM5 8.5h2v2H5z" />
    {:else if activity === 'offline'}
      <circle cx="6" cy="6" r="3.6" class="stroke" />
    {:else if activity === 'updating'}
      <path d="M6 1.5v6M3.5 5L6 7.5 8.5 5M2.5 10.5h7" class="stroke" />
    {:else if activity === 'unknown'}
      <path d="M4 4.5a2 2 0 1 1 2.8 1.8c-.5.3-.8.6-.8 1.2V8M6 9.6v.9" class="stroke" />
    {:else}
      <rect x="2.5" y="2.5" width="7" height="7" class="stroke" />
    {/if}
  </svg>
  <span class="word">{label}</span>
</span>

<style>
  .md-badge {
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

  .md-badge.large {
    min-height: 32px;
    padding-inline: var(--md-space-2) var(--md-space-3);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .md-badge.dashed {
    border-style: dashed;
  }

  .glyph {
    width: 12px;
    height: 12px;
    flex: none;
    fill: currentcolor;
  }

  .large .glyph {
    width: 14px;
    height: 14px;
  }

  .glyph .stroke {
    fill: none;
    stroke: currentcolor;
    stroke-width: 1.6;
  }

  [data-tone='mowing'] {
    --tone: var(--md-state-mowing);
    --tone-bg: var(--md-state-mowing-bg);
  }

  [data-tone='returning'] {
    --tone: var(--md-state-returning);
    --tone-bg: var(--md-state-returning-bg);
  }

  [data-tone='charging'] {
    --tone: var(--md-state-charging);
    --tone-bg: var(--md-state-charging-bg);
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
</style>
