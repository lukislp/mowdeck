<!--
  The emergency stop of a mower sub-page (drive, health) as a fixed command bar without other
  actions: one tap sends `emergency_stop` through Fleet, the status follows the command result.
  Full width at the bottom on phones, the instrument column's foot on desktops. It lifts the
  toasts above itself and reports its height (bindable) so the page can keep content clear.
-->
<script lang="ts">
  import { CommandBar, type StopState } from '@mowdeck/ui';

  import { commands, sendCommand } from '$lib/live.svelte';
  import { m } from '$lib/paraglide/messages';

  interface Props {
    mowerId: string;
    /** Runs first, e.g. ending a drive session. */
    onbeforestop?: () => void;
    height?: number;
  }
  let { mowerId, onbeforestop, height = $bindable(120) }: Props = $props();

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
    const isPhone = !window.matchMedia('(min-width: 1024px)').matches;
    root.style.setProperty('--md-toast-offset', isPhone ? `${height + 8}px` : '16px');
    return () => root.style.removeProperty('--md-toast-offset');
  });
</script>

<div class="stop-bar" bind:clientHeight={height}>
  <CommandBar
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

  @media (min-width: 1024px) {
    .stop-bar {
      inset-inline: auto 0;
      width: var(--md-instrument-column);
      padding: var(--md-space-3) var(--md-space-4) var(--md-space-4);
      border-top: var(--md-hairline) solid var(--md-color-line);
      border-inline-start: var(--md-hairline) solid var(--md-color-line);
      background: var(--md-color-surface);
    }
  }
</style>
