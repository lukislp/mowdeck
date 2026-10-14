<!--
  The emergency stop: a red actuator on a yellow ring (machine stop convention), the only element
  using the "Signal stop" colours. One tap sends; no confirmation, never disabled, never animated.
-->
<script lang="ts">
  import type { StopState } from '../types.js';

  interface Props {
    /** "NOT-STOPP" / "STOP". */
    label: string;
    /** Feedback under the label (e.g. "gesendet"), empty when idle. */
    status?: string;
    state?: StopState;
    onstop: () => void;
  }
  let { label, status = '', state = 'idle', onstop }: Props = $props();

  function press(): void {
    if (typeof navigator !== 'undefined' && 'vibrate' in navigator) navigator.vibrate(30);
    onstop();
  }
</script>

<button
  type="button"
  class="md-stop"
  data-state={state}
  data-testid="emergency-stop"
  aria-describedby={status ? 'md-stop-status' : undefined}
  onclick={press}
>
  <span class="actuator" aria-hidden="true"><span class="cap"></span></span>
  <span class="text">
    <span class="label">{label}</span>
    {#if status}<span class="status" id="md-stop-status" role="status">{status}</span>{/if}
  </span>
</button>

<style>
  .md-stop {
    display: flex;
    align-items: center;
    gap: var(--md-space-3);
    width: 100%;
    min-height: var(--md-target-stop);
    padding: var(--md-space-2) var(--md-space-4) var(--md-space-2) var(--md-space-2);
    border: 0;
    border-radius: var(--md-radius-2);
    background: var(--md-stop-fill);
    color: var(--md-stop-ink);
    cursor: pointer;
    text-align: left;
    touch-action: manipulation;
  }

  .md-stop:active {
    background: var(--md-stop-fill-pressed);
  }

  .md-stop:focus-visible {
    outline: 3px solid var(--md-stop-ring);
    outline-offset: 2px;
  }

  .actuator {
    display: grid;
    place-items: center;
    width: 48px;
    height: 48px;
    flex: none;
    border-radius: var(--md-radius-round);
    background: var(--md-stop-ring);
  }

  .cap {
    width: 30px;
    height: 30px;
    border-radius: var(--md-radius-round);
    background: var(--md-stop-fill);
    box-shadow: inset 0 -3px 0 var(--md-stop-fill-pressed);
  }

  .text {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .label {
    font-size: var(--md-font-size-16);
    line-height: var(--md-line-20);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .status {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }
</style>
