<!--
  A round patch of lawn mowed in stripes at the next angle (ADR 0061), with the angles of the
  runs after it as numbered marks on the rim. The stripes turn to a new angle with a short
  animation (none with reduced motion). Schematic: 0° draws the stripes across, angles turn
  counter-clockwise as on the mower map; the mower's own reference may differ.
-->
<script lang="ts">
  import { formatNumber } from '#lib/i18n.js';
  import { angleText } from '#lib/schedules/rotation.js';

  interface Props {
    /** The angles of the coming runs, next first; empty draws an unmowed patch. */
    angles: readonly number[];
    /** Rendered size in pixels. */
    size?: number;
    /** Accessible description, e.g. "Next runs: 30°, 75°, 120°". */
    label: string;
    /** Show the next angle in the middle. */
    readout?: boolean;
    testid?: string;
  }
  let { angles, size = 112, label, readout = true, testid }: Props = $props();

  const next = $derived(angles[0] ?? null);
  const later = $derived(angles.slice(1, 6));
  const STRIPES = Array.from({ length: 16 }, (_, i) => -64 + i * 8);
  const uid = $props.id();

  function rim(angle: number, radius: number): { x: number; y: number } {
    const rad = (angle * Math.PI) / 180;
    return { x: radius * Math.cos(rad), y: -radius * Math.sin(rad) };
  }
</script>

<figure class="dial" style:--size="{size}px" data-testid={testid}>
  <svg viewBox="-50 -50 100 100" role="img" aria-label={label}>
    <defs>
      <clipPath id="patch-{uid}"><circle r="38" /></clipPath>
    </defs>
    <circle class="ring" r="44" />
    <g clip-path="url(#patch-{uid})">
      <circle class="ground" r="38" />
      {#if next !== null}
        <g class="stripes" style:transform="rotate({-next}deg)">
          {#each STRIPES as y, i (y)}
            <rect x="-64" {y} width="128" height="8" class={i % 2 ? 'dark' : 'light'} />
          {/each}
        </g>
      {/if}
    </g>
    {#each later as angle, i (i)}
      {@const p = rim(angle, 44)}
      <g class="mark" style:opacity={1 - i * 0.15}>
        <circle cx={p.x} cy={p.y} r="5" />
        <text x={p.x} y={p.y} dy="0.35em">{i + 2}</text>
      </g>
    {/each}
    {#if next !== null}
      {@const a = rim(next, 44)}
      {@const b = rim(next + 180, 44)}
      <line class="axis" x1={b.x} y1={b.y} x2={a.x} y2={a.y} />
      <circle class="needle" cx={a.x} cy={a.y} r="5.5" />
      <text class="needle-index" x={a.x} y={a.y} dy="0.35em">1</text>
    {/if}
  </svg>
  {#if readout}
    <figcaption class="readout mono">
      {next === null ? '–' : angleText(next, formatNumber)}
    </figcaption>
  {/if}
</figure>

<style>
  .dial {
    position: relative;
    display: grid;
    flex: none;
    place-items: center;
    width: var(--size);
    height: var(--size);
    margin: 0;
  }

  svg {
    width: 100%;
    height: 100%;
    overflow: visible;
  }

  .ring {
    fill: none;
    stroke: var(--md-color-line);
    stroke-width: 0.75;
    stroke-dasharray: 1.5 2.5;
  }

  .ground {
    fill: var(--md-state-mowing-bg);
  }

  .stripes {
    transition: transform 700ms cubic-bezier(0.2, 0.8, 0.2, 1);
  }

  .light {
    fill: var(--md-state-mowing-bg);
  }

  .dark {
    fill: color-mix(in srgb, var(--md-state-mowing) 34%, var(--md-state-mowing-bg));
  }

  .axis {
    stroke: var(--md-color-ink);
    stroke-width: 0.9;
    stroke-dasharray: 3 2;
    opacity: 0.55;
  }

  .needle {
    fill: var(--md-color-ink);
  }

  .needle-index,
  .mark text {
    font-family: var(--md-font-mono);
    font-size: 6px;
    font-weight: 600;
    text-anchor: middle;
  }

  .needle-index {
    fill: var(--md-color-ink-inverse);
  }

  .mark circle {
    fill: var(--md-color-surface);
    stroke: var(--md-color-line-strong);
    stroke-width: 0.75;
  }

  .mark text {
    fill: var(--md-color-ink-muted);
  }

  .readout {
    position: absolute;
    padding: 1px 6px;
    border-radius: var(--md-radius-0);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-semibold);
    box-shadow: 0 0 0 var(--md-hairline) var(--md-color-line);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  @media (prefers-reduced-motion: reduce) {
    .stripes {
      transition: none;
    }
  }
</style>
