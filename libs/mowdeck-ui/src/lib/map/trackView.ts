/**
 * The view limits of the theft tracking map (`TrackMap`): the box its base map serves tiles in
 * (the tracked positions plus the server's margin, cut to the source's coverage) and the lowest
 * zoom at which that box still fills the whole view. Outside the box the server answers 404
 * `tile-outside-map`, so the map never lets the user pan or zoom out to an empty area.
 *
 * Pure (no Leaflet), so the numbers are tested on their own (`trackView.test.ts`).
 */

/** `[south, west, north, east]` in degrees. */
export type GeoBox = readonly [number, number, number, number];

const EARTH_RADIUS_M = 6_378_137;
/** Latitude limit of Web Mercator. */
const MAX_LAT = 85.0511287798066;
const TILE_SIZE = 256;

/**
 * The box around `points` with `marginM` metres more on every side; null without points. The
 * same arithmetic as the server's `points_bounds`, so the client never asks for less than it
 * gets (the server's box is never smaller).
 */
export function marginBox(
  points: readonly { lat: number; lon: number }[],
  marginM: number,
): GeoBox | null {
  if (!points.length) return null;
  let south = Infinity;
  let west = Infinity;
  let north = -Infinity;
  let east = -Infinity;
  for (const p of points) {
    if (!Number.isFinite(p.lat) || !Number.isFinite(p.lon)) continue;
    south = Math.min(south, p.lat);
    north = Math.max(north, p.lat);
    west = Math.min(west, p.lon);
    east = Math.max(east, p.lon);
  }
  if (!Number.isFinite(south)) return null;
  const midLat = (south + north) / 2;
  const dLat = degrees(marginM / EARTH_RADIUS_M);
  const dLon = degrees(marginM / (EARTH_RADIUS_M * Math.max(Math.cos(radians(midLat)), 0.01)));
  return [
    Math.max(-90, south - dLat),
    Math.max(-180, west - dLon),
    Math.min(90, north + dLat),
    Math.min(180, east + dLon),
  ];
}

/** The overlap of two boxes; null when they do not overlap. */
export function intersectBoxes(a: GeoBox, b: GeoBox): GeoBox | null {
  const box: GeoBox = [
    Math.max(a[0], b[0]),
    Math.max(a[1], b[1]),
    Math.min(a[2], b[2]),
    Math.min(a[3], b[3]),
  ];
  return box[0] < box[2] && box[1] < box[3] ? box : null;
}

/**
 * Where a layer serves tiles: the positions plus `marginM`, cut to the layer's `coverage`.
 * Null when nothing limits the view (no margin known and no coverage) or nothing is served.
 */
export function servedBox(
  points: readonly { lat: number; lon: number }[],
  marginM: number | null | undefined,
  coverage: GeoBox | null | undefined,
): GeoBox | null {
  const around = marginM && marginM > 0 ? marginBox(points, marginM) : null;
  if (around && coverage) return intersectBoxes(around, coverage);
  return around ?? coverage ?? null;
}

/** Web Mercator x and y of a position, 0..1 over the world. */
function mercator(lat: number, lon: number): [number, number] {
  const clamped = Math.max(-MAX_LAT, Math.min(MAX_LAT, lat));
  const sin = Math.sin(radians(clamped));
  return [(lon + 180) / 360, 0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)];
}

/**
 * The lowest zoom at which `box` covers a view of `width` x `height` pixels completely (both
 * sides), rounded up to `snap`; null without a size (a map that is not laid out yet).
 */
export function fillZoom(box: GeoBox, width: number, height: number, snap = 0.25): number | null {
  if (!(width > 0 && height > 0)) return null;
  const [x0, y0] = mercator(box[2], box[1]);
  const [x1, y1] = mercator(box[0], box[3]);
  const spanX = Math.max(x1 - x0, 1e-12);
  const spanY = Math.max(y1 - y0, 1e-12);
  const zoom = Math.log2(Math.max(width / (spanX * TILE_SIZE), height / (spanY * TILE_SIZE)));
  const step = snap > 0 ? snap : 1;
  // Within 1% of a step counts as on it (no extra step for rounding noise).
  return Math.ceil(Math.round(zoom / (step / 100)) / 100) * step;
}

function radians(value: number): number {
  return (value * Math.PI) / 180;
}

function degrees(value: number): number {
  return (value * 180) / Math.PI;
}
