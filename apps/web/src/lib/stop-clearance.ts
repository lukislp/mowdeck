/**
 * Keeping layout-level floating elements clear of a page's fixed emergency stop.
 *
 * Pages with a fixed stop (the mower page's command bar, `StopBar` on drive, health and camera)
 * own the bottom edge of a phone and the foot of the instrument column on a desktop. Sheets and
 * toasts mounted in a layout ("Was ist neu", step-up, toasts) are not inside the page, so a
 * page-scoped `--md-sheet-bottom` never reaches them. The page therefore publishes the space its
 * stop takes on the root element, and `Sheet`/`ToastRegion` (@mowdeck/ui) fall back to it:
 *
 * - `--md-stop-clearance`: the height of a stop bar at the bottom edge (px).
 * - `--md-stop-inset-start`: the width of a stop rail at the left edge (phone held sideways).
 *
 * The stop keeps the highest z-index of the two (26 against 25), and nothing ever sits on it.
 */

export interface StopClearance {
  /** Height of a bar at the bottom edge, px. */
  bottom?: number;
  /** Width of a rail at the inline start edge, px. */
  insetStart?: number;
}

export const STOP_CLEARANCE = '--md-stop-clearance';
export const STOP_INSET_START = '--md-stop-inset-start';

/** Publish the stop's space on the root element; the returned function removes it again. */
export function publishStopClearance(
  clearance: StopClearance,
  root: HTMLElement = document.documentElement,
): () => void {
  const set = (name: string, value: number | undefined) => {
    if (value !== undefined && Number.isFinite(value) && value > 0) {
      root.style.setProperty(name, `${Math.ceil(value)}px`);
    } else {
      root.style.removeProperty(name);
    }
  };
  set(STOP_CLEARANCE, clearance.bottom);
  set(STOP_INSET_START, clearance.insetStart);
  return () => {
    root.style.removeProperty(STOP_CLEARANCE);
    root.style.removeProperty(STOP_INSET_START);
  };
}
