<!--
  The mower's camera light (the headlamp) as an instrument tile: a lamp, the state the mower
  reports (on, off or unknown) and one switch. The switch shows the reported state, never an
  optimistic one: while a `set_light` command is on its way the readout turns to the pending
  style and the switch waits. Disabled without the capability, while the mower is offline and
  without the right to control it; the reason is written below. The rules live in
  `lib/mower/light.ts`, shared with the drive view's `LightChip`.
-->
<script lang="ts">
  import { lightReasonText } from '#lib/i18n.js';
  import { lightView, type LightInput } from '#lib/mower/light.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props extends LightInput {
    onchange: (on: boolean) => void;
  }
  let { on, capable, online, allowed, pending, onchange }: Props = $props();

  const view = $derived(lightView({ on, capable, online, allowed, pending }));
  const reason = $derived(view.reason ? lightReasonText(view.reason) : null);
  const disabled = $derived(view.disabled);
  const readout = $derived(on === true ? m.light_on() : on === false ? m.light_off() : '—');
  const state = $derived(view.state);
</script>

<div
  class="light"
  class:pending
  data-testid="light-control"
  data-state={state}
  data-pending={pending ? 'true' : 'false'}
>
  <span class="label" id="light-label">{m.light_title()}</span>
  <div class="row">
    <span class="lamp {state}" aria-hidden="true"></span>
    <output
      class="value"
      aria-labelledby="light-label"
      aria-live="polite"
      data-testid="light-state"
    >
      <span class="word">{readout}</span>
      {#if on === null}<span class="sr">{m.light_unknown()}</span>{/if}
    </output>
    <button
      type="button"
      role="switch"
      class="toggle"
      aria-checked={on === true}
      aria-labelledby="light-label"
      aria-describedby="light-note"
      aria-busy={pending}
      {disabled}
      data-testid="light-toggle"
      onclick={() => onchange(view.next)}
    >
      <span class="track" aria-hidden="true"><span class="knob"></span></span>
      <span class="action">{on === true ? m.light_switch_off() : m.light_switch_on()}</span>
    </button>
  </div>
  <p class="note" id="light-note" data-testid="light-note">
    {pending ? m.light_pending() : (reason ?? m.light_reported())}
  </p>
</div>

<style>
  .light {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    padding: var(--md-space-2) var(--md-space-3);
    background: var(--md-color-surface-sunken);
  }

  .label {
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    color: var(--md-color-ink-muted);
  }

  .row {
    display: grid;
    grid-template-columns: auto 1fr auto;
    align-items: center;
    gap: var(--md-space-3);
    min-height: var(--md-target);
  }

  /* The lamp: a filled square with a halo when on, hollow when off, dashed when unknown. */
  .lamp {
    width: 14px;
    height: 14px;
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
  }

  .lamp.on {
    border-color: var(--md-state-paused);
    background: var(--md-state-paused);
    box-shadow: 0 0 0 4px var(--md-state-paused-bg);
  }

  .lamp.unknown {
    border-style: dashed;
  }

  .value {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-24);
    line-height: var(--md-line-32);
    font-weight: var(--md-weight-medium);
    text-transform: uppercase;
  }

  .pending .word {
    color: var(--md-color-ink-muted);
    text-decoration: underline dotted;
    text-underline-offset: 4px;
  }

  .sr {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  .toggle {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-2);
    min-height: var(--md-target);
    padding: 0 var(--md-space-3);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
    cursor: pointer;
    touch-action: manipulation;
  }

  .toggle:disabled {
    border-style: dashed;
    color: var(--md-color-ink-muted);
    cursor: not-allowed;
  }

  .track {
    position: relative;
    width: 28px;
    height: 16px;
    border: var(--md-border-control) solid currentcolor;
    border-radius: var(--md-radius-1);
  }

  .knob {
    position: absolute;
    inset-block: 2px;
    inset-inline-start: 2px;
    width: 10px;
    background: currentcolor;
  }

  .toggle[aria-checked='true'] .knob {
    inset-inline: auto 2px;
  }

  .note {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }
</style>
