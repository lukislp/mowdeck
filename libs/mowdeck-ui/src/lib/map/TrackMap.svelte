<!--
  The map of theft tracking: a recorded track on a world map (EPSG:3857), the start (green) and
  the last known position (red and pulsing, hollow grey while the mower is silent). It is not
  tied to the mower's own map: the mower may be anywhere, so positions are plain latitude and
  longitude.

  Background, first that applies: `layers` (base maps served by MowDeck itself around the track,
  street map and aerial photo, loaded through `load` so the page can add a token; a switch on the
  map picks one, `layer` is bindable), else the installation's raster tile server (`tiles`), else
  a plain grid backdrop with `labels.noTiles`. A layer without data at the position (`covered:
  false`, or a whole round of tiles failing) is skipped for the next one, with `labels.uncovered`.

  The view fits the track until the user pans or zooms in any way (drag, pinch, wheel, double
  tap, keys, the buttons); from then on new positions never change the view, until the fit
  control ("show the whole track") turns fitting back on. `follow` (bindable) keeps the last
  position in the centre at the user's zoom (pinching zooms around it); a drag ends it. A layer
  with a `margin` and/or `coverage` limits the view to where it serves tiles: no panning out of
  that box, no zooming out further than the box fills the view (`trackView.ts`), no zooming in
  beyond the layer's `maxZoom`. The limits follow a growing track and a layer switch without
  moving a view that is still valid. Leaflet is loaded lazily; `onfail` reports a failed load.
  The page owns the texts (`labels`).
-->
<script lang="ts" module>
  import type { GeoBox } from './trackView';

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
    /** Where the credit line links to (e.g. OpenStreetMap's copyright page). */
    attributionUrl?: string | null;
    minZoom?: number;
    /** Highest zoom with real detail; deeper zooms scale the last level. */
    maxZoom?: number;
    load: (url: string, signal: AbortSignal) => Promise<Blob>;
    /** False when the source has no data at the last position (then another layer is shown). */
    covered?: boolean;
    /** Metres around the positions where tiles are served (the view stays inside; 0: no limit). */
    margin?: number;
    /** `[south, west, north, east]`: where the source has data at all (null: everywhere). */
    coverage?: GeoBox | null;
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
    /** Shown when the chosen layer has no data here and another one stands in. */
    uncovered?: (layer: string) => string;
    /** Shown when base maps exist but none has data at the position. */
    noneHere?: string;
  }
</script>

