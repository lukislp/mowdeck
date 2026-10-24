/**
 * The drive page's view (ADR 0044): the live camera fills the stage with the map as a corner
 * thumbnail (`camera`), or the map fills it as before (`map`). Decided from the capabilities
 * only, never from a vendor field: a mower whose `camera` capability is missing or `none`, and a
 * camera that failed during this visit, get the map. Otherwise the viewer's stored choice wins,
 * the camera by default.
 *
 * Per browser in `localStorage` (every access guarded: private mode, blocked storage): the view
 * (`mowdeck.driveView`), whether the map thumbnail is shown in camera mode
 * (`mowdeck.driveMapThumb`) and the drive camera's own layout, one camera or both front cameras
 * (`mowdeck.driveCameraLayout`, separate from the mower page's camera panel so driving always
 * starts on the front left camera unless the driver chose "Both").
 */
import type { CameraLayout } from '#lib/camera/layout.js';

export type DriveView = 'camera' | 'map';

/** The capabilities the drive view looks at (`MowerCapabilities.camera`). */
export interface DriveViewCaps {
  camera?: string | null;
}

export const VIEW_KEY = 'mowdeck.driveView';
export const THUMB_KEY = 'mowdeck.driveMapThumb';
export const DRIVE_CAMERA_LAYOUT_KEY = 'mowdeck.driveCameraLayout';

/** The mower offers a camera stream (any access other than `none`). */
export function hasCamera(caps: DriveViewCaps | null | undefined): boolean {
  return !!caps?.camera && caps.camera !== 'none';
}

/**
 * What the stage shows: the map without a camera or after a camera error, else the stored
 * choice (default: the camera).
 */
export function driveView(
  caps: DriveViewCaps | null | undefined,
  stored: DriveView | null,
  cameraError: boolean,
): DriveView {
  if (!hasCamera(caps) || cameraError) return 'map';
  return stored ?? 'camera';
}

type Store = Pick<Storage, 'getItem' | 'setItem'>;

function safeStorage(): Store | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

function read(key: string, storage: Store | null): string | null {
  try {
    return storage?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function write(key: string, value: string, storage: Store | null): void {
  try {
    storage?.setItem(key, value);
  } catch {
    // private mode or blocked storage: the choice lasts until the page closes
  }
}

/** The stored view (null: nothing chosen yet). */
export function loadView(storage: Store | null = safeStorage()): DriveView | null {
  const value = read(VIEW_KEY, storage);
  return value === 'camera' || value === 'map' ? value : null;
}

export function saveView(view: DriveView, storage: Store | null = safeStorage()): void {
  write(VIEW_KEY, view, storage);
}

/** The map thumbnail over the camera is shown unless the viewer hid it. */
export function loadThumb(storage: Store | null = safeStorage()): boolean {
  return read(THUMB_KEY, storage) !== 'hidden';
}

export function saveThumb(shown: boolean, storage: Store | null = safeStorage()): void {
  write(THUMB_KEY, shown ? 'shown' : 'hidden', storage);
}

/** The drive camera's layout: the front left camera unless "Both" was chosen while driving. */
export function loadDriveCameraLayout(storage: Store | null = safeStorage()): CameraLayout {
  return read(DRIVE_CAMERA_LAYOUT_KEY, storage) === 'both' ? 'both' : 'single';
}

export function saveDriveCameraLayout(
  layout: CameraLayout,
  storage: Store | null = safeStorage(),
): void {
  write(DRIVE_CAMERA_LAYOUT_KEY, layout, storage);
}
