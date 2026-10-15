<!--
  A non-modal bottom sheet with three snap points for phones, over a map that stays visible and
  tappable above it: `peek` (the peek snippet: a summary and the primary action), `half` (45 % of
  the space above the sheet's bottom edge) and `full` (90 %, never more than the space minus
  56 px; the body scrolls inside). The handle is a button: drag it (pointer events; a fling goes
  on to the next snap, a slow release to the nearest), or tap / Enter / Space to cycle the snap
  points. Escape returns to peek. It stops above the command bar (`--md-sheet-bottom`), adds the
  bottom safe-area inset when it sits on the screen edge (`safeArea`), and reports the height it
  covers (`visibleHeight`, the settled snap height) so a map can keep its content above it. The
  snap logic lives in `bottom-sheet.ts`.
-->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import {
    TAP_SLOP_PX,
    allowedSnaps,
    clampHeight,
    coerceSnap,
    nextSnap,
    releaseSnap,
    releaseVelocity,
    snapHeights,
    type PointerSample,
    type SnapPoint,
  } from './bottom-sheet';

  interface Props {
    open: boolean;
    /** The current snap point (bindable). */
    snap?: SnapPoint;
    /** The snap points this sheet offers (peek always); default all three. */
    snaps?: readonly SnapPoint[];
    /** The accessible name of the sheet. */
    label: string;
    /** The accessible name of the handle button (it says expanded or not on its own). */
    handleLabel: string;
    /** The height the sheet covers in px, safe-area inset included (bindable, read only). */
    visibleHeight?: number;
    /** Add `env(safe-area-inset-bottom)`; off when the sheet sits above a bar that has it. */
    safeArea?: boolean;
    /** Always visible: a summary and the primary action. */
    peek: Snippet;
    /** The rest, shown from half on; it scrolls inside the sheet. */
    children?: Snippet;
    onsnapchange?: (snap: SnapPoint) => void;
    testid?: string;
  }
  let {
    open,
    snap = $bindable('peek'),
    snaps,
    label,
    handleLabel,
    visibleHeight = $bindable(0),
    safeArea = true,
    peek,
    children,
    onsnapchange,
    testid,
  }: Props = $props();

  const uid = $props.id();
  const allowed = $derived(allowedSnaps(snaps));
  const current = $derived(coerceSnap(snap, allowed));

  let el: HTMLElement | undefined = $state();
  let headHeight = $state(0);
  let available = $state(0);
  let safePx = $state(0);
  /** The height while a finger drags the handle, else null. */
  let dragHeight = $state<number | null>(null);

  const heights = $derived(snapHeights(available, headHeight));
  const height = $derived(dragHeight ?? heights[current]);

  let drag: { y: number; height: number; moved: boolean; samples: PointerSample[] } | null = null;
  let suppressClick = false;

  function measure(): void {
    if (!el) return;
    available = el.getBoundingClientRect().bottom;
    safePx = parseFloat(getComputedStyle(el).paddingBottom) || 0;
  }

  $effect(() => {
    if (!open || !el) return;
    measure();
    window.addEventListener('resize', measure);
    window.visualViewport?.addEventListener('resize', measure);
    return () => {
      window.removeEventListener('resize', measure);
      window.visualViewport?.removeEventListener('resize', measure);
    };
  });

  // The settled height, not every frame of a drag or a transition: a map pads by it.
  $effect(() => {
    visibleHeight = open && available > 0 ? heights[current] + safePx : 0;
  });

  function setSnap(next: SnapPoint): void {
    if (next === snap) return;
    snap = next;
    onsnapchange?.(next);
  }

  function pointerdown(event: PointerEvent): void {
    suppressClick = false;
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    measure();
    drag = {
      y: event.clientY,
      height: heights[current],
      moved: false,
      samples: [{ t: event.timeStamp, y: event.clientY }],
    };
    (event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId);
  }

  function pointermove(event: PointerEvent): void {
    if (!drag) return;
    const dy = drag.y - event.clientY;
    if (!drag.moved && Math.abs(dy) < TAP_SLOP_PX) return;
    drag.moved = true;
    drag.samples.push({ t: event.timeStamp, y: event.clientY });
    if (drag.samples.length > 12) drag.samples.shift();
    dragHeight = clampHeight(drag.height + dy, heights, allowed);
  }

  function pointerup(event: PointerEvent): void {
    if (!drag) return;
    const ended = drag;
    drag = null;
    if (ended.moved) {
      ended.samples.push({ t: event.timeStamp, y: event.clientY });
      suppressClick = true;
      const at = dragHeight ?? ended.height;
      setSnap(releaseSnap(at, releaseVelocity(ended.samples), heights, allowed));
    }
    dragHeight = null;
  }

  function pointercancel(): void {
    drag = null;
    dragHeight = null;
  }

  function click(): void {
    if (suppressClick) {
      suppressClick = false;
      return;
    }
    setSnap(nextSnap(current, allowed));
  }

  function keydown(event: KeyboardEvent): void {
    if (open && event.key === 'Escape' && current !== 'peek') setSnap('peek');
  }

  const level = $derived(allowed.indexOf(current));
