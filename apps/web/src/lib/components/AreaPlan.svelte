<!--
  A schematic plan of the mower's map in its local metre frame: metric grid (1 m and 5 m),
  areas, no-go zones (hatched), dock, and the order badges of selected areas. Display only; the
  interactive `MowerMap` replaces it; the plan stays as the fallback when Leaflet cannot load.
-->
<script lang="ts">
  type Ring = readonly (readonly number[])[];

  /** The parts of a `MowerMap` the plan draws (points as `[x, y]` in metres). */
  interface PlanMap {
    areas?: { id: string; name?: string | null; boundary: Ring }[];
    no_go_zones?: { id: string; boundary: Ring }[];
    obstacles?: { id: string; boundary: Ring }[];
    paths?: { id: string; points: Ring }[];
    dock?: { x: number; y: number } | null;
  }

  interface Props {
    map: PlanMap;
    selected?: string[];
    label: string;
    areaName: (area: { id: string; name?: string | null }) => string;
  }
  let { map, selected = [], label, areaName }: Props = $props();

  const PAD = 2;

  const bounds = $derived.by(() => {
    const points = [
      ...(map.areas ?? []).flatMap((a) => a.boundary),
      ...(map.no_go_zones ?? []).flatMap((z) => z.boundary),
      ...(map.dock ? [[map.dock.x, map.dock.y]] : []),
    ].map((p) => [p[0] ?? 0, p[1] ?? 0]);
    if (!points.length) return { minX: 0, minY: 0, maxX: 10, maxY: 10 };
    const xs = points.map((p) => p[0] ?? 0);
    const ys = points.map((p) => p[1] ?? 0);
    return {
      minX: Math.floor(Math.min(...xs)) - PAD,
      minY: Math.floor(Math.min(...ys)) - PAD,
      maxX: Math.ceil(Math.max(...xs)) + PAD,
      maxY: Math.ceil(Math.max(...ys)) + PAD,
    };
  });
  const width = $derived(bounds.maxX - bounds.minX);
  const height = $derived(bounds.maxY - bounds.minY);
  // y grows north in the map frame and down in SVG: flip.
  const fy = (y: number) => bounds.maxY - y + bounds.minY;
  const pts = (ring: Ring) => ring.map((p) => `${p[0] ?? 0},${fy(p[1] ?? 0)}`).join(' ');
  const centroid = (ring: Ring) => {
    const x = ring.reduce((s, p) => s + (p[0] ?? 0), 0) / ring.length;
    const y = ring.reduce((s, p) => s + (p[1] ?? 0), 0) / ring.length;
    return { x, y: fy(y) };
  };
  const range = (from: number, to: number) =>
    Array.from({ length: Math.max(0, to - from + 1) }, (_, i) => from + i);
</script>

<svg
  class="plan"
  viewBox="{bounds.minX} {bounds.minY} {width} {height}"
  preserveAspectRatio="xMidYMid meet"
  role="img"
  aria-label={label}
>
  <defs>
    <pattern
      id="nogo"
      width="0.8"
      height="0.8"
      patternUnits="userSpaceOnUse"
      patternTransform="rotate(45)"
    >
      <line x1="0" y1="0" x2="0" y2="0.8" class="hatch" />
    </pattern>
  </defs>
  {#each range(bounds.minX, bounds.maxX) as x (x)}
    <line x1={x} y1={bounds.minY} x2={x} y2={bounds.maxY} class:major={x % 5 === 0} class="grid" />
  {/each}
  {#each range(bounds.minY, bounds.maxY) as y (y)}
    <line x1={bounds.minX} y1={y} x2={bounds.maxX} y2={y} class:major={y % 5 === 0} class="grid" />
  {/each}
  {#each map.areas ?? [] as area (area.id)}
    {@const order = selected.indexOf(area.id)}
    {@const c = centroid(area.boundary)}
    <polygon points={pts(area.boundary)} class="area" class:selected={order >= 0} />
    <text x={c.x} y={c.y} class="name">{areaName(area).toUpperCase()}</text>
    {#if order >= 0}
      <rect x={c.x - 0.6} y={c.y + 0.4} width="1.2" height="1.2" class="badge" />
      <text x={c.x} y={c.y + 1.3} class="order">{order + 1}</text>
    {/if}
  {/each}
  {#each map.no_go_zones ?? [] as zone (zone.id)}
    <polygon points={pts(zone.boundary)} class="nogo" />
  {/each}
  {#each map.obstacles ?? [] as zone (zone.id)}
    <polygon points={pts(zone.boundary)} class="obstacle" />
  {/each}
  {#each map.paths ?? [] as path (path.id)}
    <polyline points={pts(path.points)} class="path" />
  {/each}
  {#if map.dock}
    <rect x={map.dock.x - 0.5} y={fy(map.dock.y) - 0.5} width="1" height="1" class="dock" />
  {/if}
</svg>

<style>
  .plan {
    display: block;
    width: 100%;
    height: 100%;
    background: var(--md-color-surface-sunken);
  }

  .grid {
    stroke: var(--md-color-line);
    stroke-width: 0.02;
    opacity: 0.6;
  }

  .grid.major {
    stroke-width: 0.04;
    opacity: 1;
  }

  .area {
    fill: color-mix(in srgb, var(--md-color-ink) 6%, transparent);
    stroke: var(--md-color-ink);
    stroke-width: 0.08;
  }

  .area.selected {
    stroke: var(--md-color-focus);
    stroke-width: 0.16;
  }

  .name {
    fill: var(--md-color-ink-muted);
    font-family: var(--md-font-sans);
    font-size: 0.6px;
    font-weight: var(--md-weight-semibold);
    letter-spacing: 0.06em;
    text-anchor: middle;
  }

  .badge {
    fill: var(--md-color-focus);
  }

  .order {
    fill: var(--md-color-ink-inverse);
    font-family: var(--md-font-mono);
    font-size: 0.9px;
    text-anchor: middle;
  }

  .nogo {
    fill: url('#nogo');
    stroke: var(--md-state-error);
    stroke-width: 0.06;
  }

  .hatch {
    stroke: var(--md-state-error);
    stroke-width: 0.12;
  }

  .obstacle {
    fill: color-mix(in srgb, var(--md-color-line-strong) 40%, transparent);
  }

  .path {
    fill: none;
    stroke: var(--md-color-ink-muted);
    stroke-width: 0.06;
    stroke-dasharray: 0.3 0.3;
  }

  .dock {
    fill: var(--md-color-ink);
  }
</style>
