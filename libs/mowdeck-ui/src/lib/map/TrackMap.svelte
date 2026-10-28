<!--
  The map of theft tracking: a recorded track on a world map (EPSG:3857), the start (green) and
  the last known position (red and pulsing, hollow grey while the mower is silent). It is not
  tied to the mower's own map: the mower may be anywhere, so positions are plain latitude and
  longitude.

  Background, first that applies: `layers` (base maps served by MowDeck itself around the track,
  street map and aerial photo, loaded through `load` so the page can add a token; a switch on the
  map picks one, `layer` is bindable), else the installation's raster tile server (`tiles`), else
  a plain grid backdrop with `labels.noTiles`.

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

  /** A base map under the track, loaded tile by tile through `load`. */
  export interface TrackMapLayer {
    id: string;
    /** Name on the layer switch. */
    label: string;
    /** URL template with `{z}`, `{x}`, `{y}`; handed to `load` filled in. */
    url: string;
    attribution?: string | null;
    minZoom?: number;
    /** Highest zoom with real detail; deeper zooms scale the last level. */
    maxZoom?: number;
    load: (url: string, signal: AbortSignal) => Promise<Blob>;
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
    /** Shown on the map when no base map is available. */
    noTiles: string;
    /** Accessible name of the layer switch. */
    layers?: string;
  }
</script>

