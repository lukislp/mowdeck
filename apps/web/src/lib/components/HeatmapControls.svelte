<!--
  The heat layer strip under the mower map: the layer switcher (off, coverage, RTK, WLAN,
  mobile), the period, the cell size, "refresh", the legend of the active scale and the state
  of the last load (loading, empty, no map yet, error, samples). With `pathShown` it also holds
  the "Mähpfad" toggle (the mower's planned route on the map) and a line about the path. The
  page owns the choice and the data; this strip only shows them and reports changes.
-->
<script lang="ts" module>
  export type HeatStatus = 'idle' | 'loading' | 'ready' | 'empty' | 'no-map' | 'error';
</script>

<script lang="ts">
  import { heatScale } from '@mowdeck/ui';

  import { formatNumber } from '$lib/i18n';
  import {
    HEAT_CELL_SIZES,
    HEAT_LAYERS,
    HEAT_PERIODS,
    legendKindOf,
    type HeatCellSize,
    type HeatLayer,
    type HeatPeriod,
  } from '$lib/map/heatmap';
  import { m } from '$lib/paraglide/messages';

  interface Props {
    layer: HeatLayer;
    period: HeatPeriod;
    cell: HeatCellSize;
    status: HeatStatus;
    /** The reason of an error, already translated. */
    error?: string | null;
    /** Cell edge of the answer (coarser than `cell` when the server coarsened it). */
    answeredCell?: number | null;
    samples?: number | null;
    truncated?: boolean;
    /** The `job` period fell back to 24 h (no start among the recent commands). */
    fallback?: boolean;
    onlayer: (layer: HeatLayer) => void;
    onperiod: (period: HeatPeriod) => void;
    oncell: (cell: HeatCellSize) => void;
    onrefresh: () => void;
    /** The planned mow path is drawn (the toggle is hidden when undefined). */
    pathShown?: boolean;
    /** A line about the path (progress, or that it follows), already translated. */
    pathNote?: string | null;
    onpath?: (shown: boolean) => void;
  }

  let {
    layer,
    period,
    cell,
    status,
    error = null,
    answeredCell = null,
    samples = null,
    truncated = false,
    fallback = false,
    onlayer,
    onperiod,
    oncell,
    onrefresh,
    pathShown = undefined,
    pathNote = null,
    onpath,
  }: Props = $props();

  const uid = $props.id();

  const LAYER_LABEL: Record<HeatLayer, () => string> = {
    off: () => m.heat_layer_off(),
    coverage: () => m.heat_layer_coverage(),
    rtk: () => m.heat_layer_rtk(),
    wifi: () => m.heat_layer_wifi(),
    mnet: () => m.heat_layer_mnet(),
  };
  const PERIOD_LABEL: Record<HeatPeriod, () => string> = {
    job: () => m.heat_period_job(),
    '24h': () => m.heat_period_24h(),
    '7d': () => m.heat_period_7d(),
    '30d': () => m.heat_period_30d(),
  };

  /** The unit of the signal scale (not translated). */
  const DBM_UNIT = 'dBm';
  const metres = (value: number) => `${formatNumber(value, value % 1 ? 1 : 0)} m`;

  const kind = $derived(legendKindOf(layer));
  const scale = $derived(heatScale(kind));
  const coarsened = $derived(answeredCell != null && answeredCell > cell + 1e-9);

  /** Labels under the swatches: both ends, or one per step for the RTK bands. */
  const legendEnds = $derived(
    kind === 'ratio'
      ? [m.heat_legend_coverage_low(), m.heat_legend_coverage_high()]
      : [m.heat_legend_weak(), m.heat_legend_strong()],
  );
  const fixLabels = $derived([
    m.heat_legend_fix_none(),
    m.heat_legend_fix_float(),
    m.heat_legend_fix_fixed(),
  ]);
  const ticks = $derived(
    kind === 'dbm'
      ? ['-90', '-80', '-70', '-60', '-50', '-40']
      : kind === 'ratio'
        ? ['0 %', '100 %']
        : [],
  );

  function onkeydown(event: KeyboardEvent): void {
    const keys = ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp'];
    if (!keys.includes(event.key)) return;
    event.preventDefault();
    const step = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : -1;
    const next =
      HEAT_LAYERS[(HEAT_LAYERS.indexOf(layer) + step + HEAT_LAYERS.length) % HEAT_LAYERS.length]!;
    onlayer(next);
    const group = event.currentTarget as HTMLElement;
    queueMicrotask(() => group.querySelector<HTMLElement>(`[data-value="${next}"]`)?.focus());
  }
</script>

<div class="heat" data-testid="heatmap-controls" data-layer={layer}>
  {#if pathShown !== undefined}
    <div class="path">
      <button
        type="button"
        class="path-toggle"
        aria-pressed={pathShown}
        title={m.path_toggle_hint()}
        data-testid="mow-path-toggle"
        onclick={() => onpath?.(!pathShown)}
      >
        <span class="path-swatch" aria-hidden="true"></span>{m.path_toggle()}
      </button>
      {#if pathShown && pathNote}
        <span class="path-note mono" data-testid="mow-path-note">{pathNote}</span>
      {/if}
    </div>
  {/if}
  <div
    class="layers"
    role="radiogroup"
    aria-label={m.heat_layer()}
    data-testid="heatmap-layer"
    tabindex="-1"
    {onkeydown}
  >
    {#each HEAT_LAYERS as option (option)}
      <button
        type="button"
        role="radio"
        class="seg"
        class:seg--off={option === 'off'}
        aria-checked={option === layer}
        tabindex={option === layer ? 0 : -1}
        data-value={option}
        data-testid="heatmap-layer-{option}"
        onclick={() => onlayer(option)}>{LAYER_LABEL[option]()}</button
      >
    {/each}
  </div>

  {#if layer !== 'off'}
    <div class="row">
      <label class="field" for="{uid}-period">
        <span class="field-label">{m.heat_period()}</span>
        <select
          id="{uid}-period"
          class="select"
          value={period}
          data-testid="heatmap-period"
          onchange={(event) => onperiod(event.currentTarget.value as HeatPeriod)}
        >
          {#each HEAT_PERIODS as option (option)}
            <option value={option}>{PERIOD_LABEL[option]()}</option>
          {/each}
        </select>
      </label>
      <div class="field">
        <span class="field-label" id="{uid}-cell">{m.heat_cell()}</span>
        <div class="chips" role="radiogroup" aria-labelledby="{uid}-cell">
          {#each HEAT_CELL_SIZES as size (size)}
            <button
              type="button"
              role="radio"
              class="chip"
              aria-checked={cell === size}
              data-testid="heatmap-cell-{size}"
              onclick={() => oncell(size)}>{metres(size)}</button
            >
          {/each}
        </div>
      </div>
      <button
        type="button"
        class="refresh"
        disabled={status === 'loading'}
        data-testid="heatmap-refresh"
        onclick={onrefresh}>{m.heat_refresh()}</button
      >
    </div>

    <div class="legend" data-testid="heatmap-legend" data-kind={kind}>
      <span class="field-label legend-title">{m.heat_legend()}</span>
      <div class="scale" class:scale--bands={kind === 'fix'}>
        <div class="swatches" aria-hidden="true">
          {#each scale.stops as stop, i (stop.key)}
            <span class="swatch" style:--md-swatch={stop.color} data-stop={i}></span>
          {/each}
        </div>
        {#if kind === 'fix'}
          <ul class="bands">
            {#each fixLabels as text, i (i)}
              <li>{text}</li>
            {/each}
          </ul>
        {:else}
          <div class="ticks mono" aria-hidden="true">
            {#each ticks as tick, i (i)}<span>{tick}</span>{/each}
          </div>
          <div class="ends">
            <span>{legendEnds[0]}</span>
            {#if kind === 'dbm'}<span class="mono unit">{DBM_UNIT}</span>{/if}
            <span>{legendEnds[1]}</span>
          </div>
        {/if}
      </div>
    </div>

    <p class="status" role="status" data-testid="heatmap-status" data-state={status}>
      {#if status === 'loading'}
        <span class="pulse" aria-hidden="true"></span>{m.heat_loading()}
      {:else if status === 'empty'}
        {m.heat_empty()}
      {:else if status === 'no-map'}
        {m.heat_no_map()}
      {:else if status === 'error'}
        <span class="bad">{m.heat_error({ reason: error ?? '' })}</span>
      {:else if status === 'ready' && samples != null}
        <span class="mono" data-testid="heatmap-samples"
          >{m.heat_samples({ count: formatNumber(samples) })}</span
        >
        {#if coarsened && answeredCell}
          <span class="mono">· {m.heat_cell_coarsened({ size: metres(answeredCell) })}</span>
        {/if}
        {#if truncated}<span>· {m.heat_truncated()}</span>{/if}
      {/if}
      {#if fallback && status !== 'loading'}
        <span class="note">{m.heat_period_fallback()}</span>
      {/if}
    </p>
  {/if}
</div>

<style>
  .heat {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
    padding: var(--md-space-2) var(--md-space-3) var(--md-space-3);
    border-top: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
  }

  .path {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2) var(--md-space-3);
  }

  .path-toggle {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-2);
    min-height: var(--md-target);
    padding: 0 var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-0);
    background: var(--md-color-surface);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    cursor: pointer;
  }

  /* The swatch is the path itself: a thin accent line, dimmed while the path is hidden. */
  .path-swatch {
    width: 20px;
    height: 2px;
    background: var(--md-color-line-strong);
  }

  .path-toggle[aria-pressed='true'] {
    border-color: var(--md-color-focus);
    color: var(--md-color-ink);
  }

  .path-toggle[aria-pressed='true'] .path-swatch {
    background: linear-gradient(
      90deg,
      var(--md-color-focus) 55%,
      color-mix(in srgb, var(--md-color-focus) 38%, var(--md-color-surface)) 55%
    );
  }

  .path-toggle:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .path-note {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-variant-numeric: tabular-nums;
  }

  .layers {
    display: flex;
    border: var(--md-hairline) solid var(--md-color-line-strong);
    overflow-x: auto;
    scrollbar-width: none;
  }

  .seg {
    flex: 1 0 auto;
    min-width: 0;
    min-height: var(--md-target);
    padding: 0 var(--md-space-2);
    border: 0;
    border-inline-start: var(--md-hairline) solid var(--md-color-line);
    border-radius: var(--md-radius-0);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    white-space: nowrap;
    cursor: pointer;
  }

  .seg:first-child {
    border-inline-start: 0;
  }

  .seg--off {
    color: var(--md-color-ink-muted);
  }

  .seg[aria-checked='true'] {
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .seg:focus-visible,
  .chip:focus-visible,
  .refresh:focus-visible,
  .select:focus-visible {
    position: relative;
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: var(--md-space-3);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
  }

  .field-label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .select {
    min-height: var(--md-target);
    padding: 0 var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-0);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font: inherit;
    font-size: var(--md-font-size-14);
  }

  .chips {
    display: flex;
  }

  .chip {
    min-width: var(--md-target);
    min-height: var(--md-target);
    padding: 0 var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-0);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    cursor: pointer;
  }

  .chip + .chip {
    border-inline-start: 0;
  }

  .chip[aria-checked='true'] {
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .refresh {
    min-height: var(--md-target);
    margin-inline-start: auto;
    padding: 0 var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-0);
    background: none;
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    cursor: pointer;
  }

  .refresh:disabled {
    color: var(--md-color-line-strong);
    cursor: progress;
  }

  .legend {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: start;
    gap: var(--md-space-3);
  }

  .legend-title {
    padding-top: 1px;
  }

  .scale {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-0_5);
    max-width: 320px;
  }

  .swatches {
    display: grid;
    grid-auto-columns: minmax(0, 1fr);
    grid-auto-flow: column;
    height: 12px;
    border: var(--md-hairline) solid var(--md-color-line-strong);
  }

  /* The map draws cells at half opacity over the sunken surface; the swatch matches that. */
  .swatch {
    background: color-mix(in srgb, var(--md-swatch) 50%, var(--md-color-surface-sunken));
  }

  .swatch + .swatch {
    border-inline-start: var(--md-hairline) solid var(--md-color-surface);
  }

  .ticks {
    display: flex;
    justify-content: space-between;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-variant-numeric: tabular-nums;
  }

  .ends {
    display: flex;
    justify-content: space-between;
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .unit {
    color: var(--md-color-ink-muted);
    letter-spacing: 0;
    text-transform: none;
  }

  .bands {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    margin: 0;
    padding: 0;
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    list-style: none;
  }

  .bands li + li {
    text-align: center;
  }

  .bands li:last-child {
    text-align: end;
  }

  .status {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-1) var(--md-space-2);
    min-height: var(--md-line-20);
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .bad {
    color: var(--md-state-error);
  }

  .note {
    flex-basis: 100%;
  }

  .pulse {
    width: 8px;
    height: 8px;
    background: var(--md-color-ink-muted);
    animation: md-heat-pulse 1s steps(2, jump-none) infinite;
  }

  @keyframes md-heat-pulse {
    to {
      opacity: 0.2;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .pulse {
      animation: none;
    }
  }
</style>
