<!--
  The map of theft tracking: a recorded track on a world map (EPSG:3857), the start (green) and
  the last known position (red and pulsing, hollow grey while the mower is silent). It is not
  tied to the mower's own map: the mower may be anywhere, so positions are plain latitude and
  longitude and the only background is the installation's raster tile server (`tiles`). Without
  one the track is drawn on a plain surface and `labels.noTiles` says so.

  The view fits the track until the user pans or zooms (the fit control turns it back on).
  `follow` (bindable) keeps the last position in the centre; a drag ends it. Leaflet is loaded
  lazily; `onfail` reports a failed load. The page owns the texts (`labels`).
-->
<script lang="ts" module>
  /** A position on the map. */
  export interface TrackMapPoint {
    lat: number;
    lon: number;
  }

  /** Translated texts of the map controls and markers. */
  export interface TrackMapLabels {
    /** Accessible name of the map region. */
    region: string;
    zoomIn: string;
    zoomOut: string;
    fit: string;
    follow: string;
    start: string;
    last: string;
    /** Shown on the map when no tile server is configured. */
    noTiles: string;
  }
</script>

<script lang="ts">
  import type * as Leaflet from 'leaflet';
  import { untrack } from 'svelte';

  import Icon from '../icons/Icon.svelte';

  import type { MapTiles } from './MowerMap.svelte';

  type LeafletModule = typeof import('leaflet');

  interface Props {
    /** The recorded track, oldest first. */
    track: readonly TrackMapPoint[];
    /** The newest position (defaults to the last point of the track). */
    last?: TrackMapPoint | null;
    /** The mower has not reported for a while: the last position is drawn hollow. */
    stale?: boolean;
    tiles?: MapTiles | null;
    labels: TrackMapLabels;
    /** Keep the last position centred (bindable; a drag or the fit control ends it). */
    follow?: boolean;
    onfail?: (error: unknown) => void;
    testid?: string;
  }

  let {
    track,
    last = null,
    stale = false,
    tiles = null,
    labels,
    follow = $bindable(false),
    onfail,
    testid = 'track-map',
  }: Props = $props();

  let container: HTMLDivElement | undefined = $state();
  let L: LeafletModule | null = $state.raw(null);
  /** Bumped when a Leaflet map is (re)created, so the drawing effect redraws. */
  let generation = $state(0);

  let lmap: Leaflet.Map | null = null;
  let layer: Leaflet.LayerGroup | null = null;
  /** Fit the whole track on every change until the user pans or zooms. */
  let autoFit = true;

  const head = $derived(last ?? (track.length ? track[track.length - 1]! : null));

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
        minZoom: 2,
        maxZoom: 22,
        fadeAnimation: false,
        center: [0, 0],
        zoom: 2,
      });
    } catch (error) {
      onfail?.(error);
      return;
    }
    if (currentTiles?.url) {
      lib
        .tileLayer(currentTiles.url, {
          maxZoom: 22,
          maxNativeZoom: currentTiles.maxZoom ?? 19,
          className: 'md-map-tiles',
        })
        .addTo(instance);
    }
    instance.createPane('md-track').style.zIndex = '450';
    // Only a drag of the user ends the automatic view; our own fit and pan do not.
    instance.on('dragstart', () => {
      follow = false;
      autoFit = false;
    });
    layer = lib.layerGroup().addTo(instance);
    lmap = instance;
    autoFit = true;
    generation += 1;
    return () => {
      instance.remove();
      lmap = null;
      layer = null;
    };
  });

  function dot(lib: LeafletModule, kind: 'start' | 'last', hollow: boolean, name: string) {
    const safe = name.replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);
    return lib.divIcon({
      className: 'md-track-icon',
      html: `<span class="md-track-dot ${kind}${hollow ? ' hollow' : ''}" role="img" aria-label="${safe}"></span>`,
      iconSize: [22, 22],
      iconAnchor: [11, 11],
    });
  }
  // Draw the track and the markers; fit or follow after every change.
  $effect(() => {
    void generation;
    const lib = L;
    const instance = lmap;
    const group = layer;
    const points = track;
    const current = head;
    const hollow = stale;
    const startLabel = labels.start;
    const lastLabel = labels.last;
    if (!lib || !instance || !group) return;
    group.clearLayers();
    const latlngs = points.map((p): Leaflet.LatLngTuple => [p.lat, p.lon]);
    if (latlngs.length > 1) {
      lib
        .polyline(latlngs, {
          pane: 'md-track',
          color: '#ffffff',
          weight: 7,
          opacity: 0.85,
          lineJoin: 'round',
          interactive: false,
        })
        .addTo(group);
      lib
        .polyline(latlngs, {
          pane: 'md-track',
          color: '#b3261e',
          weight: 4,
          lineJoin: 'round',
          interactive: false,
        })
        .addTo(group);
    }
    if (points.length) {
      lib
        .marker([points[0]!.lat, points[0]!.lon], {
          icon: dot(lib, 'start', false, startLabel),
          pane: 'md-track',
          keyboard: false,
          interactive: false,
        })
        .addTo(group);
    }
    if (current) {
      lib
        .marker([current.lat, current.lon], {
          icon: dot(lib, 'last', hollow, lastLabel),
          pane: 'md-track',
          keyboard: false,
          interactive: false,
          zIndexOffset: 1000,
        })
        .addTo(group);
    }
    untrack(() => {
      if (!current) return;
      if (follow) {
        instance.setView([current.lat, current.lon], Math.max(instance.getZoom(), 17), {
          animate: false,
        });
      } else if (autoFit) {
        fit(instance, lib, latlngs.length ? latlngs : [[current.lat, current.lon]]);
      }
    });
  });

  function fit(
    instance: Leaflet.Map,
    lib: LeafletModule,
    latlngs: readonly Leaflet.LatLngTuple[],
  ): void {
    if (latlngs.length === 1) {
      instance.setView(latlngs[0]!, 18, { animate: false });
      return;
    }
    instance.fitBounds(lib.latLngBounds(latlngs as Leaflet.LatLngTuple[]), {
      padding: [48, 48],
      maxZoom: 19,
      animate: false,
    });
  }

  function refit(): void {
    follow = false;
    autoFit = true;
    const lib = L;
    if (!lib || !lmap) return;
    const latlngs = track.map((p): Leaflet.LatLngTuple => [p.lat, p.lon]);
    const current = head;
    if (latlngs.length) fit(lmap, lib, latlngs);
    else if (current) fit(lmap, lib, [[current.lat, current.lon]]);
  }

  function zoom(delta: number): void {
    autoFit = false;
    lmap?.setZoom((lmap.getZoom() ?? 2) + delta);
  }

  function toggleFollow(): void {
    follow = !follow;
    if (follow && head && lmap) {
      lmap.setView([head.lat, head.lon], Math.max(lmap.getZoom(), 17), { animate: false });
    }
  }