<script lang="ts">
  import type * as Leaflet from 'leaflet';
  import { untrack } from 'svelte';

  import Icon from '../icons/Icon.svelte';

  import { createAerialLayer } from './aerial';
  import type { MapTiles } from './MowerMap.svelte';

  type LeafletModule = typeof import('leaflet');

  interface Props {
    /** The recorded track, oldest first. */
    track: readonly TrackMapPoint[];
    /** The newest position (defaults to the last point of the track). */
    last?: TrackMapPoint | null;
    /** The mower has not reported for a while: the last position is drawn hollow. */
    stale?: boolean;
    /** Base maps served around the track (take precedence over `tiles`). */
    layers?: readonly TrackMapLayer[];
    /** Id of the shown base map (bindable; the first one when unset or unknown). */
    layer?: string | null;
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
    layers = [],
    layer = $bindable(null),
    tiles = null,
    labels,
    follow = $bindable(false),
    onfail,
    testid = 'track-map',
  }: Props = $props();

  let container: HTMLDivElement | undefined = $state();
  let L: LeafletModule | null = $state.raw(null);
  /** Bumped when a Leaflet map is (re)created, so the drawing effects run on the new map. */
  let generation = $state(0);

  let lmap: Leaflet.Map | null = null;
  let group: Leaflet.LayerGroup | null = null;
  /** Fit the whole track on every change until the user pans or zooms. */
  let autoFit = true;

  const head = $derived(last ?? (track.length ? track[track.length - 1]! : null));
  const chosen = $derived(layers.find((entry) => entry.id === layer) ?? layers[0] ?? null);
  const chosenKey = $derived(chosen ? `${chosen.id} ${chosen.url}` : null);
  /** Browser side tiles only without MowDeck's own base maps. */
  const rasterUrl = $derived(chosen ? null : (tiles?.url ?? null));
  const attribution = $derived(
    chosen ? (chosen.attribution ?? null) : rasterUrl ? (tiles?.attribution ?? null) : null,
  );
  const bare = $derived(!chosen && !rasterUrl);

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

  // The map itself: one per Leaflet load and container. The background is a separate effect, so
  // switching it (or the tile settings arriving late) never replaces the map and its drawing.
  $effect(() => {
    const lib = L;
    const el = container;
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
    instance.createPane('md-track').style.zIndex = '450';
    // Only a drag of the user ends the automatic view; our own fit and pan do not.
    instance.on('dragstart', () => {
      follow = false;
      autoFit = false;
    });
    group = lib.layerGroup().addTo(instance);
    lmap = instance;
    autoFit = true;
    // untrack: reading generation here would make this effect depend on its own write.
    generation = untrack(() => generation) + 1;
    const resize =
      typeof ResizeObserver === 'undefined'
        ? null
        : new ResizeObserver(() => {
            instance.invalidateSize({ pan: false });
            if (autoFit && !untrack(() => follow)) untrack(refit);
          });
    resize?.observe(el);
    return () => {
      resize?.disconnect();
      instance.remove();
      lmap = null;
      group = null;
    };
  });

  // The background: MowDeck's base map, else the browser side tiles, else nothing (the grid).
  // Keyed by id and URL: a page that hands in fresh layer objects on every poll keeps its tiles.
  $effect(() => {
    void generation;
    void chosenKey;
    const lib = L;
    const instance = lmap;
    const base = untrack(() => chosen);
    const raster = rasterUrl;
    if (!lib || !instance) return;
    let background: Leaflet.TileLayer | null = null;
    if (base) {
      background = createAerialLayer(
        lib,
        {
          url: base.url,
          opacity: 1,
          minZoom: base.minZoom ?? 0,
          maxZoom: base.maxZoom ?? 19,
          load: base.load,
        },
        () => untrack(() => chosen)?.load,
      );
    } else if (raster) {
      background = lib.tileLayer(raster, {
        maxZoom: 22,
        maxNativeZoom: untrack(() => tiles?.maxZoom) ?? 19,
        className: 'md-map-tiles',
      });
    }
    background?.addTo(instance);
    return () => {
      background?.remove();
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
    const target = group;
    const points = track;
    const current = head;
    const hollow = stale;
    const startLabel = labels.start;
    const lastLabel = labels.last;
    if (!lib || !instance || !target) return;
    target.clearLayers();
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
        .addTo(target);
      lib
        .polyline(latlngs, {
          pane: 'md-track',
          color: '#b3261e',
          weight: 4,
          lineJoin: 'round',
          interactive: false,
        })
        .addTo(target);
    }
    if (points.length) {
      lib
        .marker([points[0]!.lat, points[0]!.lon], {
          icon: dot(lib, 'start', false, startLabel),
          pane: 'md-track',
          keyboard: false,
          interactive: false,
        })
        .addTo(target);
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
        .addTo(target);
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
    const lib = L;
    if (!lib || !lmap) return;
    const latlngs = track.map((p): Leaflet.LatLngTuple => [p.lat, p.lon]);
    const current = head;
    if (latlngs.length) fit(lmap, lib, latlngs);
    else if (current) fit(lmap, lib, [[current.lat, current.lon]]);
  }

  function fitAll(): void {
    follow = false;
    autoFit = true;
    refit();
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

<div
  class="md-track-map"
  class:bare
  data-testid={testid}
  data-layer={chosen?.id ?? (rasterUrl ? 'tiles' : 'grid')}
>
  <div
    class="canvas"
    bind:this={container}
    role="application"
    aria-label={labels.region}
    onwheel={() => (autoFit = false)}
  ></div>
  {#if layers.length > 1}
    <div class="layers" role="radiogroup" aria-label={labels.layers ?? labels.region}>
      {#each layers as entry (entry.id)}
        <button
          type="button"
          role="radio"
          aria-checked={entry.id === chosen?.id}
          data-testid="track-map-layer-{entry.id}"
          onclick={() => (layer = entry.id)}>{entry.label}</button
        >
      {/each}
    </div>
  {/if}
  <div class="controls">
    <button type="button" aria-label={labels.zoomIn} onclick={() => zoom(1)}>
      <Icon name="plus" size={20} />
    </button>
    <button type="button" aria-label={labels.zoomOut} onclick={() => zoom(-1)}>
      <Icon name="minus" size={20} />
    </button>
    <button type="button" aria-label={labels.fit} onclick={fitAll}>
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
  {#if bare}
    <p class="note">{labels.noTiles}</p>
  {:else if attribution}
    <p class="note attribution">{attribution}</p>
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

  /* Without any base map: a survey grid, so the track never sits on a featureless (in dark mode
     black) plane. */
  .bare .canvas {
    background-color: var(--md-color-surface-sunken);
    background-image:
      linear-gradient(var(--md-color-line) 1px, transparent 1px),
      linear-gradient(90deg, var(--md-color-line) 1px, transparent 1px),
      linear-gradient(var(--md-color-line-strong) 1px, transparent 1px),
      linear-gradient(90deg, var(--md-color-line-strong) 1px, transparent 1px);
    background-size:
      24px 24px,
      24px 24px,
      120px 120px,
      120px 120px;
  }

  .layers {
    position: absolute;
    inset-block-start: var(--md-space-3);
    inset-inline-start: var(--md-space-3);
    z-index: 500;
    display: flex;
    gap: var(--md-space-0_5);
    padding: var(--md-space-0_5);
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-round);
    background: var(--md-color-surface);
    box-shadow: var(--md-shadow-float);
  }

  .layers button {
    min-block-size: 36px;
    padding: 0 var(--md-space-3);
    border: 0;
    border-radius: var(--md-radius-round);
    background: none;
    color: var(--md-color-ink-muted);
    font: inherit;
    font-size: var(--md-font-size-13);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    cursor: pointer;
  }

  .layers button[aria-checked='true'] {
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .layers button:focus-visible {
    outline: 3px solid var(--md-color-focus);
    outline-offset: 1px;
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
