/**
 * The names of a mower's cameras in the user's language: from the camera's `placement`, else
 * from well-known ids (Mammotion slots `0` front left, `1` front right, `2` rear; the simulator's
 * `front_left` and `front_right`). Unknown cameras keep the name from the session.
 */
import { m } from '$lib/paraglide/messages';

import { placementOf, type CameraInfo, type CameraPlacement } from './session';

const BY_PLACEMENT: Record<CameraPlacement, () => string> = {
  front_left: () => m.camera_front_left(),
  front_right: () => m.camera_front_right(),
  front: () => m.camera_front(),
  rear: () => m.camera_rear(),
};

const BY_ID: Record<string, () => string> = {
  '2': () => m.camera_rear(),
  front: () => m.camera_front(),
  rear: () => m.camera_rear(),
};

export function cameraLabel(camera: Pick<CameraInfo, 'id' | 'name' | 'placement'>): string {
  const placement = placementOf(camera);
  if (placement) return BY_PLACEMENT[placement]();
  return BY_ID[camera.id.trim().toLowerCase()]?.() ?? camera.name;
}

export interface CameraOption {
  value: string;
  label: string;
}

/** The picker value of the side-by-side layout. */
export const BOTH = 'both';

/**
 * The picker's cameras: those whose uid publishes video right now (`publishing`, null: not
 * known, all with a uid), plus the chosen one while it is missing, so the choice stays visible.
 * A publisher the session does not list appears as "Camera <uid>". With `both` (the session
 * has a stereo pair) the side-by-side view comes last; it is offered even when a camera of the
 * pair does not publish, so its tile can say that it delivers nothing.
 */
export function cameraOptions(
  cameras: readonly CameraInfo[],
  publishing: readonly number[] | null,
  chosen: number | null,
  both = false,
): CameraOption[] {
  const listed = cameras.filter(
    (c): c is CameraInfo & { uid: number } => c.uid !== null && c.uid !== undefined,
  );
  const options = listed
    .filter((c) => publishing === null || publishing.includes(c.uid) || c.uid === chosen)
    .map((c) => ({ value: String(c.uid), label: cameraLabel(c) }));
  const known = new Set(listed.map((c) => c.uid));
  for (const uid of publishing ?? []) {
    if (!known.has(uid)) options.push({ value: String(uid), label: m.camera_unknown({ uid }) });
  }
  if (both) options.push({ value: BOTH, label: m.camera_both() });
  return options;
}
