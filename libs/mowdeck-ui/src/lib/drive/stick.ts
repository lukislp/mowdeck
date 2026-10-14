/**
 * Pure joystick math for `Joystick`: a pointer offset from the pad centre (or held arrow keys)
 * becomes `{linear, angular}` in -1..1. Up is forward (+linear), left turns left (+angular), as in
 * the `ManualDrive` contract. A radial dead zone (default 10 %) maps small offsets to zero and the
 * rest is rescaled, so the output starts at 0 just outside the dead zone and reaches 1 at the rim.
 */

export interface StickVector {
  linear: number;
  angular: number;
}

export const STICK_ZERO: Readonly<StickVector> = Object.freeze({ linear: 0, angular: 0 });
export const DEFAULT_DEAD_ZONE = 0.1;

export type ArrowKey = 'ArrowUp' | 'ArrowDown' | 'ArrowLeft' | 'ArrowRight';
export const ARROW_KEYS: readonly ArrowKey[] = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'];

function round(value: number): number {
  const rounded = Math.round(value * 100) / 100;
  return rounded === 0 ? 0 : rounded; // no -0
}

/** The thumb position as a fraction of the radius, clamped to the unit circle. */
export function clampToPad(dx: number, dy: number, radius: number): { x: number; y: number } {
  if (radius <= 0) return { x: 0, y: 0 };
  const x = dx / radius;
  const y = dy / radius;
  const magnitude = Math.hypot(x, y);
  return magnitude > 1 ? { x: x / magnitude, y: y / magnitude } : { x, y };
}

/** Screen offset (y grows downwards) to a drive vector with dead zone and rescaling. */
export function stickVector(
  dx: number,
  dy: number,
  radius: number,
  deadZone: number = DEFAULT_DEAD_ZONE,
): StickVector {
  const { x, y } = clampToPad(dx, dy, radius);
  const magnitude = Math.hypot(x, y);
  const zone = Math.min(Math.max(deadZone, 0), 0.95);
  if (magnitude <= zone || magnitude === 0) return { ...STICK_ZERO };
  const scale = (magnitude - zone) / (1 - zone) / magnitude;
  return { linear: round(-y * scale), angular: round(-x * scale) };
}

/** Held arrow keys to a vector (diagonals normalised to the unit circle). */
export function keysVector(held: ReadonlySet<string>): StickVector {
  const y = (held.has('ArrowDown') ? 1 : 0) - (held.has('ArrowUp') ? 1 : 0);
  const x = (held.has('ArrowRight') ? 1 : 0) - (held.has('ArrowLeft') ? 1 : 0);
  return stickVector(x, y, 1, 0);
}

/** Scale the forward/backward part by the speed limit (0..1); turning keeps its range. */
export function limitVector(vector: StickVector, linearScale: number): StickVector {
  const scale = Math.min(Math.max(linearScale, 0), 1);
  return { linear: round(vector.linear * scale), angular: vector.angular };
}

export function isZero(vector: StickVector): boolean {
  return vector.linear === 0 && vector.angular === 0;
}
