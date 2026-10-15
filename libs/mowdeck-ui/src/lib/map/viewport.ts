/**
 * Keeping map content in view when something covers the bottom of the map (a bottom sheet):
 * the pan that brings a box of container pixels inside the uncovered part, minus a padding.
 */

/** A box in container pixels (left/top inclusive, right/bottom exclusive like a DOMRect). */
export interface PixelBox {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

/** The box around container points, or null without points. */
export function pixelBox(points: readonly { x: number; y: number }[]): PixelBox | null {
  if (!points.length) return null;
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  return {
    left: Math.min(...xs),
    top: Math.min(...ys),
    right: Math.max(...xs),
    bottom: Math.max(...ys),
  };
}

export type Reveal = { kind: 'inside' } | { kind: 'pan'; dx: number; dy: number } | { kind: 'fit' };

/**
 * How to bring `box` into the map of `width` x `height` px whose bottom `insetBottom` px are
 * covered, keeping `pad` px off every edge: already `inside`, a `pan` by (dx, dy) in Leaflet's
 * `panBy` sense (positive dy moves the content up), or `fit` when the box is larger than the
 * uncovered part (zoom out to it).
 */
export function revealPan(
  box: PixelBox,
  width: number,
  height: number,
  insetBottom: number,
  pad: number,
): Reveal {
  const inner = {
    left: pad,
    top: pad,
    right: width - pad,
    bottom: height - Math.max(0, insetBottom) - pad,
  };
  if (inner.right <= inner.left || inner.bottom <= inner.top) return { kind: 'inside' };
  if (box.right - box.left > inner.right - inner.left) return { kind: 'fit' };
  if (box.bottom - box.top > inner.bottom - inner.top) return { kind: 'fit' };
  const dx =
    box.left < inner.left
      ? box.left - inner.left
      : box.right > inner.right
        ? box.right - inner.right
        : 0;
  const dy =
    box.top < inner.top
      ? box.top - inner.top
      : box.bottom > inner.bottom
        ? box.bottom - inner.bottom
        : 0;
  return dx === 0 && dy === 0 ? { kind: 'inside' } : { kind: 'pan', dx, dy };
}