<script lang="ts">
  import type * as Leaflet from 'leaflet';
  import { untrack } from 'svelte';

  import Icon from '../icons/Icon.svelte';

  import { createAerialLayer } from './aerial';
  import type { MapTiles } from './MowerMap.svelte';
  import { fillZoom, servedBox } from './trackView';

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
  /** Above zero while this component moves the map itself (any other move is the user's). */
  let ownMoves = 0;
  /** The background tile layer on the map (its request box follows the served area). */
  let shownTiles: Leaflet.TileLayer | null = null;
  /** The current zoom and limits, as data attributes (tests and e2e read them). */
  let zoomNow = $state<number | null>(null);
  let limits = $state.raw<{ min: number; max: number; box: GeoBox | null } | null>(null);

  const MIN_ZOOM = 2;
  const MAX_ZOOM = 22;
  const ZOOM_SNAP = 0.25;

  const head = $derived(last ?? (track.length ? track[track.length - 1]! : null));
  /** Layers whose tiles kept failing here (no single tile loaded): treated as uncovered. */
  let failed = $state.raw<ReadonlySet<string>>(new Set());
  const usable = $derived(
    layers.filter((entry) => entry.covered !== false && !failed.has(entry.id)),
  );
  const requested = $derived(layers.find((entry) => entry.id === layer) ?? layers[0] ?? null);
  // The chosen layer when it has data at the position, else the first one that has (a stolen
  // mower may be far outside an aerial photo of one state), else none (the grid).
  const chosen = $derived(
    requested && usable.includes(requested) ? requested : (usable[0] ?? null),
  );
  const fallbackFrom = $derived(requested && requested !== chosen ? requested : null);
  const chosenKey = $derived(chosen ? `${chosen.id} ${chosen.url}` : null);
  /** Browser side tiles only without MowDeck's own base maps. */
  const rasterUrl = $derived(chosen ? null : (tiles?.url ?? null));
  const attribution = $derived(
    chosen ? (chosen.attribution ?? null) : rasterUrl ? (tiles?.attribution ?? null) : null,
  );
  const attributionUrl = $derived(chosen?.attributionUrl ?? null);
  const bare = $derived(!chosen && !rasterUrl);
  /** Where the shown layer serves tiles: positions plus margin, cut to its coverage. */
  const served = $derived.by(() => {
    if (!chosen) return null;
    const points = head ? [...track, head] : track;
    return servedBox(points, chosen.margin, chosen.coverage);
  });
  const servedKey = $derived(served ? served.map((v) => v.toFixed(6)).join(',') : '');

  /** Run a map change of this component, so its move events are not taken for the user's. */
  function own(action: () => void): void {
    ownMoves += 1;
    try {
      action();
    } finally {
      ownMoves -= 1;
    }
  }

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
        // Smooth zooming: quarter steps for pinch and wheel, half steps for the buttons, a
        // gentler wheel, a hard stop at the limits (no rubber band past the served area).
        zoomSnap: ZOOM_SNAP,
        zoomDelta: 0.5,
        wheelPxPerZoomLevel: 90,
        bounceAtZoomLimits: false,
        maxBoundsViscosity: 1,
        minZoom: MIN_ZOOM,
        maxZoom: MAX_ZOOM,
        fadeAnimation: false,
        center: [0, 0],
        zoom: 2,
      });
    } catch (error) {
      onfail?.(error);
      return;
    }
    instance.createPane('md-track').style.zIndex = '450';
    // Any move that is not this component's own (drag, pinch, wheel, double tap, keys) is the
    // user's: from then on new positions never change the view. Pinching fires no drag and no
    // wheel event, which is why the view used to jump back to the whole track on the next poll.
    const byUser = () => {
      if (ownMoves === 0) autoFit = false;
    };
    instance.on('movestart', byUser);
    instance.on('zoomstart', byUser);
    // Only a drag ends following; a pinch zooms around the followed position.
    instance.on('dragstart', () => {
      follow = false;
      autoFit = false;
    });
    instance.on('zoomend', () => (zoomNow = instance.getZoom()));
    group = lib.layerGroup().addTo(instance);
    lmap = instance;
    autoFit = true;
    // untrack: reading generation here would make this effect depend on its own write.
    generation = untrack(() => generation) + 1;
    const resize =
      typeof ResizeObserver === 'undefined'
        ? null
        : new ResizeObserver(() => {
            // A phone's address bar or keyboard coming and going resizes the map: keep the
            // user's view, only re-apply the limits (they depend on the size).
            own(() => instance.invalidateSize({ pan: false }));
            untrack(applyLimits);
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
      const id = base.id;
      let loaded = 0;
      let errors = 0;
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
      background.on('tileload', () => (loaded += 1));
      const giveUp = () => (failed = new Set([...untrack(() => failed), id]));
      background.on('tileerror', () => {
        errors += 1;
        if (errors >= 6 && loaded === 0) giveUp();
      });
      // A whole round of tiles in view without a single image: no data here (or the source is
      // down); the next layer stands in.
      background.on('load', () => {
        if (errors > 0 && loaded === 0) giveUp();
      });
    } else if (raster) {
      background = lib.tileLayer(raster, {
        maxZoom: 22,
        maxNativeZoom: untrack(() => tiles?.maxZoom) ?? 19,
        className: 'md-map-tiles',
      });
    }
    if (background) untrack(() => clipTiles(lib, background!, served));
    background?.addTo(instance);
    shownTiles = background;
    return () => {
      background?.remove();
      if (shownTiles === background) shownTiles = null;
    };
  });

  // The view limits of the shown layer. Leaflet moves the view only when it is outside the new
  // limits (a zoom below the new minimum, a centre outside the box); a valid view stays.
  $effect(() => {
    void generation;
    void servedKey;
    void chosenKey;
    void chosen?.minZoom;
    void chosen?.maxZoom;
    untrack(applyLimits);
  });

  function applyLimits(): void {
    const lib = L;
    const instance = lmap;
    if (!lib || !instance) return;
    const layer = chosen;
    const box = served;
    let min = MIN_ZOOM;
    let max = MAX_ZOOM;
    if (layer) {
      max = layer.maxZoom ?? 19;
      min = layer.minZoom ?? MIN_ZOOM;
      if (box) {
        const size = instance.getSize();
        // Leaflet snaps to whole zooms without 3D transforms; the limit must be a snap level.
        const fill = fillZoom(box, size.x, size.y, lib.Browser.any3d ? ZOOM_SNAP : 1);
        if (fill !== null) min = Math.max(min, fill);
      }
      min = Math.min(min, max);
    }
    const bounds = box ? lib.latLngBounds([box[0], box[1]], [box[2], box[3]]) : null;
    own(() => {
      instance.setMaxZoom(max);
      instance.setMinZoom(min);
      // Leaflet's typings lack the documented null that removes the box.
      instance.setMaxBounds(bounds as Leaflet.LatLngBounds);
    });
    if (shownTiles) clipTiles(lib, shownTiles, layer ? box : null);
    limits = { min, max, box };
    zoomNow = instance.getZoom();
  }

  /** Ask only for tiles inside the served box (Leaflet reads the option per tile). */
  function clipTiles(lib: LeafletModule, tiles: Leaflet.TileLayer, box: GeoBox | null): void {
    tiles.options.bounds = box ? lib.latLngBounds([box[0], box[1]], [box[2], box[3]]) : undefined;
  }

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
        // Follow at the user's zoom: only the centre moves.
        own(() =>
          instance.setView([current.lat, current.lon], instance.getZoom(), { animate: false }),
        );
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
    own(() => {
      if (latlngs.length === 1) {
        instance.setView(latlngs[0]!, 18, { animate: false });
        return;
      }
      instance.fitBounds(lib.latLngBounds(latlngs as Leaflet.LatLngTuple[]), {
        padding: [48, 48],
        maxZoom: 19,
        animate: false,
      });
    });
    zoomNow = instance.getZoom();
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
    const map = lmap;
    if (follow && head && map) {
      own(() => map.setView([head.lat, head.lon], Math.max(map.getZoom(), 17), { animate: false }));
      zoomNow = map.getZoom();
    }
  }

  // While following, pinch, wheel and double tap zoom around the followed position, so a zoom
  // never pulls it off centre (Leaflet reads these options at the start of every gesture).
  $effect(() => {
    void generation;
    const map = lmap;
    if (!map) return;
    const around = follow ? 'center' : true;
    map.options.touchZoom = around;
    map.options.scrollWheelZoom = around;
    map.options.doubleClickZoom = around;
  });