</script>

<div class="md-track-map" data-testid={testid}>
  <div
    class="canvas"
    bind:this={container}
    role="application"
    aria-label={labels.region}
    onwheel={() => (autoFit = false)}
  ></div>
  <div class="controls">
    <button type="button" aria-label={labels.zoomIn} onclick={() => zoom(1)}>
      <Icon name="plus" size={20} />
    </button>
    <button type="button" aria-label={labels.zoomOut} onclick={() => zoom(-1)}>
      <Icon name="minus" size={20} />
    </button>
    <button type="button" aria-label={labels.fit} onclick={refit}>
      <Icon name="fit" size={20} />
    </button>
    <button
      type="button"
      class="follow"
      aria-label={labels.follow}
      aria-pressed={follow}
      onclick={toggleFollow}
    >
      <Icon name="crosshair" size={20} />
    </button>
  </div>
  {#if tiles?.url}
    {#if tiles.attribution}<p class="note attribution">{tiles.attribution}</p>{/if}
  {:else}
    <p class="note">{labels.noTiles}</p>
  {/if}
</div>

<style>
  .md-track-map {
    position: relative;
    inline-size: 100%;
    block-size: 100%;
    min-block-size: 240px;
    overflow: hidden;
    background: var(--md-color-surface-sunken);
  }

  .canvas {
    position: absolute;
    inset: 0;
    background: var(--md-color-surface-sunken);
  }

  .controls {
    position: absolute;
    inset-inline-end: var(--md-space-3);
    inset-block-end: var(--md-space-6);
    z-index: 500;
    display: grid;
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-3);
    background: var(--md-color-surface);
    box-shadow: var(--md-shadow-float);
    overflow: hidden;
  }

  .controls button {
    display: grid;
    place-items: center;
    inline-size: var(--md-target);
    block-size: var(--md-target);
    border: 0;
    border-block-start: var(--md-hairline) solid var(--md-color-line);
    background: none;
    color: var(--md-color-ink);
    cursor: pointer;
  }

  .controls button:first-child {
    border-block-start: 0;
  }

  .controls button[aria-pressed='true'] {
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .controls button:focus-visible {
    outline: 3px solid var(--md-color-focus);
    outline-offset: -3px;
  }

  .note {
    position: absolute;
    inset-inline-start: var(--md-space-2);
    inset-block-end: var(--md-space-1);
    z-index: 500;
    margin: 0;
    padding: 0 var(--md-space-1);
    border-radius: var(--md-radius-2);
    background: var(--md-hud-glass);
    color: var(--md-hud-ink);
    font-size: var(--md-font-size-11);
    line-height: var(--md-line-16);
    max-inline-size: calc(100% - 96px);
  }

  .attribution {
    font-size: var(--md-font-size-11);
  }

  :global(.md-track-icon) {
    background: none;
    border: 0;
  }

  :global(.md-track-dot) {
    position: relative;
    display: block;
    inline-size: 22px;
    block-size: 22px;
    box-sizing: border-box;
    border: 3px solid var(--md-hud-ink);
    border-radius: var(--md-radius-round);
    box-shadow: var(--md-shadow-float);
  }

  :global(.md-track-dot.start) {
    inline-size: 16px;
    block-size: 16px;
    margin: 3px;
    background: var(--md-state-mowing);
  }

  :global(.md-track-dot.last) {
    background: var(--md-stop-fill);
  }

  :global(.md-track-dot.last.hollow) {
    background: var(--md-color-surface);
    border-color: var(--md-state-offline);
  }

  :global(.md-track-dot.last:not(.hollow))::after {
    content: '';
    position: absolute;
    inset: -9px;
    border: 2px solid var(--md-stop-fill);
    border-radius: var(--md-radius-round);
    opacity: 0;
    animation: md-track-pulse 1.8s var(--md-ease) infinite;
  }

  @keyframes md-track-pulse {
    0% {
      transform: scale(0.5);
      opacity: 0.8;
    }

    100% {
      transform: scale(1.5);
      opacity: 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    :global(.md-track-dot.last:not(.hollow))::after {
      animation: none;
      opacity: 0.45;
      transform: none;
    }
  }
</style>
