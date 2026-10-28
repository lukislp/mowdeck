<!--
  The mower map of the instrument panel, on Leaflet 1.9: areas with uppercase labels, hatched
  no-go zones, obstacles, dashed transfer paths, the dock (a 1.2 x 0.9 m footprint drawn to scale,
  open side towards its heading, with a fixed-size bolt glyph on top), the mower chevron in its state colour
  and the driven route fading with age (optionally tinted by RTK quality), plus the RTK signal
  map: the route's points binned into metric cells coloured by their fix (`rtk.ts`).

  Heat layers (`heat`, see `heat.ts`): cells answered by Telemetry's heatmap endpoint (coverage,
  RTK fixed share, WLAN or mobile signal), drawn as rectangles in their own pane below the areas
  so the area outlines stay crisp, plus the thin outlines of missed spots and one highlighted
  spot (`highlightGap`). The page owns the layer choice, period and legend; with `heat` given
  (even `null`) the old client-side RTK toggle leaves the layer panel. The `rtk*` props and
  `showRtk` are deprecated: they stay for callers that still bin route points in the browser.

  Without tiles it draws in the mower's local metres (`CRS.Simple`) over a metric grid and
  sends no request to any third party; with `tiles` it switches to EPSG:3857 and converts the
  local metres to WGS84 (see `projection.ts`). What is drawn comes from the pure layer model
  (`layers.ts`); this component only maps descriptors to Leaflet layers. Leaflet is loaded
  lazily; `onfail` reports a failed load so the page can fall back to a schematic plan.

  The mower's planned mow path (`path`, see `mow-path.ts`): a thin line in the accent colour,
  the part already mowed (up to `progress.index`) solid and the part ahead dimmed, in its own
  pane below the driven route, with a legend entry; `pathPreview` captions it as a preview.

  The spot of a spot-mowing run (`spot`, see `spot.ts`): a circle around a tapped point or a
  drawn polygon, in its own pane between the mow path and the route, with the corners placed so
  far while a polygon is still being drawn. With `onmaptap` a tap on the map (not on a control)
  reports its local metres, so the page can place or extend the spot; the cursor says so.

  `insetTop` (px) is the part covered at the top left by a page overlay (a chip): the legend moves
  below it and the fit and the spot keep clear of it. `insetBottom` (px) is the part of the map that something covers at its bottom (a bottom sheet
  on phones): the fit pads by it, the scale, the lower legend and the attribution move above it,
  and the spot with its route preview is panned (or, when larger, fitted) into the part above it
  whenever the spot, the preview or the inset changes (`viewport.ts`).

  Texts arrive translated in `labels`; area selection is reported with `onselectionchange`.
  With `onarealabel` each area label gets a small edit button (rename an area from the map).
-->
<script lang="ts" module>
  /** Translated texts of the map controls. */
  export interface MowerMapLabels {
    /** Accessible name of the map region. */
    region: string;
    zoomIn: string;
    zoomOut: string;
    fit: string;
    follow: string;
    layers: string;
    route: string;
    zones: string;
    rtkTint: string;
    expand: string;
    collapse: string;
    /** The letter of the north arrow. */
    north: string;
    dock: string;
    mower: string;
    rtkFixed: string;
    rtkFloat: string;
    rtkNone: string;
    /** The RTK signal map layer (cells coloured by fix quality). */
    rtkLayer: string;
    /** Legend title of the RTK signal map. */
    rtkLegend: string;
    rtkUnknown: string;
    /** Label of the cell size choice. */
    rtkCellSize: string;
    /** A cell size in metres, formatted for the locale (e.g. "0,5 m"). */
    rtkCell: (metres: number) => string;
    /** Label of the aggregate choice, and its two options. */
    rtkAggregate: string;
    rtkWorst: string;
    rtkMajority: string;
    /** Accessible name of a selectable area, given its name. */
    selectArea: (name: string) => string;
    /** Accessible name of the edit button beside an area label, given the area's name. */
    renameArea?: (name: string) => string;
    /** Legend of the planned mow path: its title, the mowed part, the part ahead, and the
     * caption of a preview (a route planned without a start). */
    path?: string;
    pathDone?: string;
    pathAhead?: string;
    pathPreview?: string;
    /** Legend entry of the spot of a spot-mowing run. */
    spot?: string;
  }

  /** An optional raster tile layer (from the installation's runtime config). */
  export interface MapTiles {
    /** URL template with `{z}`, `{x}`, `{y}` and optionally `{s}`. */
    url: string;
    attribution?: string | null;
    maxZoom?: number | null;
  }

  import type { MapAerial } from './aerial';
</script>

