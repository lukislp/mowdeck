<!--
  A setpoint control (blade height, speed): minus, the value as a monospaced readout with its
  scale, plus. The value is committed 600 ms after the last step, never per tap (with `immediate`
  at every step, for a draft that only a later action sends); until the mower confirms it the
  readout stays in the pending style. The control shrinks with its container (no clipped plus).
-->
<script lang="ts">
  interface Props {
    id: string;
    label: string;
    /** The confirmed value from the mower, or null when unknown. */
    value: number | null;
    min: number;
    max: number;
    step: number;
    unit: string;
    decimals?: number;
    /** Format a number for display (locale aware); default fixed decimals. */
    format?: (value: number) => string;
    decreaseLabel: string;
    increaseLabel: string;
    disabled?: boolean;
    /** A commanded value is on its way (shown as pending until confirmed). */
    pending?: boolean;
    commitDelayMs?: number;
    /** Commit at every step instead of after the delay (a draft, not a command). */
    immediate?: boolean;
    oncommit: (value: number) => void;
  }
  let {
    id,
    label,
    value,
    min,
    max,
    step,
    unit,
    decimals = 0,
    format,
    decreaseLabel,
    increaseLabel,
    disabled = false,
    pending = false,
    commitDelayMs = 600,
    immediate = false,
    oncommit,
  }: Props = $props();

  let draft = $state<number | null>(null);
  let timer: ReturnType<typeof setTimeout> | undefined;

  const shown = $derived(draft ?? value);
  const text = $derived(shown === null ? '--' : format ? format(shown) : shown.toFixed(decimals));
  const position = $derived(
    shown === null ? 0 : Math.max(0, Math.min(1, (shown - min) / (max - min || 1))),
  );

  function clamp(next: number): number {
    const rounded = Math.round(next / step) * step;
    return Number(Math.max(min, Math.min(max, rounded)).toFixed(Math.max(decimals, 3)));
  }

  function nudge(direction: 1 | -1): void {
    const base = draft ?? value ?? min;
    draft = clamp(base + direction * step);
    clearTimeout(timer);
    if (immediate) commit();
    else timer = setTimeout(commit, commitDelayMs);
  }

  function commit(): void {
    if (draft === null) return;
    const next = draft;
    draft = null;
    if (next !== value) oncommit(next);
  }

  $effect(() => () => clearTimeout(timer));
</script>

<div class="md-stepper" class:pending={pending || draft !== null}>
  <span class="label" id="{id}-label">{label}</span>
  <div class="row">
    <button
      type="button"
      class="nudge"
      aria-label={decreaseLabel}
      disabled={disabled || shown === null || shown <= min}
      onclick={() => nudge(-1)}>−</button
    >
    <output
      class="value"
      {id}
      aria-labelledby="{id}-label"
      aria-live="polite"
      data-value={shown ?? ''}
      data-testid="{id}-value"
    >
      <span class="number">{text}</span><span class="unit">{unit}</span>
    </output>
    <button
      type="button"
      class="nudge"
      aria-label={increaseLabel}
      disabled={disabled || (shown !== null && shown >= max)}
      onclick={() => nudge(1)}>+</button
    >
  </div>
  <div class="scale" aria-hidden="true">
    <span class="fill" style:inline-size="{position * 100}%"></span>
  </div>
  <div class="range" aria-hidden="true">
    <span>{format ? format(min) : min.toFixed(decimals)}</span>
    <span>{format ? format(max) : max.toFixed(decimals)}</span>
  </div>
</div>

<style>
  .md-stepper {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    min-inline-size: 0;
    padding: var(--md-space-2) var(--md-space-3) var(--md-space-2);
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
    grid-template-columns: var(--md-target) minmax(0, 1fr) var(--md-target);
    align-items: center;
    gap: var(--md-space-2);
  }

  .nudge {
    width: var(--md-target);
    height: var(--md-target);
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-24);
    line-height: 1;
    cursor: pointer;
    touch-action: manipulation;
  }

  .nudge:disabled {
    border-style: dashed;
    color: var(--md-color-ink-muted);
    cursor: not-allowed;
  }

  .value {
    display: flex;
    align-items: baseline;
    justify-content: center;
    gap: 0.25em;
    min-inline-size: 0;
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-24);
    line-height: var(--md-line-32);
    font-weight: var(--md-weight-medium);
    font-variant-numeric: tabular-nums slashed-zero;
  }

  .unit {
    font-size: 0.6em;
    font-weight: var(--md-weight-regular);
    color: var(--md-color-ink-muted);
  }

  .pending .number {
    color: var(--md-color-ink-muted);
    text-decoration: underline dotted;
    text-underline-offset: 4px;
  }

  .scale {
    position: relative;
    height: 4px;
    background: var(--md-color-line);
  }

  .fill {
    position: absolute;
    inset-block: 0;
    inset-inline-start: 0;
    background: var(--md-color-ink);
  }

  .range {
    display: flex;
    justify-content: space-between;
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-11);
    line-height: var(--md-line-16);
  }
</style>