</script>

<svelte:window onkeydown={keydown} />

{#if open}
  <section
    class="md-bottom-sheet"
    class:dragging={dragHeight !== null}
    class:edge={safeArea}
    aria-label={label}
    data-testid={testid}
    data-snap={current}
    data-visible-height={Math.round(visibleHeight)}
    style:height="{Math.max(0, height)}px"
    bind:this={el}
  >
    <div class="head" bind:clientHeight={headHeight}>
      <button
        type="button"
        class="handle"
        aria-label={handleLabel}
        aria-expanded={current !== 'peek'}
        aria-controls={children ? `${uid}-body` : undefined}
        data-testid={testid ? `${testid}-handle` : undefined}
        onpointerdown={pointerdown}
        onpointermove={pointermove}
        onpointerup={pointerup}
        onpointercancel={pointercancel}
        onclick={click}
      >
        <span class="level" aria-hidden="true">
          {#each allowed as s, i (s)}
            <span class="pip" class:on={i <= level}></span>
          {/each}
        </span>
      </button>
      <div class="peek">{@render peek()}</div>
    </div>
    {#if children}
      <div
        class="body"
        id="{uid}-body"
        hidden={current === 'peek' && dragHeight === null}
        data-testid={testid ? `${testid}-body` : undefined}
      >
        {@render children()}
      </div>
    {/if}
  </section>
{/if}

<style>
  .md-bottom-sheet {
    position: fixed;
    inset-inline: 0;
    bottom: var(--md-sheet-bottom, 0);
    z-index: 25;
    display: flex;
    flex-direction: column;
    box-sizing: content-box;
    max-width: 720px;
    margin-inline: auto;
    padding-bottom: 0;
    border-top: var(--md-border-control) solid var(--md-color-ink);
    background: var(--md-color-surface);
    box-shadow: var(--md-shadow-float);
    overflow: hidden;
    transition: height var(--md-motion-base) var(--md-ease);
    animation: rise var(--md-motion-base) var(--md-ease);
  }

  .md-bottom-sheet.edge {
    padding-bottom: env(safe-area-inset-bottom, 0);
  }

  .md-bottom-sheet.dragging {
    transition: none;
  }

  .head {
    flex: none;
  }

  /* The handle: a full-width strip with a three-step level meter (peek, half, full). */
  .handle {
    display: grid;
    place-items: center;
    width: 100%;
    height: 32px;
    padding: 0;
    border: 0;
    background: none;
    color: var(--md-color-ink);
    cursor: grab;
    touch-action: none;
    user-select: none;
  }

  .handle:active {
    cursor: grabbing;
  }

  .handle:focus-visible {
    outline-offset: -4px;
  }

  .level {
    display: flex;
    gap: 3px;
  }

  .pip {
    width: 14px;
    height: 4px;
    background: var(--md-color-line-strong);
  }

  .pip.on {
    background: var(--md-color-ink);
  }

  .peek {
    padding: 0 var(--md-gutter) var(--md-space-3);
  }

  .body {
    flex: 1 1 auto;
    min-height: 0;
    padding: var(--md-space-2) var(--md-gutter) var(--md-space-4);
    border-top: var(--md-hairline) solid var(--md-color-line);
    overflow-y: auto;
    overscroll-behavior: contain;
  }

  .body[hidden] {
    display: none;
  }

  @keyframes rise {
    from {
      transform: translateY(24px);
      opacity: 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .md-bottom-sheet {
      transition: none;
      animation: none;
    }
  }
</style>
