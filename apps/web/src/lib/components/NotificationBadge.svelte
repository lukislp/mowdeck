<!--
  The kind of a notification as a word, a glyph and the state tone of what happened (stuck is
  the error tone, battery low the warning tone, offline dashed); never colour alone.
-->
<script lang="ts">
  import { kindLabel, kindTone, type NotificationKind } from '#lib/notifications/kinds.js';

  let { kind }: { kind: NotificationKind } = $props();
  const tone = $derived(kindTone(kind));
</script>

<span class="badge" data-tone={tone} data-kind={kind} class:dashed={tone === 'offline'}>
  <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
    {#if kind === 'mowing_finished'}
      <path d="M2 6.5l2.5 2.5L10 3.5" class="stroke" />
    {:else if kind === 'mower_stuck'}
      <path d="M5 2h2v5H5zM5 8.5h2v2H5z" />
    {:else if kind === 'battery_low'}
      <path d="M2 4h7v4H2zM9.5 5.2h1v1.6h-1M3 5h1.4v2H3z" class="mixed" />
    {:else if kind === 'mower_offline'}
      <circle cx="6" cy="6" r="3.6" class="stroke" />
    {:else if kind === 'mower_online'}
      <circle cx="6" cy="6" r="3.6" />
    {:else if kind === 'job_started'}
      <path d="M3 2l7 4-7 4z" />
    {:else}
      <path d="M6 1.5v5M6 9v1.5" class="stroke" />
    {/if}
  </svg>
  <span>{kindLabel(kind)}</span>
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
    width: 12px;
    height: 12px;
    fill: currentcolor;
  }

  .stroke {
    fill: none;
    stroke: currentcolor;
    stroke-width: 1.5;
  }

  .mixed {
    fill: none;
    stroke: currentcolor;
    stroke-width: 1;
  }
</style>
