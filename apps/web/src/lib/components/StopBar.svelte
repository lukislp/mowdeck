<!--
  The emergency stop of a mower sub-page (drive, health) as a fixed command bar without other
  actions: one tap sends `emergency_stop` through Fleet, the status follows the command result.
  Full width at the bottom on phones, the instrument column's foot on desktops. It lifts the
  toasts above itself and reports its height (bindable) so the page can keep content clear.
  The root element gets `--md-stop-clearance` (bar height) or, as a rail, `--md-stop-inset-start`
  (rail width), so layout-level sheets and toasts keep clear of the stop (`publishStopClearance`).

  `rail`: a phone held sideways (the drive view, ADR 0044) gets a narrow vertical rail at the
  left edge instead, full height, `--md-stop-rail` wide (default 112 px), the stop upright at its
  foot and the page's own controls (`children`, e.g. the drive session's Stop) above it.
-->
<script lang="ts">
  import { CommandBar, type StopState } from '@mowdeck/ui';
  import type { Snippet } from 'svelte';

  import { commands, sendCommand } from '#lib/live.svelte.js';
  import { m } from '#lib/paraglide/messages.js';
  import { publishStopClearance } from '#lib/stop-clearance.js';

  interface Props {
    mowerId: string;
    /** Runs first, e.g. ending a drive session. */
    onbeforestop?: () => void;
    height?: number;
    /** A narrow vertical rail at the left edge (phone held sideways). */
    rail?: boolean;
    /** Controls above the stop in the rail. */
    children?: Snippet;
  }
  let { mowerId, onbeforestop, height = $bindable(120), rail = false, children }: Props = $props();
  let width = $state(0);

  let stopState = $state<StopState>('idle');

  async function onstop(): Promise<void> {
    onbeforestop?.();
    stopState = 'sending';
    const ok = await sendCommand(mowerId, { kind: 'emergency_stop' });
    // The result can arrive over the socket before the 202 response; keep what it set.
    if (!ok) stopState = 'failed';
    else if (stopState === 'sending') stopState = 'sent';
  }

  const lastStop = $derived(commands.latest(mowerId, 'emergency_stop'));
  $effect(() => {
    if (lastStop?.status === 'executed') stopState = 'executed';
    else if (lastStop && ['rejected', 'failed', 'expired'].includes(lastStop.status)) {
      stopState = 'failed';
    }
  });
  const stopStatus = $derived(
    stopState === 'sending'
      ? m.stop_sending()
      : stopState === 'sent'
        ? m.stop_sent()
        : stopState === 'executed'
          ? m.stop_executed()
          : stopState === 'failed'
            ? m.stop_failed()
            : '',
  );

  $effect(() => {
    const root = document.documentElement;
    const isPhone = !rail && !window.matchMedia('(min-width: 1024px)').matches;
    root.style.setProperty('--md-toast-offset', isPhone ? `${height + 8}px` : '16px');
    return () => root.style.removeProperty('--md-toast-offset');
  });
  $effect(() => publishStopClearance(rail ? { insetStart: width } : { bottom: height }));
</script>

<div
  class="stop-bar"
  class:rail
  bind:clientHeight={height}
  bind:clientWidth={width}
  data-testid="stop-bar"
  data-variant={rail ? 'rail' : 'bar'}
>
  {#if rail && children}
    <div class="rail-extra">{@render children()}</div>
  {/if}
  <CommandBar
    {rail}
    actions={[]}
    onaction={() => undefined}
    label={m.cmd_bar_label()}
    stopLabel={m.cmd_stop()}
    {stopStatus}
    {stopState}
    {onstop}
  />
</div>

<style>
  .stop-bar {
    position: fixed;
    inset-inline: 0;
    bottom: 0;
    z-index: 26;
  }

  .rail {
    inset-block: var(--md-safe-top) 0;
    inset-inline: 0 auto;
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
    gap: var(--md-space-2);
    width: calc(var(--md-stop-rail, 112px) + env(safe-area-inset-left, 0px));
    padding: var(--md-space-2) var(--md-space-2)
      calc(var(--md-space-2) + env(safe-area-inset-bottom, 0px))
      calc(var(--md-space-2) + env(safe-area-inset-left, 0px));
    border-inline-end: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  .rail :global(.md-command-bar) {
    height: auto;
    padding: 0;
    background: none;
  }

  .rail-extra {
    display: grid;
    gap: var(--md-space-2);
  }

  @media (min-width: 1024px) {
    .stop-bar:not(.rail) {
      inset-inline: auto 0;
      width: var(--md-instrument-column);
      padding: var(--md-space-3) var(--md-space-4) var(--md-space-4);
      border-top: var(--md-hairline) solid var(--md-color-line);
      border-inline-start: var(--md-hairline) solid var(--md-color-line);
      background: var(--md-color-surface);
    }
  }
</style>
