<!--
  The guided tour (ADR 0073) on top of the app: a viewfinder frame around the element a step
  points at (the rest of the page dimmed and not clickable, so nothing is triggered by accident,
  in the demo neither), and a coach card with the step's text, its position and Back, Next and
  "Leave the tour".

  Fixed layers only: the page underneath never moves (no layout shift). A modal dialog with a
  focus trap; Esc leaves the tour, ← and → step through it; every step change is announced.
  On phones the card docks to the top or bottom edge, away from the target; on wide screens it
  sits beside the target. Motion is a short glide of the frame, off with prefers-reduced-motion.
-->
<script lang="ts">
  import { Button } from '@mowdeck/ui';
  import { tick } from 'svelte';

  import { goto } from '$app/navigation';

  import { m } from '#lib/paraglide/messages.js';

  import { leadsToAddMower, tourText } from './labels';
  import { tour as appTour } from './runtime';
  import type { Tour } from './tour.svelte';

  /** The app's tour; tests pass their own. */
  let { tour = appTour }: { tour?: Tour } = $props();

  /** Room around the target inside the frame, and between frame and card. */
  const PAD = 6;
  const GAP = 12;
  const EDGE = 16;
  /** Below this width the card docks to an edge (the app's phone layout). */
  const PHONE = 768;

  interface Box {
    top: number;
    left: number;
    width: number;
    height: number;
  }

  let card = $state<HTMLElement | null>(null);
  let cardHeight = $state(0);
  let cardWidth = $state(0);
  let innerWidth = $state(0);
  let innerHeight = $state(0);
  let box = $state<Box | null>(null);
  let returnFocus: HTMLElement | null = null;

  const titleId = $props.id();
  const step = $derived(tour.step);
  const context = $derived(tour.context);
  const text = $derived(step && context ? tourText(step.id, context) : null);
  const progress = $derived(tour.progress);
  const phone = $derived(innerWidth > 0 && innerWidth < PHONE);
  const addMower = $derived(step && context ? leadsToAddMower(step.id, context) : false);
  const ticks = $derived(Array.from({ length: progress.total }, (_, i) => i));
  const counter = $derived(
    `${String(progress.current).padStart(2, '0')} / ${String(progress.total).padStart(2, '0')}`,
  );
  const announcement = $derived(
    text && progress.current > 0
      ? m.tour_announce({ current: progress.current, total: progress.total, title: text.title })
      : '',
  );

  function reducedMotion(): boolean {
    return globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
  }

  /** The target's box, clipped to the viewport and padded (null: nothing to frame). */
  function frameOf(element: HTMLElement): Box | null {
    const rect = element.getBoundingClientRect();
    const top = Math.max(rect.top - PAD, 0);
    const left = Math.max(rect.left - PAD, 0);
    const bottom = Math.min(rect.bottom + PAD, innerHeight);
    const right = Math.min(rect.right + PAD, innerWidth);
    if (bottom <= top || right <= left) return null;
    return { top, left, width: right - left, height: bottom - top };
  }

  /** A target that fits the screen but is not fully on it (content above it loaded later and
   * pushed it away), or a tall one that left it completely. */
  function drifted(element: HTMLElement): boolean {
    const rect = element.getBoundingClientRect();
    if (rect.height < innerHeight) return rect.top < 0 || rect.bottom > innerHeight;
    return rect.bottom < 0 || rect.top > innerHeight;
  }

  // Follow the target while it is shown: scroll it into view, then track its box every frame
  // (the page may still be settling, a map may resize) and bring it back when the page pushed it
  // away (the page cannot be scrolled under the tour's layer). A target that leaves the
  // document is looked for again.
  $effect(() => {
    const element = tour.target;
    if (!tour.running || !element) {
      box = null;
      return;
    }
    const reveal = (behavior: ScrollBehavior): void =>
      element.scrollIntoView?.({ block: 'center', inline: 'nearest', behavior });
    reveal(reducedMotion() ? 'auto' : 'smooth');
    let revealed = performance.now();
    let frame = 0;
    const follow = (): void => {
      if (!element.isConnected) {
        box = null;
        void tour.retarget();
        return;
      }
      const now = performance.now();
      if (now - revealed > 600 && drifted(element)) {
        reveal('auto');
        revealed = now;
      }
      const next = frameOf(element);
      if (
        next?.top !== box?.top ||
        next?.left !== box?.left ||
        next?.width !== box?.width ||
        next?.height !== box?.height
      ) {
        box = next;
      }
      frame = requestAnimationFrame(follow);
    };
    follow();
    return () => cancelAnimationFrame(frame);
  });

  /** Where the card goes: docked on phones, beside the frame on wide screens. */
  const placement = $derived.by(
    (): { dock: 'top' | 'bottom' | null; top: number; left: number } => {
      if (phone) {
        const centre = box ? box.top + box.height / 2 : 0;
        return { dock: box && centre > innerHeight / 2 ? 'top' : 'bottom', top: 0, left: 0 };
      }
      const width = cardWidth || 380;
      const height = cardHeight || 240;
      if (!box) {
        return {
          dock: null,
          top: Math.max((innerHeight - height) / 2, EDGE),
          left: Math.max((innerWidth - width) / 2, EDGE),
        };
      }
      const left = Math.min(Math.max(box.left, EDGE), Math.max(innerWidth - width - EDGE, EDGE));
      if (box.top + box.height + GAP + height <= innerHeight - EDGE) {
        return { dock: null, top: box.top + box.height + GAP, left };
      }
      if (box.top - GAP - height >= EDGE) {
        return { dock: null, top: box.top - GAP - height, left };
      }
      // A target as tall as the screen (the map): beside it if there is room, else over its
      // lower right corner.
      if (box.left + box.width + GAP + width <= innerWidth - EDGE) {
        return {
          dock: null,
          top: Math.max(innerHeight - height - EDGE, EDGE),
          left: box.left + box.width + GAP,
        };
      }
      if (box.left - GAP - width >= EDGE) {
        return {
          dock: null,
          top: Math.max(innerHeight - height - EDGE, EDGE),
          left: box.left - GAP - width,
        };
      }
      return {
        dock: null,
        top: Math.max(innerHeight - height - EDGE, EDGE),
        left: Math.max(innerWidth - width - EDGE, EDGE),
      };
    },
  );

  function focusables(): HTMLElement[] {
    if (!card) return [];
    return [
      ...card.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], [tabindex="0"]'),
    ];
  }

  function focusPrimary(): void {
    card?.querySelector<HTMLElement>('[data-tour-primary] button')?.focus();
  }

  // Opening: remember where the focus was and move it into the card. Every step: keep it inside.
  $effect(() => {
    if (!tour.running) return;
    void step;
    void tick().then(() => {
      if (!card) return;
      if (!card.contains(document.activeElement)) {
        if (!returnFocus && document.activeElement instanceof HTMLElement) {
          returnFocus = document.activeElement;
        }
        focusPrimary();
      }
    });
  });

  // Closing: the focus goes back where it was (or stays on the page the tour ended on).
  $effect(() => {
    if (tour.running) return;
    const back = returnFocus;
    returnFocus = null;
    if (back?.isConnected) back.focus();
  });

  function keydown(event: KeyboardEvent): void {
    if (!tour.running) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      tour.skip();
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      void primary();
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      void tour.back();
    } else if (event.key === 'Tab') {
      const items = focusables();
      if (items.length === 0) return;
      const first = items[0]!;
      const last = items.at(-1)!;
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !card?.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !card?.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    }
  }

  function focusin(event: FocusEvent): void {
    if (!tour.running || !card) return;
    if (event.target instanceof Node && !card.contains(event.target)) focusPrimary();
  }

  async function primary(): Promise<void> {
    if (!step || !context) return;
    if (addMower) {
      tour.finish();
      await goto('/mowers/new');
    } else if (tour.last) {
      tour.finish();
    } else {
      await tour.next();
    }
  }
