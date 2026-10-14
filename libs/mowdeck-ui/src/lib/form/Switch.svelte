<!--
  An on/off setting as a 48 px row: label (and hint) on the left, a square toggle on the right
  that says ON or OFF in words besides its position, so the state never depends on colour.
-->
<script lang="ts">
  interface Props {
    id: string;
    label: string;
    checked?: boolean;
    hint?: string;
    /** The words shown inside the toggle, e.g. { on: 'An', off: 'Aus' }. */
    states: { on: string; off: string };
    disabled?: boolean;
    testid?: string;
    onchange?: (checked: boolean) => void;
  }
  let {
    id,
    label,
    checked = $bindable(false),
    hint,
    states,
    disabled = false,
    testid,
    onchange,
  }: Props = $props();

  function toggle(): void {
    if (disabled) return;
    checked = !checked;
    onchange?.(checked);
  }
</script>

<div class="md-switch" class:disabled>
  <span class="text">
    <label for={id}>{label}</label>
    {#if hint}<span class="hint" id="{id}-hint">{hint}</span>{/if}
  </span>
  <button
    {id}
    type="button"
    role="switch"
    aria-checked={checked}
    aria-describedby={hint ? `${id}-hint` : undefined}
    {disabled}
    data-testid={testid}
    onclick={toggle}
  >
    <span class="track" aria-hidden="true">
      <span class="word">{checked ? states.on : states.off}</span>
      <span class="knob"></span>
    </span>
  </button>
</div>

<style>
  .md-switch {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-4);
    min-height: var(--md-target);
  }

  .text {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-0_5);
    min-width: 0;
  }

  label {
    color: var(--md-color-ink);
    font-size: var(--md-font-size-16);
    line-height: var(--md-line-24);
    cursor: pointer;
  }

  .hint {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  button {
    display: grid;
    place-items: center;
    flex: none;
    min-width: var(--md-target);
    min-height: var(--md-target);
    padding: 0;
    border: 0;
    background: none;
    cursor: pointer;
  }

  .track {
    position: relative;
    display: flex;
    align-items: center;
    width: 64px;
    height: 28px;
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface-sunken);
    transition: background-color var(--md-motion-fast) var(--md-ease);
  }

  .word {
    flex: 1;
    padding-inline: 26px var(--md-space-1);
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-medium);
    letter-spacing: var(--md-label-tracking);
    text-align: center;
    text-transform: uppercase;
  }

  .knob {
    position: absolute;
    inset-block: 3px;
    inset-inline-start: 3px;
    width: 20px;
    border-radius: var(--md-radius-1);
    background: var(--md-color-ink-muted);
    transition: inset-inline-start var(--md-motion-fast) var(--md-ease);
  }

  button[aria-checked='true'] .track {
    border-color: var(--md-color-ink);
    background: var(--md-color-ink);
  }

  button[aria-checked='true'] .word {
    padding-inline: var(--md-space-1) 26px;
    color: var(--md-color-ink-inverse);
  }

  button[aria-checked='true'] .knob {
    inset-inline-start: calc(100% - 23px);
    background: var(--md-color-ink-inverse);
  }

  button:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .disabled label,
  .disabled .track {
    cursor: not-allowed;
  }

  .disabled .track {
    border-style: dashed;
  }

  @media (prefers-reduced-motion: reduce) {
    .track,
    .knob {
      transition: none;
    }
  }
</style>
