/**
 * The aerial photo layer of the mower map: a Leaflet tile layer whose tiles are loaded through a
 * callback instead of the browser's own image request, so the page can add the bearer token
 * (an `<img>` cannot). The blob becomes the image's source and is released when the tile goes.
 *
 * The tiles come from MowDeck's own tile route (EPSG:3857, XYZ), which asks the photo source for
 * exactly the tile's Mercator box, so they line up with the map's geographic projection.
 */
import type * as Leaflet from 'leaflet';

/** An aerial photo under the map; see `MowerMap`'s `aerial` prop. */
export interface MapAerial {
  /** URL template with `{z}`, `{x}`, `{y}`; handed to `load` filled in. */
  url: string;
  /** Opacity of the photo, 0 to 1. */
  opacity: number;
  attribution?: string | null;
  minZoom?: number;
  /** Highest zoom with real detail; deeper zooms scale the last level. */
  maxZoom?: number;
  /** `[south, west, north, east]` in degrees: where the source serves tiles. */
  bounds?: readonly [number, number, number, number] | null;
  /** Loads one tile (with the bearer token). */
  load: (url: string, signal: AbortSignal) => Promise<Blob>;
}

/** Create the layer; `loader` is read per tile, so a refreshed token function is picked up. */
export function createAerialLayer(
  lib: typeof Leaflet,
  aerial: MapAerial,
  loader: () => MapAerial['load'] | undefined,
): Leaflet.TileLayer {
  const options: Leaflet.TileLayerOptions = {
    minZoom: aerial.minZoom ?? 0,
    maxNativeZoom: aerial.maxZoom ?? 19,
    maxZoom: 22,
    opacity: aerial.opacity,
    className: 'md-map-aerial',
    keepBuffer: 1,
    updateWhenIdle: true,
  };
  if (aerial.bounds) {
    const [south, west, north, east] = aerial.bounds;
    options.bounds = lib.latLngBounds([south, west], [north, east]);
  }
  const layer = lib.tileLayer(aerial.url, options);
  const pending = new WeakMap<HTMLElement, { abort: AbortController; objectUrl: string | null }>();

  // createTile is protected in the typings; Leaflet calls it from _addTile.
  const hooks = layer as unknown as {
    createTile: (coords: Leaflet.Coords, done: Leaflet.DoneCallback) => HTMLElement;
  };
  hooks.createTile = (coords, done) => {
    const img = document.createElement('img');
    img.alt = '';
    img.setAttribute('role', 'presentation');
    const state = { abort: new AbortController(), objectUrl: null as string | null };
    pending.set(img, state);
    const load = loader();
    if (!load) {
      done(new Error('no tile loader'), img);
      return img;
    }
    load(layer.getTileUrl(coords), state.abort.signal)
      .then((blob) => {
        if (state.abort.signal.aborted) return;
        state.objectUrl = URL.createObjectURL(blob);
        img.onload = () => done(undefined, img);
        img.onerror = () => done(new Error('tile is not an image'), img);
        img.src = state.objectUrl;
      })
      .catch((error: unknown) => {
        if (!state.abort.signal.aborted) done(error as Error, img);
      });
    return img;
  };

  layer.on('tileunload', (event) => {
    const tile = (event as Leaflet.TileEvent).tile;
    const state = pending.get(tile);
    if (!state) return;
    state.abort.abort();
    if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
    pending.delete(tile);
  });
  return layer;
}
