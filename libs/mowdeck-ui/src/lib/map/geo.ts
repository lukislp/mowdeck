/**
 * Local metres to WGS84 and back: an exact port of `mowdeck_control.geo` (local tangent plane,
 * equirectangular). The map contract keeps geometry in the mower's local frame; only the
 * browser converts for display. `src/tests/geo.test.ts` checks this port against golden vectors
 * printed by `scripts/geo_vectors.py` from the Python implementation.
 */

/** WGS84 equatorial radius in metres (as in `mowdeck_control.geo.EARTH_RADIUS_M`). */
export const EARTH_RADIUS_M = 6_378_137.0;

/** Anchors the local frame on the earth (`GeoReference` of the contracts). */
export interface GeoReference {
  origin_lat: number;
  origin_lon: number;
  /** Local axes are east and north rotated counter-clockwise by this angle. */
  rotation_deg?: number;
}

// The same factors as CPython's math.radians() and math.degrees(), for bit-identical results.
const RAD = Math.PI / 180;
const DEG = 180 / Math.PI;

/** A local point `(x, y)` in metres to `[lat, lon]` in degrees. */
export function toWgs84(ref: GeoReference, x: number, y: number): [number, number] {
  const r = (ref.rotation_deg ?? 0) * RAD;
  const east = x * Math.cos(r) - y * Math.sin(r);
  const north = x * Math.sin(r) + y * Math.cos(r);
  const lat = ref.origin_lat + (north / EARTH_RADIUS_M) * DEG;
  const lonScale = EARTH_RADIUS_M * Math.cos(ref.origin_lat * RAD);
  const lon = ref.origin_lon + (east / lonScale) * DEG;
  return [lat, lon];
}

/** `[lat, lon]` in degrees to a local point `[x, y]` in metres; the inverse of {@link toWgs84}. */
export function fromWgs84(ref: GeoReference, lat: number, lon: number): [number, number] {
  const north = (lat - ref.origin_lat) * RAD * EARTH_RADIUS_M;
  const lonScale = EARTH_RADIUS_M * Math.cos(ref.origin_lat * RAD);
  const east = (lon - ref.origin_lon) * RAD * lonScale;
  const r = (ref.rotation_deg ?? 0) * RAD;
  const x = east * Math.cos(r) + north * Math.sin(r);
  const y = -east * Math.sin(r) + north * Math.cos(r);
  return [x, y];
}

/** A local direction (radians, counter-clockwise from +x) to a compass heading in degrees
 * (0 = north, clockwise) in `[0, 360)`. */
export function compassHeading(ref: GeoReference, localAngleRad: number): number {
  const enuDeg = localAngleRad * DEG + (ref.rotation_deg ?? 0);
  const heading = pyMod(90 - enuDeg, 360);
  return heading >= 360 ? 0 : heading;
}

/** Python's `%` for floats: the result has the sign of the divisor. */
function pyMod(a: number, b: number): number {
  return ((a % b) + b) % b;
}
