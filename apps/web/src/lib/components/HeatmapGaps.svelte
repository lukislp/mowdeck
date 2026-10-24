<!--
  Missed spots of the coverage layer, under the map: largest first with their area in m². A
  row highlights its spot on the map; the selected row offers the follow-ups:

  - "Nachmähen" (re-mow just this spot) opens the spot sheet with the spot's polygon when the
    viewer may drive the mower and it drives manually (`canSpot`); otherwise it stays disabled
    with its reason shown.
  - "Bereich mähen" opens the area start with the area around the spot chosen (when the
    viewer may start and the spot lies inside a mapped area).
-->
<script lang="ts">
  import type { HeatGap } from '@mowdeck/ui';

  import { formatNumber } from '#lib/i18n.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    /** Spots, largest first, with their index in the answer. */
    gaps: readonly { index: number; gap: HeatGap }[];
    /** Index (in the answer) of the highlighted spot. */
    selected: number | null;
    onselect: (index: number | null) => void;
    /** The area around a spot (id and display name), if any. */
    areaOf: (gap: HeatGap) => { id: string; name: string } | null;
    /** The viewer may start a job now. */
    canStart: boolean;
    onmowarea: (areaId: string) => void;
    /** The viewer may drive the mower and it drives manually: spot mowing is offered. */
    canSpot?: boolean;
    /** "Nachmähen": the spot's polygon goes to the spot sheet. */
    onremow?: (gap: HeatGap) => void;
  }

  let {
    gaps,
    selected,
    onselect,
    areaOf,
    canStart,
    onmowarea,
    canSpot = false,
    onremow,
  }: Props = $props();

  const uid = $props.id();
  const total = $derived(gaps.reduce((sum, g) => sum + g.gap.area_m2, 0));
  const area = (value: number) => formatNumber(value, value < 10 ? 1 : 0);
</script>

<section class="gaps" aria-labelledby="{uid}-title" data-testid="heatmap-gaps">
  <header class="head">
    <h3 class="title" id="{uid}-title">{m.heat_gaps_title()}</h3>
    {#if gaps.length}
      <span class="mono sum" data-testid="heatmap-gaps-summary">
        {m.heat_gaps_summary({ count: String(gaps.length), area: area(total) })}
      </span>
    {/if}
  </header>

  {#if !gaps.length}
    <p class="empty" data-testid="heatmap-gaps-empty">{m.heat_gaps_none()}</p>
  {:else}
    <ol class="list">
      {#each gaps as item, rank (item.index)}
        {@const active = selected === item.index}
        {@const around = active ? areaOf(item.gap) : null}
        <li class="item" class:active>
          <button
            type="button"
            class="pick"
            aria-pressed={active}
            aria-label={m.heat_gap_show({ n: String(rank + 1) })}
            data-testid="heatmap-gap-{item.index}"
            onclick={() => onselect(active ? null : item.index)}
          >
            <span class="rank mono">{rank + 1}</span>
            <span class="name">{m.heat_gap_name({ n: String(rank + 1) })}</span>
            <span class="size mono">{m.heat_gap_area({ area: area(item.gap.area_m2) })}</span>
          </button>
          {#if active}
            <div class="actions">
              {#if canSpot && onremow}
                <button
                  type="button"
                  class="act act--go"
                  title={m.heat_gap_remow_hint()}
                  data-testid="heatmap-gap-remow"
                  onclick={() => onremow(item.gap)}>{m.heat_gap_remow()}</button
                >
              {:else}
                <span class="disabled-wrap" title={m.heat_gap_remow_unavailable()}>
                  <button
                    type="button"
                    class="act"
                    disabled
                    aria-describedby="{uid}-why"
                    data-testid="heatmap-gap-remow">{m.heat_gap_remow()}</button
                  >
                </span>
              {/if}
              {#if around && canStart}
                <button
                  type="button"
                  class="act act--go"
                  title={m.heat_gap_mow_area_hint()}
                  data-testid="heatmap-gap-mow-area"
                  onclick={() => onmowarea(around.id)}
                  >{m.heat_gap_mow_area({ name: around.name })}</button
                >
              {/if}
              <button
                type="button"
                class="act act--quiet"
                data-testid="heatmap-gap-clear"
                onclick={() => onselect(null)}>{m.heat_gap_clear()}</button
              >
              {#if !(canSpot && onremow)}
                <p class="why" id="{uid}-why">{m.heat_gap_remow_unavailable()}</p>
              {/if}
            </div>
          {/if}
        </li>
      {/each}
    </ol>
  {/if}
</section>

<style>
  .gaps {
    display: flex;
    flex-direction: column;
    min-height: 0;
    border-top: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  .head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--md-space-2);
    padding: var(--md-space-2) var(--md-space-3);
  }

  .title {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .sum {
    font-size: var(--md-font-size-12);
    font-variant-numeric: tabular-nums;
  }

  .empty {
    margin: 0;
    padding: 0 var(--md-space-3) var(--md-space-3);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .list {
    margin: 0;
    padding: 0;
    overflow-y: auto;
    list-style: none;
  }

  .item {
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .item.active {
    box-shadow: inset 3px 0 0 var(--md-color-focus);
  }

  .pick {
    display: grid;
    grid-template-columns: 28px minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--md-space-2);
    width: 100%;
    min-height: var(--md-target);
    padding: 0 var(--md-space-3);
    border: 0;
    border-radius: var(--md-radius-0);
    background: none;
    color: var(--md-color-ink);
    font: inherit;
    font-size: var(--md-font-size-14);
    text-align: start;
    cursor: pointer;
  }

  .pick:hover {
    background: var(--md-color-surface-sunken);
  }

  .pick:focus-visible,
  .act:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .rank {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .active .rank {
    color: var(--md-color-focus);
  }

  .size {
    font-variant-numeric: tabular-nums;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
    padding: 0 var(--md-space-3) var(--md-space-3)
      calc(28px + var(--md-space-3) + var(--md-space-2));
  }

  .disabled-wrap {
    display: inline-flex;
    cursor: not-allowed;
  }

  .act {
    min-height: var(--md-target);
    padding: 0 var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-0);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    cursor: pointer;
  }

  .act:disabled {
    border-style: dashed;
    color: var(--md-color-ink-muted);
    pointer-events: none;
  }

  .act--go {
    border-color: var(--md-color-focus);
    color: var(--md-color-focus);
  }

  .act--quiet {
    border-color: transparent;
    color: var(--md-color-ink-muted);
  }

  .why {
    flex-basis: 100%;
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }
</style>
