/**
 * The projection adapter of `MowerMap`: one interface for both rendering modes, so the layer
 * code never knows which one is active.
 *
 * - `local` (default, no tiles): Leaflet's `CRS.Simple` in the mower's local metres. A Leaflet
 *   `[lat, lng]` is `[y, x]`, so the metric grid stays metric and crisp at every zoom, and no
 *   third party learns where the garden is.
 * - `geo` (tiles configured): `EPSG:3857`; local metres become WGS84 through the geo port.
 *
 * Screen rotations are clockwise degrees from "up", for CSS `rotate()`.
 */
import { compassHeading, fromWgs84, toWgs84, type GeoReference } from './geo';

export type ProjectionMode = 'local' | 'geo';

/** A Leaflet `LatLngTuple` (`[lat, lng]`; in `local` mode `[y, x]` in metres). */
export type MapCoord = [number, number];

export interface Projection {
  readonly mode: ProjectionMode;
  /** A local point (metres) to map coordinates. */
  fromLocal(x: number, y: number): MapCoord;
  /** A WGS84 position (degrees) to map coordinates. */
  fromWgs84(lat: number, lon: number): MapCoord;
  /** Map coordinates back to a local point (metres). */
  toLocal(coord: MapCoord): [number, number];
  /** Screen rotation of a compass heading (0 = north, clockwise). */
  headingToScreen(compassDeg: number): number;
  /** Screen rotation of a local direction (degrees, counter-clockwise from +x). */
  localAngleToScreen(localDeg: number): number;
  /** Screen rotation of the north arrow. */
  readonly northScreen: number;
}

/** Normalise an angle to `[0, 360)`. */
export function normalizeDeg(deg: number): number {
  const d = ((deg % 360) + 360) % 360;
  return d >= 360 ? 0 : d;
}

export function createProjection(ref: GeoReference, mode: ProjectionMode): Projection {
  const rotation = ref.rotation_deg ?? 0;
  if (mode === 'local') {
    return {
      mode,
      fromLocal: (x, y) => [y, x],
      fromWgs84: (lat, lon) => {
        const [x, y] = fromWgs84(ref, lat, lon);
        return [y, x];
      },
      toLocal: ([lat, lng]) => [lng, lat],
      // Screen up is local +y, which points `rotation` degrees west of north.
      headingToScreen: (compassDeg) => normalizeDeg(compassDeg + rotation),
      localAngleToScreen: (localDeg) => normalizeDeg(90 - localDeg),
      northScreen: normalizeDeg(rotation),
    };
  }
  return {
    mode,
    fromLocal: (x, y) => toWgs84(ref, x, y),
    fromWgs84: (lat, lon) => [lat, lon],
    toLocal: ([lat, lng]) => fromWgs84(ref, lat, lng),
    headingToScreen: (compassDeg) => normalizeDeg(compassDeg),
    localAngleToScreen: (localDeg) => compassHeading(ref, localDeg * (Math.PI / 180)),
    northScreen: 0,
  };
}
