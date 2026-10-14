<!--
  The manual drive pad: a round, high-contrast pad with a 72 px thumb. Hold it with a finger or
  the mouse (pointer capture keeps the drag outside the pad) or with the arrow keys when focused.
  While held it reports `{linear, angular}` in -1..1 (up is forward, left turns left, 10 % radial
  dead zone) through `onsample` at a fixed cadence (`interval`, the first sample at once); letting
  go, Escape, blur or `disabled` end the hold (`onengage(false)`). The pad never sends anything
  itself: the page turns samples into drive frames.
-->
<script lang="ts">
  import { onDestroy } from 'svelte';

  import {
    ARROW_KEYS,
    DEFAULT_DEAD_ZONE,
    STICK_ZERO,
    clampToPad,
    keysVector,
    stickVector,
    type StickVector,
  } from './stick.js';

  interface Props {
    /** Accessible name of the pad. */
    label: string;
    /** Usage hint (pointer and arrow keys), announced as the description. */
    hint?: string;
    disabled?: boolean;
    /** Radial dead zone as a fraction of the radius. */
    deadZone?: number;
    /** Milliseconds between samples while the pad is held. */
    interval?: number;
    /** The current vector (bindable, read only for the parent). */
    value?: StickVector;
    onsample?: (vector: StickVector) => void;
    onengage?: (engaged: boolean) => void;
    testid?: string;
  }
  let {
    label,
    hint,
    disabled = false,
    deadZone = DEFAULT_DEAD_ZONE,
    interval = 100,
    value = $bindable({ ...STICK_ZERO }),
    onsample,
    onengage,
    testid = 'joystick',
  }: Props = $props();

  const uid = $props.id();
  let pad: HTMLDivElement | undefined = $state();
  let engaged = $state(false);
  let thumb = $state({ x: 0, y: 0 });
  let pointerId: number | null = null;
  const held = new Set<string>();
  let timer: ReturnType<typeof setInterval> | undefined;

  function emit(): void {
    onsample?.(value);
  }

  function engage(): void {
    if (engaged) return;
    engaged = true;
    onengage?.(true);
    emit();
    timer = setInterval(emit, Math.max(20, interval));
  }

  function release(): void {
    clearInterval(timer);
    timer = undefined;
    held.clear();
    pointerId = null;
    thumb = { x: 0, y: 0 };
    value = { ...STICK_ZERO };
    if (!engaged) return;
    engaged = false;
    onengage?.(false);
  }

  function track(event: PointerEvent): void {
    if (!pad) return;
    const box = pad.getBoundingClientRect();
    const radius = box.width / 2;
    const dx = event.clientX - (box.left + radius);
    const dy = event.clientY - (box.top + box.height / 2);
    thumb = clampToPad(dx, dy, radius);
    value = stickVector(dx, dy, radius, deadZone);
  }

  function onpointerdown(event: PointerEvent): void {
    if (disabled || pointerId !== null) return;
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    event.preventDefault();
    pointerId = event.pointerId;
    try {
      pad?.setPointerCapture(event.pointerId);
    } catch {
      // Synthetic events without an active pointer: track without capture.
    }
    pad?.focus({ preventScroll: true });
    track(event);
    engage();
  }

  function onpointermove(event: PointerEvent): void {
    if (event.pointerId !== pointerId) return;
    track(event);
  }

  function onpointerend(event: PointerEvent): void {
    if (event.pointerId !== pointerId) return;
    release();
  }

  function showKeys(): void {
    value = keysVector(held);
    thumb = { x: -value.angular, y: -value.linear };
  }

  function onkeydown(event: KeyboardEvent): void {
    if (disabled) return;
    if (event.key === 'Escape') {
      release();
      return;
    }
    if (!(ARROW_KEYS as readonly string[]).includes(event.key)) return;
    event.preventDefault();
    if (pointerId !== null) return;
    held.add(event.key);
    showKeys();
    engage();
  }

  function onkeyup(event: KeyboardEvent): void {
    if (!held.has(event.key)) return;
    held.delete(event.key);
    if (held.size === 0) release();
    else showKeys();
  }

  $effect(() => {
    if (disabled) release();
  });

  onDestroy(() => {
    clearInterval(timer);
  });
</script>

