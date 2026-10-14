<!--
  One metric of the last 30 UTC days as a strip of bars (no chart library): ink bars on a hairline
  baseline, scaled to the peak, the newest day on the right, the peak and the total as readouts
  and the first and last day underneath. The SVG has an accessible summary; the numbers are in
  `title` elements per bar for pointer users.
-->
<script lang="ts">
  import { barGeometry } from '$lib/health/series';

  interface Props {
    label: string;
    /** One value per day, oldest first. */
    values: number[];
    days: string[];
    format: (value: number) => string;
    unit: string;
    peakLabel: (value: string) => string;
    summary: string;
    firstLabel: string;
    lastLabel: string;
    testid?: string;
  }
  let {
    label,
    values,
    days,
    format,
    unit,
    peakLabel,
    summary,
    firstLabel,
    lastLabel,
    testid,
  }: Props = $props();

  const WIDTH = 300;
  const HEIGHT = 64;
  const bars = $derived(barGeometry(values, WIDTH, HEIGHT));
  const peak = $derived(Math.max(0, ...values));
  const total = $derived(values.reduce((sum, v) => sum + v, 0));
</script>

<figure class="daily" data-testid={testid} data-total={total}>
  <figcaption>
    <span class="label">{label}</span>
    <span class="total mono">{format(total)}<small>{unit}</small></span>
  </figcaption>
  <svg
    viewBox="0 0 {WIDTH} {HEIGHT}"
    preserveAspectRatio="none"
    role="img"
    aria-label={summary}
    data-bars={bars.filter((b) => b.height > 0).length}
  >
    <line class="peak-line" x1="0" y1="0.5" x2={WIDTH} y2="0.5" />
    {#each bars as bar, i (days[i])}
      <rect
        class="bar"
        class:today={i === bars.length - 1}
        x={bar.x}
        y={bar.y}
        width={bar.width}
        height={bar.height}
      >
        <title>{days[i]}: {format(bar.value)} {unit}</title>
      </rect>
    {/each}
    <line class="base" x1="0" y1={HEIGHT - 0.5} x2={WIDTH} y2={HEIGHT - 0.5} />
  </svg>
  <div class="axis mono">
    <span>{firstLabel}</span>
    <span>{peakLabel(`${format(peak)} ${unit}`)}</span>
    <span>{lastLabel}</span>
  </div>
</figure>

<style>
  .daily {
    display: grid;
    gap: var(--md-space-1);
    margin: 0;
  }

  figcaption {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--md-space-2);
  }

  .label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .total {
    color: var(--md-color-ink);
    font-size: var(--md-font-size-16);
  }

  .total small {
    margin-inline-start: var(--md-space-0_5);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  svg {
    display: block;
    width: 100%;
    height: 64px;
    background: var(--md-color-surface-sunken);
  }

  .bar {
    fill: var(--md-color-ink-muted);
  }

  .bar.today {
    fill: var(--md-color-ink);
  }

  .base {
    stroke: var(--md-color-line-strong);
    stroke-width: 1;
    vector-effect: non-scaling-stroke;
  }

  .peak-line {
    stroke: var(--md-color-line);
    stroke-width: 1;
    stroke-dasharray: 2 3;
    vector-effect: non-scaling-stroke;
  }

  .axis {
    display: flex;
    justify-content: space-between;
    gap: var(--md-space-2);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
  }
</style>
