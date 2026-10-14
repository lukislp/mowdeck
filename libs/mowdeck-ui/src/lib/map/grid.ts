/**
 * The metric grid of the local map: a Leaflet `GridLayer` drawing 1-5-10-50-... m lines on canvas
 * tiles, so it covers the whole visible map at any pan and zoom. Pure spacing logic lives in
 * `gridSpacing` (unit tested); the layer only paints.
 */
import type * as Leaflet from 'leaflet';

/** Candidate minor spacings in metres, ascending. */
const LADDER = [1, 5, 10, 50, 100, 500, 1_000, 5_000, 10_000, 50_000];
/** The thinnest minor line distance in screen pixels. */
const MIN_MINOR_PX = 9;
const MINOR_ALPHA = 0.45;

export interface GridSpacing {
  /** Distance of the thin lines in metres. */
  minor: number;
  /** Distance of the strong lines in metres (a multiple of `minor`). */
  major: number;
}

/** Line spacing for a scale of `pxPerMetre`: thins out when zoomed out so it never fills solid. */
export function gridSpacing(pxPerMetre: number): GridSpacing {
  const scale = Number.isFinite(pxPerMetre) && pxPerMetre > 0 ? pxPerMetre : 1;
  const minor = LADDER.find((step) => step * scale >= MIN_MINOR_PX) ?? LADDER[LADDER.length - 1]!;
  // 1, 10, 100 ... step by five; 5, 50, 500 ... by two (so majors stay at 5/10/50/100 m).
  const leading = String(minor)[0];
  return { minor, major: leading === '5' ? minor * 2 : minor * 5 };
}

/** The multiples of `step` within `[from, to]`, with their indices. */
export function gridMultiples(from: number, to: number, step: number): number[] {
  const out: number[] = [];
  const first = Math.ceil(from / step);
  const last = Math.floor(to / step);
  if (last - first > 2_000) return out;
  for (let k = first; k <= last; k += 1) out.push(k);
  return out;
}

export interface GridLayerOptions {
  /** Pane name; the pane must exist. */
  pane: string;
  /** CSS custom property holding the line colour, resolved on `host`. */
  colorVar?: string;
  host: HTMLElement;
}

/** Create the grid layer for a `CRS.Simple` map (1 unit = 1 m). */
export function createGridLayer(lib: typeof Leaflet, options: GridLayerOptions): Leaflet.GridLayer {
  const Layer = lib.GridLayer.extend({
    createTile(this: Leaflet.GridLayer, coords: Leaflet.Coords): HTMLElement {
      const size = this.getTileSize();
      const dpr = Math.max(1, Math.round(window.devicePixelRatio || 1));
      const tile = document.createElement('canvas');
      tile.width = size.x * dpr;
      tile.height = size.y * dpr;
      const ctx = tile.getContext('2d');
      if (!ctx) return tile;
      const scale = 2 ** coords.z; // px per metre at the tile's zoom (CRS.Simple)
      const { minor, major } = gridSpacing(scale);
      const px0 = coords.x * size.x;
      const py0 = coords.y * size.y;
      const color =
        getComputedStyle(options.host)
          .getPropertyValue(options.colorVar ?? '--md-color-line')
          .trim() || '#888';
      ctx.fillStyle = color;
      const xs = gridMultiples(px0 / scale, (px0 + size.x) / scale, minor);
      // Screen y grows downwards while the local y grows up.
      const ys = gridMultiples(-(py0 + size.y) / scale, -py0 / scale, minor);
      const strong = Math.round(major / minor);
      const line = (k: number, vertical: boolean, offset: number, sign: number) => {
        const isMajor = k % strong === 0;
        ctx.globalAlpha = isMajor ? 1 : MINOR_ALPHA;
        const at = Math.round((sign * k * minor * scale - offset) * dpr);
        if (vertical) ctx.fillRect(at, 0, dpr, tile.height);
        else ctx.fillRect(0, at, tile.width, dpr);
      };
      for (const k of xs) line(k, true, px0, 1);
      for (const k of ys) line(k, false, py0, -1);
      return tile;
    },
  });
  return new (Layer as unknown as new (o: Leaflet.GridLayerOptions) => Leaflet.GridLayer)({
    pane: options.pane,
    tileSize: 256,
    noWrap: true,
    keepBuffer: 2,
    updateWhenZooming: false,
    interactive: false,
  } as Leaflet.GridLayerOptions);
}
