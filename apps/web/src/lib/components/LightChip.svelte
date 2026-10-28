<!--
  The mower's camera light as a glass chip over a camera picture (the drive view, ADR 0044): the
  lamp, the word for the state the mower reports and, unless `compact`, the name. One tap asks
  for the opposite of the reported state; the chip never shows an optimistic state. While a
  `set_light` command is on its way it waits (dotted, `aria-busy`); without the right to control
  the mower or while it is offline it is dashed and says why (title and description). The rules
  are the tile's (`LightToggle`), from `lib/mower/light.ts`; the caller shows the chip only when
  the mower has the `light` capability.
-->
<script lang="ts">
  import { lightReasonText } from '#lib/i18n.js';
  import { lightView, type LightInput } from '#lib/mower/light.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props extends LightInput {
    onchange: (on: boolean) => void;
    /** Lamp and state word only (phones upright, where the picture is small). */
    compact?: boolean;
    testid?: string;
  }
  let {
    on,
    capable,
    online,
    allowed,
    pending,
    onchange,
    compact = false,
    testid = 'light-chip',
  }: Props = $props();

  const uid = $props.id();
  const view = $derived(lightView({ on, capable, online, allowed, pending }));
  const note = $derived(
    pending ? m.light_pending() : view.reason ? lightReasonText(view.reason) : null,
  );
  const word = $derived(on === true ? m.light_on() : on === false ? m.light_off() : '—');
</script>

<button
  type="button"
  role="switch"
  class="chip"
  class:compact
  class:pending
  aria-checked={on === true}
  aria-label={m.light_title()}
  aria-describedby={note ? `${uid}-note` : undefined}
  aria-busy={pending}
  title={note ?? (view.next ? m.light_switch_on() : m.light_switch_off())}
  disabled={view.disabled}
  data-testid={testid}
  data-state={view.state}
  data-pending={pending ? 'true' : 'false'}
  onclick={() => {
    if (!view.disabled) onchange(view.next);
  }}
>
  <span class="lamp" aria-hidden="true"></span>
  <span class="name">{m.light_chip()}</span>
  <span class="word">{word}</span>
  {#if on === null}<span class="sr">{m.light_unknown()}</span>{/if}
</button>
{#if note}<span class="sr" id="{uid}-note">{note}</span>{/if}

<style>
  .chip {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-2);
    min-height: var(--md-target);
    padding: 0 var(--md-space-3) 0 var(--md-space-2);
    border: var(--md-hairline) solid var(--md-hud-line);
    border-radius: var(--md-radius-2);
    background: var(--md-hud-glass);
    color: var(--md-hud-ink);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    cursor: pointer;
    touch-action: manipulation;
  }

  .chip:focus-visible {
    outline: 3px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .chip.compact {
    min-height: 36px;
    padding: 0 var(--md-space-2);
  }

  .compact .name {
    display: none;
  }

  .name {
    color: var(--md-hud-ink-muted);
  }

  .word {
    min-width: 3ch;
    font-weight: var(--md-weight-semibold);
  }

  /* The lamp: hollow when off, dashed when unknown, filled with a halo when on (as the tile). */
  .lamp {
    width: 12px;
    height: 12px;
    flex: none;
    border: var(--md-border-control) solid currentcolor;
    border-radius: var(--md-radius-1);
  }

  [data-state='unknown'] .lamp {
    border-style: dashed;
  }

  [data-state='on'] {
    border-color: var(--md-hud-lit);
  }

  [data-state='on'] .lamp {
    border-color: var(--md-hud-lit);
    background: var(--md-hud-lit);
    box-shadow: 0 0 0 3px var(--md-state-paused-bg);
  }

  [data-state='on'] .word {
    color: var(--md-hud-lit);
  }

  .pending .word {
    color: var(--md-hud-ink-muted);
    text-decoration: underline dotted;
    text-underline-offset: 3px;
  }

  .chip:disabled {
    border-style: dashed;
    color: var(--md-hud-ink-muted);
    cursor: not-allowed;
  }

  .chip.pending:disabled {
    cursor: progress;
  }

  .sr {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
</style>
