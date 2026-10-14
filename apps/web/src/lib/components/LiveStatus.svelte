<!-- The socket's state as a small indicator: a square lamp and a word. -->
<script lang="ts">
  import { m } from '$lib/paraglide/messages';
  import type { Phase } from '$lib/realtime/client.svelte';

  let { phase, tooMany = false }: { phase: Phase; tooMany?: boolean } = $props();

  const state = $derived(
    phase === 'ready'
      ? 'ready'
      : phase === 'connecting' || phase === 'authenticating'
        ? 'busy'
        : 'down',
  );
  const label = $derived(
    tooMany
      ? m.live_too_many()
      : state === 'ready'
        ? m.live_ready()
        : state === 'busy'
          ? m.live_connecting()
          : m.live_offline(),
  );
</script>

<span class="live" data-state={state} data-testid="live-status" role="status">
  <span class="lamp" aria-hidden="true"></span>
  <span class="label">{label}</span>
</span>

<style>
  .live {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-2);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    white-space: nowrap;
  }

  .lamp {
    width: 8px;
    height: 8px;
    border: var(--md-hairline) solid var(--md-state-offline);
  }

  [data-state='ready'] .lamp {
    border-color: var(--md-state-mowing);
    background: var(--md-state-mowing);
  }

  [data-state='busy'] .lamp {
    border-color: var(--md-state-paused);
  }

  [data-state='down'] .lamp {
    border-style: dashed;
  }
</style>
