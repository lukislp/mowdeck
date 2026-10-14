<!--
  The fleet map: every visible mower on one map, on Leaflet 1.9 like `MowerMap`. Each mower is a
  marker in its state colour (chevron with heading, dot without; hollow when stale or offline)
  with its name; a tap or Enter selects it (`onselect`), a tap on the map clears the selection.
  The areas and docks of mowers whose map snapshot is loaded are drawn faintly underneath.

  Mowers of several sites have different local frames, so the fleet map always uses the `geo`
  projection (EPSG:3857). Without tiles it sends no request to any third party and draws a
  metric grid around the fleet reference instead, with a step that follows the visible span
  (`fleet.ts`); with `tiles` the grid gives way to the tile layer. Texts arrive in `labels`.
-->
<script lang="ts" module>
  /** Translated texts of the fleet map. */
  export interface FleetMapLabels {
    region: string;
    zoomIn: string;
    zoomOut: string;
    fit: string;
    /** The toggle of the area layer. */
    areas: string;
    north: string;
    /** Accessible name of a marker: the mower's name and its state word. */
    marker: (name: string, state: string) => string;
  }
</script>

<script lang="ts">
  import type * as Leaflet from 'leaflet';
  import { untrack } from 'svelte';

  import Icon from '../icons/Icon.svelte';

  import {
    fleetBounds,
    fleetGeometry,
    fleetGrid,
    fleetMarkers,
    fleetReference,
    type FleetMarker,
    type FleetMower,
    type FleetTone,
  } from './fleet';
  import type { MapTiles } from './MowerMap.svelte';
  import { createProjection } from './projection';
  import { scaleFor } from './scale';

  type LeafletModule = typeof import('leaflet');

  interface Props {
    mowers: readonly FleetMower[];
    tiles?: MapTiles | null;
    labels: FleetMapLabels;
    /** The state word of a tone (shown to assistive technology with the name). */
    toneLabel: (tone: FleetTone) => string;
    /** The selected mower (bindable). */
    selected?: string | null;
    onselect?: (id: string | null) => void;
    areaName?: (area: { id: string; name?: string | null }) => string;
    /** Leaflet could not be loaded or started. */
    onfail?: (error: unknown) => void;
    /** Refit whenever this changes (e.g. the filter); the first positions always fit. */
    fitKey?: string;
    testid?: string;
  }

  let {
    mowers,
    tiles = null,
    labels,
    toneLabel,
    selected = $bindable(null),
    onselect,
    areaName,
    onfail,
    fitKey = '',
    testid = 'fleet-map',
  }: Props = $props();

  let container: HTMLDivElement | undefined = $state();
  let L: LeafletModule | null = $state.raw(null);
  let generation = $state(0);
  let showAreas = $state(true);
  let scale = $state({ width: 0, label: '' });
  /** Bumped after every move or zoom, so the grid follows the view. */
  let view = $state(0);

  let lmap: Leaflet.Map | null = null;
  let groups: Record<'grid' | 'areas' | 'docks', Leaflet.LayerGroup> | null = null;
  let markerLayers = new Map<string, Leaflet.Marker>();
  let fittedKey: string | null = null;
  /** Refit on resize until the user pans or zooms (the fit control turns it back on). */
  let autoFit = true;

  const reference = $derived(fleetReference(mowers));
  const projection = $derived(
    createProjection(reference ?? { origin_lat: 0, origin_lon: 0 }, 'geo'),
  );
  const markers = $derived(fleetMarkers(mowers, projection));
  const geometry = $derived(fleetGeometry(mowers, areaName));
  const bounds = $derived(fleetBounds(markers, geometry.areas));
  const withTiles = $derived(!!tiles?.url);

  // --- Leaflet loading and the map instance -------------------------------------------------

  $effect(() => {
    let cancelled = false;
    Promise.all([import('leaflet'), import('leaflet/dist/leaflet.css')])
      .then(([mod]) => {
        if (cancelled) return;
        L = ((mod as unknown as { default?: LeafletModule }).default ?? mod) as LeafletModule;
      })
      .catch((error: unknown) => {
        if (!cancelled) onfail?.(error);
      });
    return () => {
      cancelled = true;
    };
  });

  $effect(() => {
    const lib = L;
    const el = container;
    const currentTiles = tiles;
    if (!lib || !el) return;
    let instance: Leaflet.Map;
    try {
      instance = lib.map(el, {
        crs: lib.CRS.EPSG3857,
        zoomControl: false,
        attributionControl: false,
        boxZoom: false,
        zoomSnap: 0.25,
        zoomDelta: 0.5,
        wheelPxPerZoomLevel: 120,
        minZoom: 3,
        maxZoom: 22,
        fadeAnimation: false,
      });
    } catch (error) {
      onfail?.(error);
      return;
    }
    instance.createPane('md-grid').style.zIndex = String(210);
    if (currentTiles?.url) {
      lib
        .tileLayer(currentTiles.url, {
          maxZoom: 22,
          maxNativeZoom: currentTiles.maxZoom ?? 19,
          className: 'md-map-tiles',
        })
        .addTo(instance);
    }
    groups = {
      grid: lib.layerGroup().addTo(instance),
      areas: lib.layerGroup().addTo(instance),
      docks: lib.layerGroup().addTo(instance),
    };
    instance.on('zoomend moveend', () => {
      scale = scaleBar(instance);
      untrack(() => (view += 1));
    });
    instance.on('click', () => select(null));
    instance.on('dragstart', () => (autoFit = false));
    instance.setView([0, 0], 3);
    lmap = instance;
    fittedKey = null;
    autoFit = true;
    untrack(() => (generation += 1));
    const manual = () => (autoFit = false);
    for (const type of ['wheel', 'dblclick', 'keydown'] as const) el.addEventListener(type, manual);
    const resize = new ResizeObserver(() => {
      instance.invalidateSize({ pan: false });
      if (autoFit && fittedKey !== null) untrack(() => fit(false));
    });
    resize.observe(el);
    return () => {
      for (const type of ['wheel', 'dblclick', 'keydown'] as const) {
        el.removeEventListener(type, manual);
      }
      resize.disconnect();
      instance.remove();
      lmap = null;
      groups = null;
      markerLayers = new Map();
    };
  });

  // --- the metric grid of the visible span (no tiles only) ----------------------------------

  $effect(() => {
    void generation;
    void view;
    const lib = L;
    const g = groups;
    const m = lmap;
    const current = projection;
    if (!lib || !g || !m) return;
    g.grid.clearLayers();
    if (withTiles || !reference) return;
    const box = m.getBounds();
    const corners = [box.getSouthWest(), box.getNorthEast(), box.getNorthWest(), box.getSouthEast()]
      .map((c) => current.toLocal([c.lat, c.lng]))
      .filter((c) => c.every(Number.isFinite));
    if (corners.length < 4) return;
    const xs = corners.map((c) => c[0]);
    const ys = corners.map((c) => c[1]);
    const extent: [number, number, number, number] = [
      Math.min(...xs),
      Math.min(...ys),
      Math.max(...xs),
      Math.max(...ys),
    ];
    for (const line of fleetGrid(extent, current, 10)) {
      lib
        .polyline([line.from, line.to], {
          pane: 'md-grid',
          interactive: false,
          className: line.major ? 'md-grid md-grid--major' : 'md-grid',
        })
        .addTo(g.grid);
    }
  });

  // --- areas and docks of the loaded maps ---------------------------------------------------

  $effect(() => {
    void generation;
    const lib = L;
    const g = groups;
    const m = lmap;
    const layers = geometry;
    const on = showAreas;
    if (!lib || !g || !m) return;
    g.areas.clearLayers();
    g.docks.clearLayers();
    if (!on) return;
    for (const area of layers.areas) {
      lib
        .polygon(area.ring, { interactive: false, className: 'md-area md-area--fleet' })
        .addTo(g.areas);
    }
    for (const dock of layers.docks) {
      lib
        .circleMarker(dock.at, { radius: 3, interactive: false, className: 'md-fleet-dock' })
        .addTo(g.docks);
    }
  });

  // --- markers ------------------------------------------------------------------------------

  $effect(() => {
    void generation;
    const lib = L;
    const m = lmap;
    const current = markers;
    const chosen = selected;
    if (!lib || !m) return;
    const seen = new Set<string>();
    for (const marker of current) {
      seen.add(marker.id);
      let layer = markerLayers.get(marker.id);
      if (!layer) {
        const root = markerElement();
        layer = lib
          .marker(marker.at, {
            icon: lib.divIcon({ html: root, className: 'md-map-icon', iconSize: [0, 0] }),
            keyboard: true,
            riseOnHover: true,
          })
          .addTo(m);
        const id = marker.id;
        layer.on('click', () => select(selected === id ? null : id));
        markerLayers.set(marker.id, layer);
      } else {
        layer.setLatLng(marker.at);
      }
      update(layer, marker, marker.id === chosen);
    }
    for (const [id, layer] of markerLayers) {
      if (!seen.has(id)) {
        layer.remove();
        markerLayers.delete(id);
      }
    }
  });

  // --- fitting ------------------------------------------------------------------------------

  // A new filter fits again; more markers or areas fit again until the user moved the map.
  let fittedCount = 0;
  $effect(() => {
    void generation;
    const key = fitKey;
    const box = bounds;
    const count = markers.length + geometry.areas.length;
    if (!lmap || !box) return;
    if (fittedKey !== key) {
      fittedKey = key;
      autoFit = true;
    } else if (!autoFit || count <= fittedCount) {
      return;
    }
    fittedCount = count;
    untrack(() => fit(false));
  });

  // --- helpers ------------------------------------------------------------------------------

  function select(id: string | null): void {
    selected = id;
    onselect?.(id);
  }

  function fit(animate = true): void {
    const m = lmap;
    const box = bounds;
    if (!m || !box) return;
    m.fitBounds(box, { padding: [56, 56], maxZoom: 19, animate: animate && !reducedMotion() });
  }

  function reducedMotion(): boolean {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function scaleBar(m: Leaflet.Map): { width: number; label: string } {
    const y = m.getSize().y / 2;
    const metres = m.distance(m.containerPointToLatLng([0, y]), m.containerPointToLatLng([100, y]));
    return scaleFor(metres / 100);
  }

  const SVG_NS = 'http://www.w3.org/2000/svg';

  function svg(tag: string, attrs: Record<string, string>): SVGElement {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
    return el;
  }

  /** The marker: a 48 px target with the glyph in the middle and the name beside it. */
  function markerElement(): HTMLElement {
    const root = document.createElement('span');
    root.className = 'md-fleet-marker';
    root.setAttribute('data-testid', 'fleet-marker');
    const hit = document.createElement('span');
    hit.className = 'md-fleet-hit';
    const rotor = document.createElement('span');
    rotor.className = 'md-map-rotor';
    const glyph = svg('svg', { viewBox: '0 0 24 24', width: '24', height: '24' });
    glyph.setAttribute('aria-hidden', 'true');
    glyph.append(svg('path', { class: 'md-map-chevron', d: 'M12 2.5 20 21 12 16.5 4 21Z' }));
    glyph.append(svg('circle', { class: 'md-map-dot', cx: '12', cy: '12', r: '6' }));
    rotor.append(glyph);
    hit.append(rotor);
    const name = document.createElement('span');
    name.className = 'md-fleet-name';
    root.append(hit, name);
    return root;
  }

  function update(layer: Leaflet.Marker, marker: FleetMarker, isSelected: boolean): void {
    const icon = layer.getElement();
    const root = icon?.firstElementChild as HTMLElement | null;
    if (!icon || !root) return;
    icon.setAttribute('aria-label', labels.marker(marker.name, toneLabel(marker.tone)));
    icon.setAttribute('aria-pressed', String(isSelected));
    root.dataset.mower = marker.id;
    root.dataset.tone = marker.tone;
    root.classList.toggle('is-hollow', marker.hollow);
    root.classList.toggle('no-heading', marker.rotation === null);
    root.classList.toggle('is-selected', isSelected);
    const rotor = root.querySelector<HTMLElement>('.md-map-rotor');
    rotor?.style.setProperty('--md-map-rotation', `${marker.rotation ?? 0}deg`);
    const name = root.querySelector('.md-fleet-name');
    if (name && name.textContent !== marker.name) name.textContent = marker.name;
    layer.setZIndexOffset(isSelected ? 1000 : 0);
  }
</script>

<div
  class="md-map md-fleet"
  class:is-geo={withTiles}
  data-testid={testid}
  data-mode="geo"
  data-ready={L !== null && generation > 0}
  data-markers={markers.length}
  data-areas={geometry.areas.length}
>
  <div class="canvas" bind:this={container} role="region" aria-label={labels.region}></div>

  <div class="hud" aria-hidden="true">
    <span class="north">
      <span class="needle"></span>
      <span class="letter">{labels.north}</span>
    </span>
    {#if scale.width > 0}
      <span class="scale">
        <span class="scale-bar" style:width="{scale.width}px"></span>
        <span class="scale-label">{scale.label}</span>
      </span>
    {/if}
  </div>

  <div class="controls">
    <div class="stack">
      <button
        type="button"
        class="ctl"
        aria-label={labels.zoomIn}
        title={labels.zoomIn}
        onclick={() => {
          autoFit = false;
          lmap?.zoomIn();
        }}
      >
        <Icon name="plus" />
      </button>
      <button
        type="button"
        class="ctl"
        aria-label={labels.zoomOut}
        title={labels.zoomOut}
        onclick={() => {
          autoFit = false;
          lmap?.zoomOut();
        }}
      >
        <Icon name="minus" />
      </button>
    </div>
    <div class="stack">
      <button
        type="button"
        class="ctl"
        aria-label={labels.fit}
        title={labels.fit}
        data-testid="fleet-map-fit"
        onclick={() => {
          autoFit = true;
          fit(true);
        }}
      >
        <Icon name="fit" />
      </button>
      <button
        type="button"
        class="ctl"
        aria-label={labels.areas}
        title={labels.areas}
        aria-pressed={showAreas}
        data-testid="fleet-map-areas"
        onclick={() => (showAreas = !showAreas)}
      >
        <Icon name="layers" />
      </button>
    </div>
  </div>

  {#if withTiles && tiles?.attribution}
    <p class="attribution">{tiles.attribution}</p>
  {/if}
</div>

<style>
  .md-map {
    position: relative;
    width: 100%;
    height: 100%;
    min-height: 0;
    overflow: hidden;
    background: var(--md-color-surface-sunken);
    color: var(--md-color-ink);
    isolation: isolate;
  }

  .canvas {
    position: absolute;
    inset: 0;
    z-index: 0;
  }

  .canvas:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .md-map :global(.leaflet-container) {
    background: var(--md-color-surface-sunken);
    font-family: var(--md-font-sans);
    outline: none;
  }

  .md-map :global(.leaflet-container.leaflet-touch-drag) {
    touch-action: none;
  }

  .md-map.is-geo :global(.md-map-tiles) {
    filter: saturate(0.6);
  }

  .md-map :global(.md-grid) {
    stroke: var(--md-color-line);
    stroke-width: 1;
    stroke-opacity: 0.45;
    fill: none;
    shape-rendering: crispedges;
  }

  .md-map :global(.md-grid--major) {
    stroke-opacity: 1;
  }

  .md-map :global(path.md-area--fleet) {
    fill: var(--md-color-ink);
    fill-opacity: 0.05;
    stroke: var(--md-color-ink-muted);
    stroke-width: 1;
    stroke-opacity: 0.8;
  }

  .md-map :global(path.md-fleet-dock) {
    fill: var(--md-color-ink);
    fill-opacity: 1;
    stroke: var(--md-color-surface-sunken);
    stroke-width: 1.5;
  }

  /* --- markers ---------------------------------------------------------------------------- */

  .md-map :global(.md-map-icon) {
    overflow: visible;
    border: 0;
    background: none;
  }

  .md-map :global(.md-map-icon:focus-visible) {
    outline: none;
  }

  .md-map :global(.md-map-icon:focus-visible .md-fleet-hit) {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .md-map :global(.md-fleet-marker) {
    --md-map-tone: var(--md-state-unknown);

    position: absolute;
    top: -24px;
    left: -24px;
    display: flex;
    align-items: center;
    cursor: pointer;
  }

  .md-map :global(.md-fleet-hit) {
    display: grid;
    place-items: center;
    width: var(--md-target);
    height: var(--md-target);
    border-radius: var(--md-radius-round);
  }

  .md-map :global(.md-fleet-marker.is-selected .md-fleet-hit) {
    box-shadow:
      inset 0 0 0 2px var(--md-map-tone),
      inset 0 0 0 4px var(--md-color-surface);
    background: color-mix(in srgb, var(--md-map-tone) 18%, transparent);
  }

  .md-map :global(.md-fleet-name) {
    margin-inline-start: calc(var(--md-space-1) * -1);
    padding: var(--md-space-0_5) var(--md-space-2);
    border: var(--md-hairline) solid var(--md-color-line);
    border-inline-start: 3px solid var(--md-map-tone);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    line-height: var(--md-line-16);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    white-space: nowrap;
  }

  .md-map :global(.md-fleet-marker.is-selected .md-fleet-name) {
    border-color: var(--md-color-ink);
    border-inline-start-color: var(--md-map-tone);
  }

  .md-map :global(.md-fleet-marker[data-tone='mowing']) {
    --md-map-tone: var(--md-state-mowing);
  }

  .md-map :global(.md-fleet-marker[data-tone='returning']) {
    --md-map-tone: var(--md-state-returning);
  }

  .md-map :global(.md-fleet-marker[data-tone='charging']) {
    --md-map-tone: var(--md-state-charging);
  }

  .md-map :global(.md-fleet-marker[data-tone='docked']) {
    --md-map-tone: var(--md-state-docked);
  }

  .md-map :global(.md-fleet-marker[data-tone='paused']) {
    --md-map-tone: var(--md-state-paused);
  }

  .md-map :global(.md-fleet-marker[data-tone='error']) {
    --md-map-tone: var(--md-state-error);
  }

  .md-map :global(.md-fleet-marker[data-tone='offline']) {
    --md-map-tone: var(--md-state-offline);
  }

  .md-map :global(.md-map-rotor) {
    display: block;
    transform: rotate(var(--md-map-rotation, 0deg));
  }

  .md-map :global(.md-map-rotor svg) {
    display: block;
    overflow: visible;
  }

  .md-map :global(.md-map-chevron),
  .md-map :global(.md-map-dot) {
    fill: var(--md-map-tone);
    stroke: var(--md-color-surface);
    stroke-width: 2;
    stroke-linejoin: miter;
    paint-order: stroke;
  }

  .md-map :global(.md-map-dot) {
    display: none;
  }

  .md-map :global(.md-fleet-marker.no-heading .md-map-chevron) {
    display: none;
  }

  .md-map :global(.md-fleet-marker.no-heading .md-map-dot) {
    display: inline;
  }

  .md-map :global(.md-fleet-marker.is-hollow .md-map-chevron),
  .md-map :global(.md-fleet-marker.is-hollow .md-map-dot) {
    fill: var(--md-color-surface);
    stroke: var(--md-map-tone);
    stroke-width: 1.5;
    stroke-dasharray: 3 2;
  }

  /* Markers glide between live positions; never during a zoom animation. */
  .md-map :global(.leaflet-marker-icon.md-map-icon) {
    transition: transform 900ms linear;
  }

  .md-map :global(.leaflet-zoom-anim .leaflet-marker-icon.md-map-icon) {
    transition: none;
  }

  /* --- HUD and controls (as in MowerMap) -------------------------------------------------- */

  .hud {
    position: absolute;
    inset-inline-start: var(--md-space-3);
    bottom: var(--md-space-3);
    z-index: 2;
    display: flex;
    align-items: flex-end;
    gap: var(--md-space-4);
    pointer-events: none;
  }

  .north {
    position: relative;
    display: grid;
    place-items: center;
    width: 32px;
    height: 32px;
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-round);
    background: var(--md-color-surface);
  }

  .needle {
    position: absolute;
    inset: 3px;
  }

  .needle::before {
    content: '';
    position: absolute;
    top: 0;
    left: 50%;
    width: 2px;
    height: 7px;
    margin-left: -1px;
    background: var(--md-color-ink);
  }

  .letter {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
  }

  .scale {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-0_5);
    padding: var(--md-space-1) var(--md-space-2);
    background: color-mix(in srgb, var(--md-color-surface) 85%, transparent);
  }

  .scale-bar {
    display: block;
    height: 6px;
    border: var(--md-border-control) solid var(--md-color-ink);
    border-top: 0;
  }

  .scale-label {
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-11);
    font-variant-numeric: tabular-nums;
    line-height: var(--md-line-16);
  }

  .controls {
    position: absolute;
    top: var(--md-space-3);
    inset-inline-end: var(--md-space-3);
    z-index: 3;
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
  }

  .stack {
    display: flex;
    flex-direction: column;
    border: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface);
  }

  .ctl {
    display: grid;
    place-items: center;
    width: var(--md-target);
    height: var(--md-target);
    padding: 0;
    border: 0;
    border-radius: var(--md-radius-0);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    cursor: pointer;
    transition: background-color var(--md-motion-fast) var(--md-ease);
  }

  .ctl + .ctl {
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .ctl:hover {
    background: var(--md-color-surface-sunken);
  }

  .ctl:focus-visible {
    position: relative;
    z-index: 1;
    outline: 2px solid var(--md-color-focus);
    outline-offset: 0;
  }

  .ctl[aria-pressed='true'] {
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .attribution {
    position: absolute;
    inset-inline-end: 0;
    bottom: 0;
    z-index: 2;
    max-width: 70%;
    margin: 0;
    padding: var(--md-space-0_5) var(--md-space-2);
    background: color-mix(in srgb, var(--md-color-surface) 85%, transparent);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    line-height: var(--md-line-16);
  }

  @media (prefers-reduced-motion: reduce) {
    .ctl,
    .md-map :global(.leaflet-marker-icon.md-map-icon) {
      transition: none;
    }
  }
</style>
