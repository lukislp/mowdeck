<!--
  The battery level of the last 24 hours as a line (no chart library): 0 to 100 % on a sunken
  plate with dashed guides at 20, 50 and 80 %, the band below 20 % in the error tint, the line in
  ink (it breaks over gaps). Min, max and the latest value are readouts beside it.
-->
<script lang="ts">
  import { batteryPath, type BatteryPoint } from '$lib/health/series';

  interface Props {
    points: BatteryPoint[];
    from: number;
    to: number;
    summary: string;
    startLabel: string;
    endLabel: string;
    testid?: string;
  }
  let { points, from, to, summary, startLabel, endLabel, testid }: Props = $props();

  const WIDTH = 300;
  const HEIGHT = 100;
  const path = $derived(batteryPath(points, from, to, WIDTH, HEIGHT));
  /** The newest point as a marker (a few minutes of data are a sliver on a 24 h axis). */
  const latest = $derived.by(() => {
    const last = [...points].sort((a, b) => Date.parse(a.at) - Date.parse(b.at)).at(-1);
    if (!last) return null;
    const span = Math.max(1, to - from);
    const at = Math.min(Math.max(Date.parse(last.at), from), to);
    return {
      x: ((at - from) / span) * 100,
      y: 100 - Math.min(Math.max(last.battery_percent, 0), 100),
    };
  });
</script>

<figure class="trace" data-testid={testid} data-points={points.length}>
  <div class="plot">
    <svg
      viewBox="0 -4 {WIDTH} {HEIGHT + 8}"
      preserveAspectRatio="none"
      role="img"
      aria-label={summary}
    >
      <rect class="low" x="0" y={HEIGHT * 0.8} width={WIDTH} height={HEIGHT * 0.2} />
      {#each [20, 50, 80] as level (level)}
        <line class="guide" x1="0" y1={HEIGHT - level} x2={WIDTH} y2={HEIGHT - level} />
      {/each}
      <path class="line" d={path} />
    </svg>
    {#if latest}
      <span
        class="latest"
        style:inset-inline-start="{latest.x}%"
        style:top="calc(4px + {latest.y / 100} * (100% - 8px))"
        aria-hidden="true"
      ></span>
    {/if}
  </div>
  <div class="scale mono" aria-hidden="true">
    <span>100</span>
    <span>50</span>
    <span>0</span>
  </div>
  <div class="axis mono">
    <span>{startLabel}</span>
    <span>{endLabel}</span>
  </div>
</figure>

<style>
  .trace {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: var(--md-space-1) var(--md-space-2);
    margin: 0;
  }

  .plot {
    position: relative;
  }

  .latest {
    position: absolute;
    width: 8px;
    height: 8px;
    border: var(--md-border-control) solid var(--md-color-surface);
    background: var(--md-color-ink);
    transform: translate(-50%, -50%);
  }

  svg {
    display: block;
    width: 100%;
    height: 120px;
    border: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface-sunken);
  }

  .low {
    fill: var(--md-state-error-bg);
  }

  .guide {
    stroke: var(--md-color-line-strong);
    stroke-width: 1;
    stroke-dasharray: 3 4;
    vector-effect: non-scaling-stroke;
  }

  .line {
    fill: none;
    stroke: var(--md-color-ink);
    stroke-width: 2;
    stroke-linejoin: round;
    vector-effect: non-scaling-stroke;
  }

  .scale {
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    height: 120px;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
  }

  .axis {
    display: flex;
    justify-content: space-between;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }
</style>