<script lang="ts">
  import type * as Leaflet from 'leaflet';
  import { untrack } from 'svelte';

  import Icon, { ICON_PATHS } from '../icons/Icon.svelte';

  import {
    buildLayers,
    routeSegments,
    toggleSelection,
    type MapMowerState,
    type MapSnapshot,
    type RoutePoint,
  } from './layers';
  import { createAerialLayer } from './aerial';
  import { createGridLayer } from './grid';
  import { createProjection, type MapCoord } from './projection';
  import { RTK_CELL_SIZES, binRtk, cellRing, type RtkAggregate, type RtkCellSize } from './rtk';
  import { gapOutlines, heatCells, heatRing, type HeatGap, type HeatLayerData } from './heat';
  import { pathLayers, type MapMowPath } from './mow-path';
  import { spotLayer, type MapSpot } from './spot';
  import { scaleFor } from './scale';
  import { pixelBox, revealPan } from './viewport';

  type LeafletModule = typeof import('leaflet');

  interface Props {
    map: MapSnapshot;
    state?: MapMowerState | null;
    route?: readonly RoutePoint[];
    /**
     * The heat layer to draw (cells from Telemetry's heatmap), or null for none. Leaving it
     * undefined keeps the deprecated client-side RTK signal map in the layer panel.
     */
    heat?: HeatLayerData | null;
    /** One missed spot (local metres) to outline strongly and bring into view. */
    highlightGap?: HeatGap | null;
    /** The mower's planned mow path (local metres) with its progress, or null for none. */
    path?: MapMowPath | null;
    /** The path is a preview (planned without a start): the legend says so. */
    pathPreview?: boolean;
    /** The spot of a spot-mowing run (local metres), or null for none. */
    spot?: MapSpot | null;
    /** A tap on the map (its local metres); given, the cursor becomes a crosshair. */
    onmaptap?: (local: [number, number]) => void;
    /**
     * Points of the RTK signal map (defaults to `route`), e.g. a longer time range.
     * @deprecated Use `heat` with the `rtk` layer of the heatmap endpoint.
     */
    rtkPoints?: readonly RoutePoint[] | null;
    /**
     * The RTK signal map layer is on (bindable; also toggled in the layer panel).
     * @deprecated Use `heat`.
     */
    showRtk?: boolean;
    /** Cell edge of the RTK signal map in metres (bindable). @deprecated Use `heat`. */
    rtkCellSize?: RtkCellSize;
    /** How a cell's class is chosen (bindable). @deprecated Use `heat`. */
    rtkAggregate?: RtkAggregate;
    selectedAreas?: readonly string[];
    /** Tapping an area toggles it in the selection. */
    selectable?: boolean;
    /** The mower has no live link (offline, ADR 0040; the chevron is drawn hollow). */
    stale?: boolean;
    tiles?: MapTiles | null;
    /** An aerial photo under the map (switches the map to geographic mode). */
    aerial?: MapAerial | null;
    labels: MowerMapLabels;
    areaName: (area: { id: string; name?: string | null }) => string;
    onselectionchange?: (ids: string[]) => void;
    /** A tap on the edit button beside an area label (shown only with this), e.g. to rename. */
    onarealabel?: (id: string) => void;
    /** Leaflet could not be loaded or started. */
    onfail?: (error: unknown) => void;
    /** Full-screen state; the expand control is shown when `onexpandchange` is given. */
    expanded?: boolean;
    onexpandchange?: (expanded: boolean) => void;
    /** Keep the mower centred (bindable; a drag or the fit control ends it). */
    follow?: boolean;
    /** Pixels covered at the bottom of the map (a bottom sheet); content is kept above them. */
    insetBottom?: number;
    /** Pixels covered at the top of the map (a chip); the legend and content stay below them. */
    insetTop?: number;
    testid?: string;
  }

  let {
    map,
    state: mowerState = null,
    route = [],
    heat = undefined,
    highlightGap = null,
    path = null,
    pathPreview = false,
    spot = null,
    onmaptap,
    rtkPoints = null,
    showRtk = $bindable(false),
    rtkCellSize = $bindable(1),
    rtkAggregate = $bindable('worst'),
    selectedAreas = [],
    selectable = false,
    stale = false,
    tiles = null,
    aerial = null,
    labels,
    areaName,
    onselectionchange,
    onarealabel,
    onfail,
    expanded = false,
    onexpandchange,
    follow = $bindable(false),
    insetBottom = 0,
    insetTop = 0,
    testid = 'mower-map',
  }: Props = $props();

  const uid = $props.id();
  const hatchId = `md-hatch-${uid}`;

  let container: HTMLDivElement | undefined = $state();
  let L: LeafletModule | null = $state.raw(null);
  /** Bumped when a Leaflet map is (re)created, so the layer effects redraw. */
  let generation = $state(0);
  let showRoute = $state(true);
  let showZones = $state(true);
  let rtkTint = $state(false);
  let layersOpen = $state(false);
  let scale = $state({ width: 0, label: '' });

  // Leaflet objects are not reactive state: effects own them.
  let lmap: Leaflet.Map | null = null;
  let groups: Record<
    | 'heat'
    | 'gaps'
    | 'rtk'
    | 'areas'
    | 'zones'
    | 'paths'
    | 'mowpath'
    | 'spot'
    | 'route'
    | 'labels'
    | 'dock',
    Leaflet.LayerGroup
  > | null = null;
  let areaPolygons = new Map<string, Leaflet.Polygon>();
  let mowerMarker: Leaflet.Marker | null = null;
  let fitted = false;
  /** Refit on resize until the user pans or zooms (the fit control turns it back on). */
  let autoFit = true;

  /** The layer is rebuilt only when the source changes; opacity is updated in place. */
  const aerialKey = $derived(aerial?.url ?? null);
  const mode = $derived(tiles?.url || aerialKey ? 'geo' : 'local');
  let aerialLayer: Leaflet.TileLayer | null = null;
  const projection = $derived(createProjection(map.reference, mode));
  const geometry = $derived(buildLayers({ map, projection, areaName }));
  const mower = $derived(
    buildLayers({ map: null, state: mowerState, projection, areaName, stale }).mower,
  );
  const routeModel = $derived(routeSegments(route, projection, { rtkTint }));
  const rtkCells = $derived(
    showRtk
      ? binRtk(rtkPoints ?? route, map.reference, { size: rtkCellSize, aggregate: rtkAggregate })
      : [],
  );
  const rtkUnknownShown = $derived(rtkCells.some((cell) => cell.rtk === 'unknown'));
  /** The page drives the heat layers; the legacy RTK toggle leaves the layer panel. */
  const heatMode = $derived(heat !== undefined);
  const heatRects = $derived(heatCells(heat));
  const gaps = $derived(gapOutlines(heat?.gaps, projection));
  const highlighted = $derived(
    highlightGap ? (gapOutlines([highlightGap], projection)[0] ?? null) : null,
  );
  const pathPieces = $derived(pathLayers(path, projection));
  const spotDrawn = $derived(spotLayer(spot, projection));
  const pathHasProgress = $derived(!!path?.progress && pathPieces.some((piece) => piece.done));

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
    const currentMode = mode;
    const currentTiles = tiles;
    const currentAerial = aerialKey ? untrack(() => aerial) : null;
    if (!lib || !el) return;
    let instance: Leaflet.Map;
    try {
      instance = lib.map(el, {
        crs: currentMode === 'local' ? lib.CRS.Simple : lib.CRS.EPSG3857,
        zoomControl: false,
        attributionControl: false,
        boxZoom: false,
        zoomSnap: 0.25,
        zoomDelta: 0.5,
        wheelPxPerZoomLevel: 120,
        minZoom: currentMode === 'local' ? -3 : 3,
        maxZoom: currentMode === 'local' ? 8 : 22,
        fadeAnimation: false,
      });
    } catch (error) {
      onfail?.(error);
      return;
    }
    for (const [name, z] of [
      ['md-grid', 210],
      ['md-heat', 380],
      ['md-rtk', 390],
      ['md-gap', 440],
      ['md-mowpath', 445],
      ['md-spot', 447],
      ['md-route', 450],
      ['md-mower', 650],
    ] as const) {
      instance.createPane(name).style.zIndex = String(z);
    }
    if (currentMode === 'geo' && currentTiles?.url) {
      lib
        .tileLayer(currentTiles.url, {
          maxZoom: 22,
          maxNativeZoom: currentTiles.maxZoom ?? 19,
          className: 'md-map-tiles',
        })
        .addTo(instance);
    }
    if (currentMode === 'geo' && currentAerial) {
      aerialLayer = createAerialLayer(lib, currentAerial, () => aerial?.load).addTo(instance);
    }
    // The metric grid is a tile layer, so it follows any pan and zoom (local projection only).
    let themeWatch: (() => void) | null = null;
    if (currentMode === 'local') {
      const grid = createGridLayer(lib, { pane: 'md-grid', host: el }).addTo(instance);
      const redraw = () => grid.redraw();
      const media = window.matchMedia('(prefers-color-scheme: dark)');
      const attrs = new MutationObserver(redraw);
      attrs.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ['data-theme'],
      });
      media.addEventListener('change', redraw);
      themeWatch = () => {
        attrs.disconnect();
        media.removeEventListener('change', redraw);
      };
    }
    groups = {
      heat: lib.layerGroup().addTo(instance),
      gaps: lib.layerGroup().addTo(instance),
      rtk: lib.layerGroup().addTo(instance),
      areas: lib.layerGroup().addTo(instance),
      zones: lib.layerGroup().addTo(instance),
      paths: lib.layerGroup().addTo(instance),
      mowpath: lib.layerGroup().addTo(instance),
      spot: lib.layerGroup().addTo(instance),
      route: lib.layerGroup().addTo(instance),
      labels: lib.layerGroup().addTo(instance),
      dock: lib.layerGroup().addTo(instance),
    };
    const onView = () => {
      scale = scaleBar(instance);
    };
    instance.on('zoomend moveend', onView);
    const manual = () => {
      autoFit = false;
    };
    instance.on('dragstart', () => {
      follow = false;
      autoFit = false;
    });
    // A tap on the map itself (areas are not interactive while a spot is placed, so it reaches
    // the map); the handler reads the current prop, so it needs no re-registration.
    instance.on('click', (event: Leaflet.LeafletMouseEvent) => {
      const report = onmaptap;
      if (!report) return;
      const [x, y] = projection.toLocal([event.latlng.lat, event.latlng.lng]);
      report([Math.round(x * 100) / 100, Math.round(y * 100) / 100]);
    });
    instance.setView([0, 0], 3);
    lmap = instance;
    fitted = false;
    untrack(() => (generation += 1));
    for (const type of ['wheel', 'dblclick', 'keydown'] as const) el.addEventListener(type, manual);
    el.addEventListener('touchstart', (event) => {
      if (event.touches.length > 1) autoFit = false;
    });
    autoFit = true;
    const resize = new ResizeObserver(() => {
      instance.invalidateSize({ pan: false });
      if (autoFit && fitted && !follow) untrack(() => fit(false));
    });
    resize.observe(el);
    return () => {
      resize.disconnect();
      themeWatch?.();
      instance.remove();
      lmap = null;
      aerialLayer = null;
      groups = null;
      mowerMarker = null;
      areaPolygons = new Map();
    };
  });

  // The photo's opacity is changed in place (the layer is rebuilt only for another source).
  $effect(() => {
    void generation;
    aerialLayer?.setOpacity(aerial?.opacity ?? 1);
  });

  // --- static layers: grid, areas, zones, paths, dock ---------------------------------------

  $effect(() => {
    void generation;
    const lib = L;
    const g = groups;
    const layers = geometry;
    const canSelect = selectable;
    if (!lib || !g || !lmap) return;
    for (const group of Object.values(g)) {
      if (group === g.areas || group === g.zones || group === g.paths || group === g.dock)
        group.clearLayers();
    }
    areaPolygons = new Map();

    for (const area of layers.areas) {
      const polygon = lib
        .polygon(area.ring, {
          interactive: canSelect,
          className: 'md-area',
          bubblingMouseEvents: false,
        })
        .addTo(g.areas);
      const el = polygon.getElement();
      if (el) {
        el.setAttribute('data-area', area.id);
        if (canSelect) {
          el.setAttribute('tabindex', '0');
          el.setAttribute('role', 'button');
          el.setAttribute('aria-label', labels.selectArea(area.name));
          el.addEventListener('keydown', (event) => {
            const key = (event as KeyboardEvent).key;
            if (key === 'Enter' || key === ' ') {
              event.preventDefault();
              toggleArea(area.id);
            }
          });
        }
      }
      if (canSelect) polygon.on('click', () => toggleArea(area.id));
      areaPolygons.set(area.id, polygon);
    }
    for (const zone of layers.noGo) {
      lib
        .polygon(zone.ring, {
          interactive: false,
          className: 'md-nogo',
          fillColor: `url(#${hatchId})`,
          fillOpacity: 1,
        })
        .addTo(g.zones);
    }
    for (const zone of layers.obstacles) {
      lib.polygon(zone.ring, { interactive: false, className: 'md-obstacle' }).addTo(g.zones);
    }
    for (const path of layers.paths) {
      lib.polyline(path.points, { interactive: false, className: 'md-path' }).addTo(g.paths);
    }
    if (layers.dock) {
      // The body is a true-scale footprint in map metres; the open (front) edge is dashed.
      lib
        .polygon(layers.dock.outline, { interactive: false, className: 'md-dock-body' })
        .addTo(g.dock);
      lib
        .polyline(layers.dock.outline.slice(1, 3), {
          interactive: false,
          className: 'md-dock-mouth',
        })
        .addTo(g.dock);
      const icon = glyph('md-map-dock', labels.dock, dockSvg());
      lib
        .marker(layers.dock.at, {
          icon: lib.divIcon({ html: icon.root, className: 'md-map-icon', iconSize: [0, 0] }),
          interactive: false,
          keyboard: false,
        })
        .addTo(g.dock);
    }
    if (!fitted && layers.bounds) {
      fitted = true;
      untrack(() => fit(false));
    }
  });

  // --- selection: classes, aria-pressed and order badges ------------------------------------

  $effect(() => {
    void generation;
    const lib = L;
    const g = groups;
    const layers = geometry;
    const order = [...selectedAreas];
    const labelAction = onarealabel;
    if (!lib || !g) return;
    g.labels.clearLayers();
    for (const area of layers.areas) {
      const index = order.indexOf(area.id);
      const el = areaPolygons.get(area.id)?.getElement();
      if (el) {
        el.classList.toggle('md-area--selected', index >= 0);
        if (selectable) el.setAttribute('aria-pressed', String(index >= 0));
      }
      const label = document.createElement('span');
      label.className = 'md-map-label';
      label.setAttribute('data-testid', 'map-area-label');
      const name = document.createElement('span');
      name.className = 'md-map-label-name';
      name.textContent = area.name;
      label.append(name);
      if (labelAction) {
        // A small edit button beside the name; the name itself stays click-through, so a tap
        // on the middle of an area still selects it.
        const edit = document.createElement('button');
        edit.type = 'button';
        edit.className = 'md-map-label-edit';
        edit.setAttribute('data-testid', 'area-rename-map');
        edit.setAttribute('data-label-area', area.id);
        edit.setAttribute('aria-label', labels.renameArea?.(area.name) ?? area.name);
        edit.append(editGlyph());
        lib.DomEvent.disableClickPropagation(edit);
        edit.addEventListener('click', () => labelAction(area.id));
        name.append(edit);
      }
      if (index >= 0) {
        const badge = document.createElement('span');
        badge.className = 'md-map-order';
        badge.textContent = String(index + 1);
        label.append(badge);
      }
      lib
        .marker(area.labelAt, {
          icon: lib.divIcon({ html: label, className: 'md-map-icon', iconSize: [0, 0] }),
          interactive: false,
          keyboard: false,
        })
        .addTo(g.labels);
    }
  });

  // --- layer visibility ---------------------------------------------------------------------

  $effect(() => {
    void generation;
    const g = groups;
    const m = lmap;
    if (!g || !m) return;
    const zonesOn = showZones;
    const routeOn = showRoute;
    if (zonesOn && !m.hasLayer(g.zones)) g.zones.addTo(m);
    if (!zonesOn && m.hasLayer(g.zones)) g.zones.remove();
    if (routeOn && !m.hasLayer(g.route)) g.route.addTo(m);
    if (!routeOn && m.hasLayer(g.route)) g.route.remove();
  });

  // --- driven route -------------------------------------------------------------------------

  $effect(() => {
    void generation;
    const lib = L;
    const g = groups;
    const model = routeModel;
    if (!lib || !g) return;
    g.route.clearLayers();
    for (const segment of model.segments) {
      const classes = ['md-route', `md-route--age-${segment.age}`];
      if (segment.rtk) classes.push(`md-route--rtk-${segment.rtk}`);
      if (segment.inexact) classes.push('md-route--inexact');
      lib
        .polyline(segment.points, {
          pane: 'md-route',
          interactive: false,
          className: classes.join(' '),
        })
        .addTo(g.route);
    }
  });

  // --- planned mow path: the part ahead dimmed, the mowed part solid on top -----------------

  $effect(() => {
    void generation;
    const lib = L;
    const g = groups;
    const pieces = pathPieces;
    if (!lib || !g) return;
    g.mowpath.clearLayers();
    // Ahead first, so the mowed part is drawn over the shared point.
    for (const piece of [...pieces].sort((a, b) => Number(a.done) - Number(b.done))) {
      const classes = [
        'md-mowpath',
        piece.done ? 'md-mowpath--done' : 'md-mowpath--ahead',
        `md-mowpath--${piece.kind}`,
      ];
      lib
        .polyline(piece.points, {
          pane: 'md-mowpath',
          interactive: false,
          className: classes.join(' '),
        })
        .addTo(g.mowpath);
    }
  });

  // --- RTK signal map: one square per cell, coloured by its class ---------------------------

  $effect(() => {
    void generation;
    const lib = L;
    const g = groups;
    const cells = rtkCells;
    const current = projection;
    if (!lib || !g) return;
    g.rtk.clearLayers();
    for (const cell of cells) {
      lib
        .polygon(cellRing(cell, current), {
          pane: 'md-rtk',
          interactive: false,
          className: `md-rtk-cell md-rtk-cell--${cell.rtk}`,
        })
        .addTo(g.rtk);
    }
  });

  // --- heat layer: one rectangle per cell, its colour step as a token expression ------------

  $effect(() => {
    void generation;
    const lib = L;
    const g = groups;
    const rects = heatRects;
    const current = projection;
    if (!lib || !g) return;
    g.heat.clearLayers();
    for (const rect of rects) {
      const cell = lib
        .polygon(heatRing(rect, current), {
          pane: 'md-heat',
          interactive: false,
          className: 'md-heat-cell',
        })
        .addTo(g.heat);
      const el = cell.getElement() as SVGElement | undefined;
      if (el) {
        el.style.setProperty('--md-heat', rect.color);
        el.setAttribute('data-stop', String(rect.stop));
      }
    }
  });

  // --- missed spots: thin outlines, the highlighted one strong and in view -------------------

  $effect(() => {
    void generation;
    const lib = L;
    const g = groups;
    const all = gaps;
    const active = highlighted;
    if (!lib || !g) return;
    g.gaps.clearLayers();
    for (const gap of all) {
      lib
        .polygon(gap.ring, { pane: 'md-gap', interactive: false, className: 'md-gap' })
        .addTo(g.gaps);
    }
    if (!active) return;
    lib
      .polygon(active.ring, {
        pane: 'md-gap',
        interactive: false,
        className: 'md-gap md-gap--active',
      })
      .addTo(g.gaps);
  });

  // Bring a newly highlighted spot into view (not again when only the cells reload).
  $effect(() => {
    void generation;
    const active = highlighted;
    const m = lmap;
    if (m && active) {
      autoFit = false;
      untrack(() => (follow = false));
      m.fitBounds(active.bounds, {
        paddingTopLeft: [64, 64 + untrack(() => insetTop)],
        paddingBottomRight: [64, 64 + untrack(() => insetBottom)],
        maxZoom: mode === 'local' ? 5 : 21,
        animate: !reducedMotion(),
      });
    }
  });

  // --- the spot of a spot-mowing run: its ring, the corners placed so far, the centre -------

  $effect(() => {
    void generation;
    const lib = L;
    const g = groups;
    const drawn = spotDrawn;
    if (!lib || !g) return;
    g.spot.clearLayers();
    if (!drawn) return;
    if (drawn.ring.length >= 3) {
      lib
        .polygon(drawn.ring, { pane: 'md-spot', interactive: false, className: 'md-spot' })
        .addTo(g.spot);
    } else if (drawn.corners.length >= 2) {
      lib
        .polyline(drawn.corners, {
          pane: 'md-spot',
          interactive: false,
          className: 'md-spot md-spot--open',
        })
        .addTo(g.spot);
    }
    for (const corner of drawn.corners) {
      lib
        .circleMarker(corner, {
          pane: 'md-spot',
          interactive: false,
          radius: 5,
          className: 'md-spot-corner',
        })
        .addTo(g.spot);
    }
    if (drawn.center) {
      lib
        .circleMarker(drawn.center, {
          pane: 'md-spot',
          interactive: false,
          radius: 3,
          className: 'md-spot-center',
        })
        .addTo(g.spot);
    }
  });

  // --- the mower ----------------------------------------------------------------------------

  $effect(() => {
    void generation;
    const lib = L;
    const m = lmap;
    const current = mower;
    if (!lib || !m) return;
    if (!current) {
      mowerMarker?.remove();
      mowerMarker = null;
      return;
    }
    if (!mowerMarker) {
      const icon = glyph('md-map-mower', labels.mower, mowerSvg());
      icon.root.setAttribute('data-testid', 'mower-marker');
      mowerMarker = lib
        .marker(current.at, {
          icon: lib.divIcon({
            html: icon.root,
            className: 'md-map-icon md-map-icon--mower',
            iconSize: [0, 0],
          }),
          pane: 'md-mower',
          interactive: false,
          keyboard: false,
        })
        .addTo(m);
    } else {
      mowerMarker.setLatLng(current.at);
    }
    const root = mowerMarker.getElement()?.firstElementChild as HTMLElement | null;
    if (root) {
      root.dataset.tone = current.tone;
      root.classList.toggle('is-hollow', current.hollow);
      root.classList.toggle('no-heading', current.rotation === null);
      const inner = root.firstElementChild as HTMLElement | null;
      inner?.style.setProperty('--md-map-rotation', `${current.rotation ?? 0}deg`);
    }
    if (follow) m.panTo(current.at, { animate: !reducedMotion() });
  });

  // --- keep the spot and its route preview above a bottom sheet --------------------------------

  $effect(() => {
    void generation;
    const m = lmap;
    const bounds = spotDrawn?.bounds;
    const inset = insetBottom;
    const top = insetTop;
    const pieces = pathPreview ? pathPieces : [];
    if (!m || !bounds) return;
    untrack(() => reveal(m, [...bounds, ...pieces.flatMap((piece) => piece.points)], inset, top));
  });

  /** Pan (or fit, when larger) so `coords` lie in the part of the map not covered below. */
  function reveal(m: Leaflet.Map, coords: MapCoord[], inset: number, top: number): void {
    const size = m.getSize();
    const box = pixelBox(coords.map((c) => m.latLngToContainerPoint(c)));
    if (!box) return;
    const pad = 24;
    const how = revealPan(
      { ...box, top: box.top - top, bottom: box.bottom - top },
      size.x,
      size.y - top,
      inset,
      pad,
    );
    const animate = !reducedMotion();
    if (how.kind === 'pan') {
      m.panBy([how.dx, how.dy], { animate });
    } else if (how.kind === 'fit') {
      m.fitBounds(coords, {
        paddingTopLeft: [pad, pad + top],
        paddingBottomRight: [pad, pad + inset],
        maxZoom: m.getZoom(),
        animate,
      });
    }
  }

  // --- helpers ------------------------------------------------------------------------------

  /** The pencil of the label edit buttons (the `edit` icon, built as DOM for Leaflet). */
  function editGlyph(): SVGSVGElement {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', '14');
    svg.setAttribute('height', '14');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    const path = document.createElementNS(ns, 'path');
    path.setAttribute('d', ICON_PATHS.edit);
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-width', '2');
    path.setAttribute('stroke-linecap', 'square');
    svg.append(path);
    return svg;
  }

  function toggleArea(id: string): void {
    onselectionchange?.(toggleSelection(selectedAreas, id));
  }

  function fit(animate = true, byUser = false): void {
    const m = lmap;
    const bounds =
      geometry.bounds ?? (mower ? ([mower.at, mower.at] as [MapCoord, MapCoord]) : null);
    if (!m || !bounds) return;
    if (byUser) follow = false;
    m.fitBounds(bounds, {
      paddingTopLeft: [32, 32 + insetTop],
      paddingBottomRight: [32, 32 + insetBottom],
      maxZoom: mode === 'local' ? 6 : 21,
      animate: animate && !reducedMotion(),
    });
  }

  function reducedMotion(): boolean {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  /** The scale bar: the longest 1-2-5 length that fits in 96 px. */
  function scaleBar(m: Leaflet.Map): { width: number; label: string } {
    const size = m.getSize();
    const y = size.y / 2;
    const metres = m.distance(m.containerPointToLatLng([0, y]), m.containerPointToLatLng([100, y]));
    return scaleFor(metres / 100);
  }

  const SVG_NS = 'http://www.w3.org/2000/svg';

  function svg(tag: string, attrs: Record<string, string>): SVGElement {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
    return el;
  }

  /** A marker glyph: an outer element (positioned by Leaflet) and an inner rotated one. */
  function glyph(
    className: string,
    label: string,
    content: SVGElement,
  ): { root: HTMLElement; inner: HTMLElement } {
    const root = document.createElement('span');
    root.className = className;
    root.setAttribute('role', 'img');
    root.setAttribute('aria-label', label);
    const inner = document.createElement('span');
    inner.className = 'md-map-rotor';
    inner.append(content);
    root.append(inner);
    return { root, inner };
  }

  function mowerSvg(): SVGElement {
    const el = svg('svg', {
      viewBox: '0 0 24 24',
      width: '24',
      height: '24',
      'aria-hidden': 'true',
    });
    el.append(svg('path', { class: 'md-map-chevron', d: 'M12 2.5 20 21 12 16.5 4 21Z' }));
    el.append(svg('circle', { class: 'md-map-dot', cx: '12', cy: '12', r: '6' }));
    return el;
  }

  function dockSvg(): SVGElement {
    const el = svg('svg', {
      viewBox: '0 0 24 24',
      width: '18',
      height: '18',
      'aria-hidden': 'true',
    });
    el.append(
      svg('path', { class: 'md-map-dock-bolt', d: 'M13.5 3 6.5 13.5H11L10 21l7.5-11H13Z' }),
    );
    return el;
  }

  function onkeydown(event: KeyboardEvent): void {
    if (event.key !== 'Escape') return;
    if (layersOpen) layersOpen = false;
    else if (expanded) onexpandchange?.(false);
  }
</script>

<svelte:window {onkeydown} />

<div
  class="md-map"
  class:is-geo={mode === 'geo'}
  class:is-selectable={selectable}
  class:is-tapping={!!onmaptap}
  data-testid={testid}
  data-mode={mode}
  data-ready={L !== null && generation > 0}
  data-route-points={routeModel.points}
  data-rtk-cells={showRtk ? rtkCells.length : undefined}
  data-heat-cells={heat ? heatRects.length : undefined}
  data-heat-kind={heat?.legend.kind}
  data-heat-gap={highlighted ? highlighted.index : undefined}
  data-path-points={path ? path.points_total : undefined}
  data-path-done={path?.progress ? path.progress.index : undefined}
  data-path-preview={path && pathPreview ? 'true' : undefined}
  data-spot={spot ? spot.kind : undefined}
  data-spot-corners={spot?.kind === 'polygon' ? spot.points.length : undefined}
  data-inset-bottom={insetBottom > 0 ? Math.round(insetBottom) : undefined}
  style:--md-map-inset-bottom="{Math.max(0, insetBottom)}px"
  style:--md-map-inset-top="{Math.max(0, insetTop)}px"
>
  <svg class="defs" width="0" height="0" aria-hidden="true" focusable="false">
    <defs>
      <pattern
        id={hatchId}
        width="7"
        height="7"
        patternUnits="userSpaceOnUse"
        patternTransform="rotate(45)"
      >
        <line x1="0" y1="0" x2="0" y2="7" class="hatch" />
      </pattern>
    </defs>
  </svg>

  <div class="canvas" bind:this={container} role="region" aria-label={labels.region}></div>

  <div class="hud" aria-hidden="true">
    <span class="north" style:--md-map-north="{projection.northScreen}deg">
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

  {#if path && pathPieces.length && labels.path}
    <ul
      class="legend legend--path"
      class:legend--below={showRtk || (rtkTint && showRoute)}
      data-testid="mow-path-legend"
    >
      <li class="legend-title">
        {labels.path}{#if pathPreview && labels.pathPreview}<span class="legend-caption"
            >{labels.pathPreview}</span
          >{/if}
      </li>
      {#if pathHasProgress && labels.pathDone}<li data-path="done">{labels.pathDone}</li>{/if}
      {#if labels.pathAhead}<li data-path="ahead">{labels.pathAhead}</li>{/if}
      {#if spotDrawn?.ring.length && labels.spot}<li data-path="spot">{labels.spot}</li>{/if}
    </ul>
  {:else if spotDrawn?.ring.length && labels.spot}
    <ul class="legend legend--path" data-testid="spot-legend">
      <li data-path="spot">{labels.spot}</li>
    </ul>
  {/if}

  {#if showRtk}
    <ul class="legend legend--cells" data-testid="map-rtk-legend">
      <li class="legend-title">{labels.rtkLegend}</li>
      <li data-rtk="fixed">{labels.rtkFixed}</li>
      <li data-rtk="float">{labels.rtkFloat}</li>
      <li data-rtk="none">{labels.rtkNone}</li>
      {#if rtkUnknownShown}<li data-rtk="unknown">{labels.rtkUnknown}</li>{/if}
    </ul>
  {:else if rtkTint && showRoute}
    <ul class="legend">
      <li data-rtk="fixed">{labels.rtkFixed}</li>
      <li data-rtk="float">{labels.rtkFloat}</li>
      <li data-rtk="none">{labels.rtkNone}</li>
    </ul>
  {/if}

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
        data-testid="map-fit"
        onclick={() => {
          autoFit = true;
          fit(true, true);
        }}
      >
        <Icon name="fit" />
      </button>
      <button
        type="button"
        class="ctl"
        aria-label={labels.follow}
        title={labels.follow}
        aria-pressed={follow}
        disabled={!mower}
        data-testid="map-follow"
        onclick={() => {
          follow = !follow;
          if (follow) autoFit = false;
        }}
      >
        <Icon name="crosshair" />
      </button>
      <button
        type="button"
        class="ctl"
        aria-label={labels.layers}
        title={labels.layers}
        aria-expanded={layersOpen}
        aria-controls="{uid}-layers"
        data-testid="map-layers"
        onclick={() => (layersOpen = !layersOpen)}
      >
        <Icon name="layers" />
      </button>
    </div>
    {#if onexpandchange}
      <div class="stack">
        <button
          type="button"
          class="ctl"
          aria-label={expanded ? labels.collapse : labels.expand}
          title={expanded ? labels.collapse : labels.expand}
          aria-pressed={expanded}
          data-testid="map-expand"
          onclick={() => onexpandchange?.(!expanded)}
        >
          <Icon name={expanded ? 'collapse' : 'expand'} />
        </button>
      </div>
    {/if}
  </div>

  {#if layersOpen}
    <div class="layers" id="{uid}-layers" role="group" aria-label={labels.layers}>
      <button
        type="button"
        class="toggle"
        aria-pressed={showRoute}
        onclick={() => (showRoute = !showRoute)}
      >
        <span class="box"></span>{labels.route}
      </button>
      <button
        type="button"
        class="toggle"
        aria-pressed={showZones}
        onclick={() => (showZones = !showZones)}
      >
        <span class="box"></span>{labels.zones}
      </button>
      <button
        type="button"
        class="toggle"
        aria-pressed={rtkTint}
        data-testid="map-rtk-tint"
        onclick={() => (rtkTint = !rtkTint)}
      >
        <span class="box"></span>{labels.rtkTint}
      </button>
      {#if !heatMode}
        <button
          type="button"
          class="toggle"
          aria-pressed={showRtk}
          data-testid="map-rtk-layer"
          onclick={() => (showRtk = !showRtk)}
        >
          <span class="box"></span>{labels.rtkLayer}
        </button>
      {/if}
      {#if showRtk && !heatMode}
        <div class="choice" role="radiogroup" aria-label={labels.rtkCellSize}>
          <span class="choice-label" aria-hidden="true">{labels.rtkCellSize}</span>
          <div class="choice-row">
            {#each RTK_CELL_SIZES as size (size)}
              <button
                type="button"
                role="radio"
                class="chip"
                aria-checked={rtkCellSize === size}
                data-testid="map-rtk-cell-{size}"
                onclick={() => (rtkCellSize = size)}>{labels.rtkCell(size)}</button
              >
            {/each}
          </div>
        </div>
        <div class="choice" role="radiogroup" aria-label={labels.rtkAggregate}>
          <span class="choice-label" aria-hidden="true">{labels.rtkAggregate}</span>
          <div class="choice-row">
            <button
              type="button"
              role="radio"
              class="chip chip--text"
              aria-checked={rtkAggregate === 'worst'}
              data-testid="map-rtk-worst"
              onclick={() => (rtkAggregate = 'worst')}>{labels.rtkWorst}</button
            >
            <button
              type="button"
              role="radio"
              class="chip chip--text"
              aria-checked={rtkAggregate === 'majority'}
              data-testid="map-rtk-majority"
              onclick={() => (rtkAggregate = 'majority')}>{labels.rtkMajority}</button
            >
          </div>
        </div>
      {/if}
    </div>
  {/if}

  {#if mode === 'geo' && (tiles?.attribution || aerial?.attribution)}
    <p class="attribution" data-testid="map-attribution">
      {[tiles?.attribution, aerial?.attribution].filter(Boolean).join(' | ')}
    </p>
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

  .defs {
    position: absolute;
    width: 0;
    height: 0;
    overflow: hidden;
  }

  .hatch {
    stroke: var(--md-state-error);
    stroke-width: 1.5;
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

  /* --- Leaflet chrome: neutralised, the panel draws its own -------------------------------- */

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

  /* --- areas, zones, paths ---------------------------------------------------------------- */

  .md-map :global(path.md-area) {
    fill: var(--md-color-ink);
    fill-opacity: 0.06;
    stroke: var(--md-color-ink);
    stroke-width: 1.5;
    stroke-opacity: 1;
    stroke-linejoin: miter;
    transition: fill-opacity var(--md-motion-fast) var(--md-ease);
  }

  .md-map.is-selectable :global(path.md-area) {
    cursor: pointer;
  }

  .md-map.is-selectable :global(path.md-area:hover) {
    fill-opacity: 0.12;
  }

  .md-map :global(path.md-area:focus-visible) {
    outline: none;
    stroke: var(--md-color-focus);
    stroke-width: 3;
    stroke-dasharray: 6 3;
  }

  .md-map :global(path.md-area.md-area--selected) {
    fill: var(--md-color-focus);
    fill-opacity: 0.12;
    stroke: var(--md-color-focus);
    stroke-width: 3;
  }

  .md-map :global(path.md-nogo) {
    stroke: var(--md-state-error);
    stroke-width: 1.5;
    stroke-opacity: 1;
  }

  .md-map :global(path.md-obstacle) {
    fill: var(--md-color-line-strong);
    fill-opacity: 0.4;
    stroke: var(--md-color-line-strong);
    stroke-width: 1;
    stroke-opacity: 1;
  }

  .md-map :global(path.md-path) {
    fill: none;
    stroke: var(--md-color-ink-muted);
    stroke-width: 1.5;
    stroke-opacity: 1;
    stroke-dasharray: 4 4;
  }

  /* --- route ------------------------------------------------------------------------------ */

  .md-map :global(path.md-route) {
    fill: none;
    stroke: var(--md-state-mowing);
    stroke-width: 2;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  .md-map :global(path.md-route--age-0) {
    stroke-opacity: 0.3;
  }

  .md-map :global(path.md-route--age-1) {
    stroke-opacity: 0.5;
  }

  .md-map :global(path.md-route--age-2) {
    stroke-opacity: 0.75;
  }

  .md-map :global(path.md-route--age-3) {
    stroke-opacity: 1;
  }

  /* Driven without an RTK fix: the line is a guess, so it is dashed. */
  .md-map :global(path.md-route--inexact) {
    stroke-dasharray: 5 5;
  }

  .md-map :global(path.md-route--rtk-fixed) {
    stroke: var(--md-rtk-fixed);
  }

  .md-map :global(path.md-route--rtk-float) {
    stroke: var(--md-rtk-float);
  }

  .md-map :global(path.md-route--rtk-none) {
    stroke: var(--md-rtk-none);
  }

  .md-map :global(path.md-route--rtk-unknown) {
    stroke: var(--md-state-unknown);
    stroke-dasharray: 2 4;
  }

  /* --- planned mow path: accent colour, the mowed part solid, the part ahead dimmed ---------- */

  .md-map :global(path.md-mowpath) {
    fill: none;
    stroke: var(--md-color-focus);
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  .md-map :global(path.md-mowpath--ahead) {
    stroke-width: 1.25;
    stroke-opacity: 0.38;
  }

  .md-map :global(path.md-mowpath--done) {
    stroke-width: 2;
    stroke-opacity: 1;
  }

  /* Driving between areas with the blades off: dotted, like the transfer paths of the map. */
  .md-map :global(path.md-mowpath--transfer) {
    stroke-dasharray: 2 4;
  }

  /* --- the spot: a strong accent ring with a light fill; corners while drawing ------------- */

  .md-map :global(path.md-spot) {
    fill: var(--md-color-focus);
    fill-opacity: 0.14;
    stroke: var(--md-color-focus);
    stroke-width: 2.5;
    stroke-linejoin: round;
  }

  .md-map :global(path.md-spot--open) {
    fill: none;
    stroke-dasharray: 4 4;
  }

  .md-map :global(path.md-spot-corner) {
    fill: var(--md-color-surface);
    stroke: var(--md-color-focus);
    stroke-width: 2.5;
  }

  .md-map :global(path.md-spot-center) {
    fill: var(--md-color-focus);
    stroke: var(--md-color-surface);
    stroke-width: 1.5;
  }

  .md-map.is-tapping .canvas {
    cursor: crosshair;
  }

  /* --- RTK signal map --------------------------------------------------------------------- */

  .md-map :global(path.md-rtk-cell) {
    fill-opacity: 0.42;
    stroke-width: 1;
    stroke-opacity: 0.55;
    shape-rendering: crispedges;
  }

  .md-map :global(path.md-rtk-cell--fixed) {
    fill: var(--md-rtk-fixed);
    stroke: var(--md-rtk-fixed);
  }

  .md-map :global(path.md-rtk-cell--float) {
    fill: var(--md-rtk-float);
    stroke: var(--md-rtk-float);
  }

  .md-map :global(path.md-rtk-cell--none) {
    fill: var(--md-rtk-none);
    fill-opacity: 0.55;
    stroke: var(--md-rtk-none);
  }

  .md-map :global(path.md-rtk-cell--unknown) {
    fill: var(--md-state-unknown);
    fill-opacity: 0.2;
    stroke: var(--md-state-unknown);
    stroke-dasharray: 2 2;
  }

  /* --- heat layers and missed spots ------------------------------------------------------- */

  .md-map :global(path.md-heat-cell) {
    fill: var(--md-heat);
    fill-opacity: 0.5;
    stroke: var(--md-heat);
    stroke-width: 1;
    stroke-opacity: 0.35;
    shape-rendering: crispedges;
  }

  .md-map :global(path.md-gap) {
    fill: none;
    stroke: var(--md-color-ink);
    stroke-width: 1;
    stroke-dasharray: 3 3;
    stroke-opacity: 0.7;
  }

  .md-map :global(path.md-gap.md-gap--active) {
    fill: var(--md-color-focus);
    fill-opacity: 0.16;
    stroke: var(--md-color-focus);
    stroke-width: 3;
    stroke-dasharray: none;
    stroke-opacity: 1;
  }

  /* --- markers ---------------------------------------------------------------------------- */

  .md-map :global(.md-map-icon) {
    overflow: visible;
    border: 0;
    background: none;
  }

  .md-map :global(.md-map-label) {
    position: absolute;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--md-space-1);
    transform: translate(-50%, -50%);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    line-height: var(--md-line-16);
    text-transform: uppercase;
    white-space: nowrap;
    pointer-events: none;
    text-shadow:
      0 0 2px var(--md-color-surface-sunken),
      0 0 4px var(--md-color-surface-sunken);
  }

  .md-map :global(.md-map-label-name) {
    position: relative;
  }

  .md-map :global(.md-map-label-edit) {
    position: absolute;
    top: 50%;
    left: 100%;
    display: grid;
    place-items: center;
    width: 28px;
    height: 28px;
    margin-left: var(--md-space-1);
    padding: 0;
    border: var(--md-hairline) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
    background: var(--md-color-surface);
    color: var(--md-color-ink-muted);
    cursor: pointer;
    pointer-events: auto;
    transform: translateY(-50%);
    text-shadow: none;
  }

  .md-map :global(.md-map-label-edit:hover) {
    border-color: var(--md-color-focus);
    color: var(--md-color-focus);
  }

  .md-map :global(.md-map-label-edit:focus-visible) {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .md-map :global(.md-map-order) {
    display: grid;
    place-items: center;
    min-width: 22px;
    height: 22px;
    padding: 0 var(--md-space-1);
    border-radius: var(--md-radius-1);
    background: var(--md-color-focus);
    color: var(--md-color-ink-inverse);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
    font-variant-numeric: tabular-nums;
    letter-spacing: 0;
    text-shadow: none;
  }

  .md-map :global(.md-map-dock),
  .md-map :global(.md-map-mower) {
    position: absolute;
    display: block;
    transform: translate(-50%, -50%);
    pointer-events: none;
  }

  .md-map :global(.md-map-rotor) {
    display: block;
    transform: rotate(var(--md-map-rotation, 0deg));
  }

  .md-map :global(.md-map-rotor svg) {
    display: block;
    overflow: visible;
  }

  .md-map :global(path.md-dock-body) {
    fill: var(--md-color-surface-sunken);
    fill-opacity: 0.92;
    stroke: var(--md-color-ink);
    stroke-width: 1.5;
    stroke-linejoin: round;
  }

  .md-map :global(path.md-dock-mouth) {
    fill: none;
    stroke: var(--md-color-surface-sunken);
    stroke-width: 2;
    stroke-dasharray: 3 3;
  }

  .md-map :global(.md-map-dock-bolt) {
    fill: var(--md-color-ink);
    stroke: var(--md-color-surface-sunken);
    stroke-width: 1;
    paint-order: stroke;
  }

  .md-map :global(.md-map-mower) {
    --md-map-tone: var(--md-state-unknown);
  }

  .md-map :global(.md-map-mower[data-tone='mowing']) {
    --md-map-tone: var(--md-state-mowing);
  }

  .md-map :global(.md-map-mower[data-tone='returning']) {
    --md-map-tone: var(--md-state-returning);
  }

  .md-map :global(.md-map-mower[data-tone='charging']) {
    --md-map-tone: var(--md-state-charging);
  }

  .md-map :global(.md-map-mower[data-tone='docked']) {
    --md-map-tone: var(--md-state-docked);
  }

  .md-map :global(.md-map-mower[data-tone='paused']) {
    --md-map-tone: var(--md-state-paused);
  }

  .md-map :global(.md-map-mower[data-tone='error']) {
    --md-map-tone: var(--md-state-error);
  }

  .md-map :global(.md-map-mower[data-tone='offline']) {
    --md-map-tone: var(--md-state-offline);
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

  .md-map :global(.md-map-mower.no-heading .md-map-chevron) {
    display: none;
  }

  .md-map :global(.md-map-mower.no-heading .md-map-dot) {
    display: inline;
  }

  .md-map :global(.md-map-mower.is-hollow .md-map-chevron),
  .md-map :global(.md-map-mower.is-hollow .md-map-dot) {
    fill: var(--md-color-surface);
    stroke: var(--md-map-tone);
    stroke-width: 1.5;
    stroke-dasharray: 3 2;
  }

  /* The chevron glides between positions; never during a zoom animation. */
  .md-map :global(.md-map-icon--mower) {
    transition: transform 900ms linear;
  }

  .md-map :global(.leaflet-zoom-anim .md-map-icon--mower) {
    transition: none;
  }

  /* --- HUD: north arrow and scale --------------------------------------------------------- */

  .hud {
    position: absolute;
    inset-inline-start: var(--md-space-3);
    bottom: calc(var(--md-space-3) + var(--md-map-inset-bottom, 0px));
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
    transform: rotate(var(--md-map-north));
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

  .legend {
    position: absolute;
    top: calc(var(--md-space-3) + var(--md-map-inset-top, 0px));
    inset-inline-start: var(--md-space-3);
    z-index: 2;
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    margin: 0;
    padding: var(--md-space-2) var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    list-style: none;
    text-transform: uppercase;
  }

  .legend li {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
  }

  .legend li::before {
    content: '';
    width: 16px;
    height: 3px;
    background: var(--md-legend);
  }

  .legend [data-rtk='fixed'] {
    --md-legend: var(--md-rtk-fixed);
  }

  .legend [data-rtk='float'] {
    --md-legend: var(--md-rtk-float);
  }

  .legend [data-rtk='none'] {
    --md-legend: var(--md-rtk-none);
  }

  .legend [data-rtk='unknown'] {
    --md-legend: var(--md-state-unknown);
  }

  .legend--path {
    gap: var(--md-space-1);
  }

  .legend--path.legend--below {
    top: auto;
    bottom: calc(var(--md-space-3) + 48px + var(--md-map-inset-bottom, 0px));
  }

  .legend--path [data-path='done'] {
    --md-legend: var(--md-color-focus);
  }

  .legend--path [data-path='ahead'] {
    --md-legend: color-mix(in srgb, var(--md-color-focus) 38%, var(--md-color-surface));
  }

  .legend--path [data-path='spot'] {
    --md-legend: color-mix(in srgb, var(--md-color-focus) 14%, var(--md-color-surface));
  }

  .legend--path [data-path='spot']::before {
    width: 12px;
    height: 12px;
    border: 2px solid var(--md-color-focus);
    border-radius: var(--md-radius-round);
  }

  .legend--path .legend-title {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
  }

  .legend--path .legend-title::before {
    display: none;
  }

  .legend-caption {
    padding: 0 var(--md-space-1);
    border: var(--md-hairline) solid var(--md-color-focus);
    color: var(--md-color-focus);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-11);
    letter-spacing: 0;
  }

  .legend--cells li::before {
    width: 12px;
    height: 12px;
    border: var(--md-hairline) solid var(--md-legend);
    background: color-mix(in srgb, var(--md-legend) 45%, transparent);
  }

  .legend--cells .legend-title {
    color: var(--md-color-ink-muted);
  }

  .legend--cells .legend-title::before {
    display: none;
  }

  /* --- controls --------------------------------------------------------------------------- */

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

  .ctl:hover:not(:disabled) {
    background: var(--md-color-surface-sunken);
  }

  .ctl:focus-visible {
    position: relative;
    z-index: 1;
    outline: 2px solid var(--md-color-focus);
    outline-offset: 0;
  }

  .ctl[aria-pressed='true'],
  .ctl[aria-expanded='true'] {
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .ctl:disabled {
    color: var(--md-color-line-strong);
    cursor: not-allowed;
  }

  .layers {
    position: absolute;
    top: var(--md-space-3);
    inset-inline-end: calc(var(--md-target) + var(--md-space-3) + var(--md-space-2));
    z-index: 3;
    display: flex;
    flex-direction: column;
    min-width: 248px;
    border: var(--md-hairline) solid var(--md-color-line-strong);
    background: var(--md-color-surface);
    box-shadow: var(--md-shadow-float);
  }

  .toggle {
    display: flex;
    align-items: center;
    gap: var(--md-space-3);
    min-height: var(--md-target);
    padding: 0 var(--md-space-3);
    border: 0;
    border-radius: var(--md-radius-0);
    background: none;
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-align: start;
    text-transform: uppercase;
    cursor: pointer;
  }

  .toggle + .toggle {
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .toggle:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .choice {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-1);
    padding: var(--md-space-2) var(--md-space-3) var(--md-space-3);
    border-top: var(--md-hairline) solid var(--md-color-line);
  }

  .choice-label {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .choice-row {
    display: flex;
  }

  .chip {
    flex: 1;
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

  .chip--text {
    font-family: var(--md-font-sans);
    font-weight: var(--md-weight-medium);
    white-space: nowrap;
  }

  .chip + .chip {
    border-inline-start: 0;
  }

  .chip[aria-checked='true'] {
    background: var(--md-color-ink);
    color: var(--md-color-ink-inverse);
  }

  .chip:focus-visible {
    position: relative;
    outline: 2px solid var(--md-color-focus);
    outline-offset: 0;
  }

  .box {
    flex: none;
    width: 16px;
    height: 16px;
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
  }

  .toggle[aria-pressed='true'] .box {
    border-color: var(--md-color-ink);
    background: var(--md-color-ink);
    box-shadow: inset 0 0 0 3px var(--md-color-surface);
  }

  .attribution {
    position: absolute;
    inset-inline-end: 0;
    bottom: var(--md-map-inset-bottom, 0);
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
    .md-map :global(.md-map-icon--mower) {
      transition: none;
    }

    .ctl,
    .md-map :global(path.md-area) {
      transition: none;
    }
  }
</style>