</script>

<svelte:window
  bind:innerWidth
  bind:innerHeight
  onkeydowncapture={keydown}
  onfocusincapture={focusin}
/>

<p class="live" aria-live="polite" data-testid="tour-live">
  {tour.running ? announcement : ''}
</p>

{#if tour.running && step && text}
  <div class="tour" data-testid="tour" data-step={step.id}>
    <!-- Catches every click on the page while the tour runs; dimmed itself when nothing is framed. -->
    <div class="catch" class:dim={!box} aria-hidden="true"></div>
    {#if box}
      <div
        class="frame"
        style:top="{box.top}px"
        style:left="{box.left}px"
        style:width="{box.width}px"
        style:height="{box.height}px"
        aria-hidden="true"
        data-testid="tour-frame"
      >
        <span class="mark tl"></span>
        <span class="mark tr"></span>
        <span class="mark bl"></span>
        <span class="mark br"></span>
        <span class="tag mono">{String(progress.current).padStart(2, '0')}</span>
      </div>
    {/if}
    <div
      bind:this={card}
      bind:offsetHeight={cardHeight}
      bind:offsetWidth={cardWidth}
      class="card"
      class:docked={placement.dock !== null}
      class:dock-top={placement.dock === 'top'}
      class:dock-bottom={placement.dock === 'bottom'}
      style:top={placement.dock ? null : `${placement.top}px`}
      style:left={placement.dock ? null : `${placement.left}px`}
      role="dialog"
      aria-modal="true"
      aria-labelledby="{titleId}-title"
      aria-describedby="{titleId}-body"
      data-testid="tour-card"
    >
      <div class="head">
        <span class="label">{m.tour_label()}</span>
        <span class="count mono" data-testid="tour-progress">
          <span class="md-visually-hidden"
            >{m.tour_progress({ current: progress.current, total: progress.total })}</span
          >
          <span aria-hidden="true">{counter}</span>
        </span>
      </div>
      <ol class="scale" aria-hidden="true">
        {#each ticks as i (i)}
          <li class:past={i < progress.current - 1} class:now={i === progress.current - 1}></li>
        {/each}
      </ol>
      <h2 class="title" id="{titleId}-title" data-testid="tour-title">{text.title}</h2>
      <div id="{titleId}-body" class="body">
        <p>{text.body}</p>
        {#if text.demo}
          <p class="demo" data-testid="tour-demo-note">
            <span class="tape" aria-hidden="true"></span>
            <span><strong>{m.tour_demo_label()}</strong> {text.demo}</span>
          </p>
        {/if}
        {#if tour.seeking && step.target}
          <p class="seeking" role="status">{m.tour_seeking()}</p>
        {/if}
      </div>
      <div class="actions">
        <button type="button" class="leave" onclick={() => tour.skip()} data-testid="tour-skip"
          >{m.tour_skip()}</button
        >
        <span class="moves">
          {#if !tour.first}
            <Button onclick={() => void tour.back()} testid="tour-back">{m.tour_back()}</Button>
          {/if}
          <span class="primary" data-tour-primary>
            <Button
              variant="primary"
              icon={addMower ? 'plus' : tour.last ? 'check' : undefined}
              onclick={() => void primary()}
              testid="tour-next"
              >{addMower ? m.mowers_add() : tour.last ? m.tour_done() : m.tour_next()}</Button
            >
          </span>
        </span>
      </div>
      <p class="keys mono" aria-hidden="true">{m.tour_keys()}</p>
    </div>
  </div>
{/if}

<style>
  /* Visually hidden, but fixed: an absolutely placed one outside the shell's positioned main
     would make the document taller than the viewport (one scroller per page). */
  .live {
    position: fixed;
    top: 0;
    left: 0;
    width: 1px;
    height: 1px;
    margin: 0;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  .tour {
    position: fixed;
    inset: 0;
    z-index: 60;
    pointer-events: none;
  }

  .catch {
    position: absolute;
    inset: 0;
    pointer-events: auto;
  }

  .catch.dim {
    background: var(--md-scrim);
  }

  /* The viewfinder: a hole in the dimmed page with corner marks in HUD ink (light on the dim
     in both themes) and the step number as a tag, like a callout on an instrument drawing. */
  .frame {
    position: fixed;
    border-radius: var(--md-radius-2);
    box-shadow: 0 0 0 200vmax var(--md-scrim);
    transition:
      top var(--md-motion-base) var(--md-ease),
      left var(--md-motion-base) var(--md-ease),
      width var(--md-motion-base) var(--md-ease),
      height var(--md-motion-base) var(--md-ease);
  }

  .mark {
    position: absolute;
    width: 16px;
    height: 16px;
    border: 0 solid var(--md-hud-ink);
  }

  .tl {
    top: -4px;
    left: -4px;
    border-top-width: 2px;
    border-left-width: 2px;
  }

  .tr {
    top: -4px;
    right: -4px;
    border-top-width: 2px;
    border-right-width: 2px;
  }

  .bl {
    bottom: -4px;
    left: -4px;
    border-bottom-width: 2px;
    border-left-width: 2px;
  }

  .br {
    right: -4px;
    bottom: -4px;
    border-right-width: 2px;
    border-bottom-width: 2px;
  }

  .tag {
    position: absolute;
    top: -4px;
    left: 18px;
    transform: translateY(-100%);
    padding: 0 var(--md-space-1);
    background: var(--md-hud-glass-strong);
    color: var(--md-hud-ink);
    font-size: var(--md-font-size-11);
    line-height: var(--md-line-16);
    letter-spacing: var(--md-label-tracking);
  }

  .card {
    position: fixed;
    display: grid;
    gap: var(--md-space-2);
    width: min(380px, calc(100vw - 2 * var(--md-space-4)));
    padding: var(--md-space-3) var(--md-space-4) var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-top: var(--md-border-control) solid var(--md-color-ink);
    border-radius: var(--md-radius-3);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    box-shadow: var(--md-shadow-float);
    pointer-events: auto;
    animation: arrive var(--md-motion-base) var(--md-ease);
  }

  .card.docked {
    inset-inline: var(--md-space-2);
    width: auto;
  }

  .card.dock-top {
    top: calc(var(--md-safe-top) + var(--md-space-2));
  }

  .card.dock-bottom {
    bottom: calc(env(safe-area-inset-bottom, 0px) + var(--md-space-2));
  }

  .head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--md-space-3);
  }

  .label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .mono {
    font-family: var(--md-font-mono);
  }

  .count {
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
    letter-spacing: var(--md-label-tracking);
  }

  /* The scale: one tick per step, like the setup tour's meter: past filled, now thick. */
  .scale {
    display: flex;
    gap: var(--md-space-0_5);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .scale li {
    flex: 1;
    height: 2px;
    background: var(--md-color-line);
  }

  .scale li.past {
    background: var(--md-color-ink-muted);
  }

  .scale li.now {
    height: 4px;
    margin-top: -1px;
    background: var(--md-color-ink);
  }

  .title {
    margin-top: var(--md-space-1);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-20);
    line-height: var(--md-line-28);
    letter-spacing: normal;
    text-transform: none;
  }

  .body {
    display: grid;
    gap: var(--md-space-2);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .demo {
    display: flex;
    align-items: stretch;
    gap: var(--md-space-2);
    padding: var(--md-space-2) var(--md-space-2) var(--md-space-2) 0;
    border: var(--md-hairline) dashed var(--md-color-line-strong);
    background: var(--md-color-surface-sunken);
    font-size: var(--md-font-size-13);
  }

  /* The demo band's hatched tape, down the edge of the note. */
  .tape {
    flex: none;
    width: 6px;
    background: repeating-linear-gradient(-55deg, var(--md-hud-lit) 0 4px, transparent 4px 8px);
  }

  .seeking {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-2);
    margin-top: var(--md-space-1);
  }

  .moves {
    display: flex;
    gap: var(--md-space-2);
    margin-inline-start: auto;
  }

  .primary {
    display: contents;
  }

  .leave {
    min-height: var(--md-target);
    padding: 0;
    border: 0;
    background: none;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    text-decoration: underline;
    text-decoration-color: var(--md-color-line-strong);
    text-underline-offset: 3px;
    cursor: pointer;
  }

  .leave:hover {
    color: var(--md-color-ink);
  }

  .keys {
    display: none;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    letter-spacing: var(--md-label-tracking);
  }

  @media (hover: hover) and (pointer: fine) {
    .keys {
      display: block;
    }
  }

  @keyframes arrive {
    from {
      transform: translateY(8px);
      opacity: 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .frame {
      transition: none;
    }

    .card {
      animation: none;
    }
  }
</style>