</script>

<div
  class="md-track-map"
  class:bare
  data-testid={testid}
  data-layer={chosen?.id ?? (rasterUrl ? 'tiles' : 'grid')}
  data-zoom={zoomNow ?? undefined}
  data-min-zoom={limits?.min}
  data-max-zoom={limits?.max}
  data-bounds={limits?.box ? limits.box.map((v) => v.toFixed(5)).join(',') : undefined}
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
          disabled={!usable.includes(entry)}
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
  {#if fallbackFrom && labels.uncovered}
    <p class="hint" role="status" data-testid="track-map-uncovered">
      {labels.uncovered(fallbackFrom.label)}
    </p>
  {/if}
  {#if bare}
    <p class="note" data-testid="track-map-bare">
      {layers.length ? (labels.noneHere ?? labels.noTiles) : labels.noTiles}
    </p>
  {:else if attribution}
    <p class="note attribution">
      {#if attributionUrl}
        <a href={attributionUrl} target="_blank" rel="noopener noreferrer">{attribution}</a>
      {:else}
        {attribution}
      {/if}
    </p>
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

    /* All gestures on the map belong to Leaflet: no page scroll, page zoom or pull to refresh
       fighting a pinch. */
    touch-action: none;
    overscroll-behavior: contain;
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

  .layers button:disabled {
    opacity: 0.45;
    cursor: default;
    text-decoration: line-through;
  }

  .hint {
    position: absolute;
    inset-block-start: calc(var(--md-space-3) + 48px);
    inset-inline: var(--md-space-3);
    z-index: 500;
    max-inline-size: 36ch;
    margin: 0;
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-start: 4px solid var(--md-state-paused);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
    box-shadow: var(--md-shadow-float);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-13);
  }

  .attribution a {
    color: inherit;
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