<!-- A two-axis control has no ARIA widget role: `application` hands pointer and arrow keys to the
     pad, which Svelte's role table counts as non-interactive. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div
  bind:this={pad}
  class="md-joystick"
  class:engaged
  class:disabled
  role="application"
  aria-roledescription="joystick"
  aria-label={label}
  aria-describedby={hint ? `${uid}-hint` : undefined}
  aria-disabled={disabled}
  tabindex={disabled ? -1 : 0}
  data-testid={testid}
  data-engaged={engaged}
  data-linear={value.linear}
  data-angular={value.angular}
  {onpointerdown}
  {onpointermove}
  onpointerup={onpointerend}
  onpointercancel={onpointerend}
  onlostpointercapture={onpointerend}
  {onkeydown}
  {onkeyup}
  onblur={release}
  oncontextmenu={(event) => event.preventDefault()}
>
  <svg class="face" viewBox="-100 -100 200 200" aria-hidden="true">
    <circle class="ring" r="66" />
    <circle class="ring" r="33" />
    <circle class="dead" r={deadZone * 100} />
    <line class="axis" x1="-96" y1="0" x2="96" y2="0" />
    <line class="axis" x1="0" y1="-96" x2="0" y2="96" />
    <path class="mark" d="M0 -92 L-7 -80 L7 -80 Z" />
    <path class="mark" d="M0 92 L-7 80 L7 80 Z" />
    <path class="mark" d="M-92 0 L-80 -7 L-80 7 Z" />
    <path class="mark" d="M92 0 L80 -7 L80 7 Z" />
    {#if engaged}
      <line class="vector" x1="0" y1="0" x2={thumb.x * 100} y2={thumb.y * 100} />
    {/if}
  </svg>
  <span class="thumb" style:--md-stick-x={thumb.x} style:--md-stick-y={thumb.y} aria-hidden="true"
  ></span>
</div>
{#if hint}<p class="md-joystick-hint" id="{uid}-hint">{hint}</p>{/if}

<style>
  .md-joystick {
    --md-stick-size: min(78vw, 300px);
    --md-stick-thumb: 72px;

    position: relative;
    width: var(--md-stick-size);
    aspect-ratio: 1;
    margin-inline: auto;
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-round);
    background: var(--md-color-surface-sunken);
    touch-action: none;
    user-select: none;
    -webkit-touch-callout: none;
    cursor: grab;
  }

  .md-joystick.engaged {
    border-color: var(--md-color-ink);
    cursor: grabbing;
  }

  .md-joystick:focus-visible {
    outline: 3px solid var(--md-color-focus);
    outline-offset: 4px;
  }

  .face {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    overflow: visible;
  }

  .ring {
    fill: none;
    stroke: var(--md-color-line-strong);
    stroke-width: 1;
    stroke-dasharray: 3 4;
  }

  .dead {
    fill: var(--md-color-line);
    stroke: var(--md-color-line-strong);
    stroke-width: 1;
  }

  .axis {
    stroke: var(--md-color-line);
    stroke-width: 1;
  }

  .mark {
    fill: var(--md-color-ink-muted);
  }

  .engaged .mark {
    fill: var(--md-color-ink);
  }

  .vector {
    stroke: var(--md-color-ink);
    stroke-width: 3;
    stroke-linecap: square;
  }

  .thumb {
    position: absolute;
    top: 50%;
    left: 50%;
    width: var(--md-stick-thumb);
    height: var(--md-stick-thumb);
    border: 4px solid var(--md-color-surface);
    border-radius: var(--md-radius-round);
    background: var(--md-color-ink);
    box-shadow:
      0 0 0 var(--md-border-control) var(--md-color-ink),
      var(--md-shadow-float);
    transform: translate(
      calc(-50% + var(--md-stick-x) * (var(--md-stick-size) / 2 - var(--md-stick-thumb) / 2)),
      calc(-50% + var(--md-stick-y) * (var(--md-stick-size) / 2 - var(--md-stick-thumb) / 2))
    );
    transition: transform var(--md-motion-fast) var(--md-ease);
    pointer-events: none;
  }

  .engaged .thumb {
    transition: none;
  }

  .disabled {
    border-style: dashed;
    cursor: not-allowed;
  }

  .disabled .thumb {
    border-color: var(--md-color-surface-sunken);
    background: var(--md-color-ink-muted);
    box-shadow: none;
  }

  .md-joystick-hint {
    margin: var(--md-space-2) 0 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
    text-align: center;
  }

  @media (prefers-reduced-motion: reduce) {
    .thumb {
      transition: none;
    }
  }
</style>
