/**
 * The camera panel's layout (stereo cameras): one camera (`single`) or both front cameras side
 * by side (`both`, stacked on phones), kept per browser in `localStorage` (`mowdeck.cameraLayout`).
 * The tiles say for every view what it shows: live video, still
 * connecting, a camera that does not publish (`missing`) or one that delivered nothing within
 * {@link NO_DATA_MS} (`no_data`). A tile never shows another camera's picture in its place.
 */
import { sideOf, type CameraInfo, type StereoPair } from './session';

export type CameraLayout = 'single' | 'both';

const LAYOUTS: readonly CameraLayout[] = ['single', 'both'];

export const LAYOUT_KEY = 'mowdeck.cameraLayout';

/** A wanted camera that shows no picture after this long is reported as delivering no data. */
export const NO_DATA_MS = 8_000;

export type TileStatus = 'live' | 'connecting' | 'missing' | 'no_data';

export interface CameraTile {
  /** Position in the panel: 0 (left or the single view), 1 (right). */
  slot: number;
  /** The camera shown (null: the first that publishes video, none yet). */
  uid: number | null;
  camera: CameraInfo | undefined;
  side: 'left' | 'right' | null;
  status: TileStatus;
}

export interface TileInput {
  layout: CameraLayout;
  pair: StereoPair | null;
  cameras: readonly CameraInfo[];
  /** The single view's choice (null: the first publisher). */
  cameraUid: number | null;
  /** The uid the single view shows. */
  activeUid: number | null;
  /** Uids that publish video (null: not known yet). */
  publishing: readonly number[] | null;
  /** Uids whose picture is attached to a tile. */
  live: readonly number[];
  /** Wanted uids that delivered nothing within {@link NO_DATA_MS}. */
  stalled: readonly number[];
}

/** `both` only when the session has a stereo pair, otherwise `single`. */
export function effectiveLayout(layout: CameraLayout, pair: StereoPair | null): CameraLayout {
  return layout !== 'single' && pair ? layout : 'single';
}

/** The layout shows both front cameras side by side. */
export function showsPair(layout: CameraLayout, pair: StereoPair | null): boolean {
  return effectiveLayout(layout, pair) !== 'single';
}

/** What the player is asked to show: the pair's uids side by side, or the single choice. */
export function layoutChoice(
  layout: CameraLayout,
  pair: StereoPair | null,
  cameraUid: number | null,
): number | null | number[] {
  return showsPair(layout, pair) && pair ? [pair.left.uid, pair.right.uid] : cameraUid;
}

/** The uids that must deliver a picture (an explicit choice; the first publisher is not). */
export function wantedUids(choice: number | null | readonly number[]): number[] {
  if (choice === null) return [];
  return typeof choice === 'number' ? [choice] : [...choice];
}

function status(
  uid: number | null,
  input: Pick<TileInput, 'publishing' | 'live' | 'stalled'>,
): TileStatus {
  if (uid === null) return 'connecting';
  if (input.live.includes(uid)) return 'live';
  if (input.publishing?.length && !input.publishing.includes(uid)) return 'missing';
  if (input.stalled.includes(uid)) return 'no_data';
  return 'connecting';
}

/** The tiles of the panel, left to right. */
export function tilesOf(input: TileInput): CameraTile[] {
  const camera = (uid: number | null) =>
    uid === null ? undefined : input.cameras.find((c) => c.uid === uid);
  if (showsPair(input.layout, input.pair) && input.pair) {
    return [input.pair.left, input.pair.right].map((c, slot) => ({
      slot,
      uid: c.uid,
      camera: c,
      side: slot === 0 ? 'left' : 'right',
      status: status(c.uid, input),
    }));
  }
  const uid = input.cameraUid ?? input.activeUid;
  return [
    {
      slot: 0,
      uid,
      camera: camera(uid),
      side: sideOf(input.pair, uid),
      status: status(uid, input),
    },
  ];
}

function safeStorage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

export function loadLayout(storage: Pick<Storage, 'getItem'> | null = safeStorage()): CameraLayout {
  try {
    const stored = storage?.getItem(LAYOUT_KEY);
    return LAYOUTS.find((layout) => layout === stored) ?? 'single';
  } catch {
    return 'single';
  }
}

export function saveLayout(
  layout: CameraLayout,
  storage: Pick<Storage, 'setItem'> | null = safeStorage(),
): void {
  try {
    storage?.setItem(LAYOUT_KEY, layout);
  } catch {
    // private mode: the layout lasts until the page closes
  }
}
