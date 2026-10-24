/**
 * Live camera sessions (ADR 0032): the types of `POST /api/v1/fleet/mowers/{id}/camera/session`,
 * the refresh timing and the error words. The session holds a short-lived secret token: it lives
 * in memory only, is never stored and never logged.
 */
import type { ApiProblem } from '#lib/api/problem.js';
import type { components } from '#lib/generated/fleet.js';

export type CameraSession = components['schemas']['CameraSession'];
export type CameraInfo = components['schemas']['CameraInfo'];
export type CameraPlacement = components['schemas']['CameraPlacement'];
export type RelayEndpoint = components['schemas']['RelayEndpoint'];

/** A camera whose Agora uid is known. */
export type UidCamera = CameraInfo & { uid: number };

/** The two front cameras of a mower (LUBA 2 AWD X): shown side by side, captured as a pair. */
export interface StereoPair {
  left: UidCamera;
  right: UidCamera;
}

/** Why the camera does not show, in categories the panel turns into words. */
export type CameraErrorKind =
  | 'unsupported'
  | 'not_in_plan'
  | 'unavailable'
  | 'not_found'
  | 'insecure'
  | 'disabled'
  | 'browser'
  | 'expired'
  | 'failed';

/** An error of the camera path with its category. */
export class CameraError extends Error {
  readonly kind: CameraErrorKind;

  constructor(kind: CameraErrorKind, message?: string) {
    super(message ?? kind);
    this.name = 'CameraError';
    this.kind = kind;
  }
}

/** The category of a refused session request. */
export function cameraErrorOf(problem: ApiProblem): CameraErrorKind {
  if (problem.slug === 'camera-unsupported') return 'unsupported';
  if (problem.slug === 'feature-not-in-plan') return 'not_in_plan';
  if (problem.status === 404) return 'not_found';
  if (problem.status === 503 || problem.status === 0) return 'unavailable';
  return 'failed';
}

/** Renew this long before the token expires (and never later than 80 % of its lifetime). */
export const RENEW_MARGIN_MS = 60_000;
/** Never renew more often than this (a session that is already about to expire). */
export const MIN_RENEW_DELAY_MS = 5_000;

/** Milliseconds until the session should be renewed. */
export function renewDelayMs(session: Pick<CameraSession, 'expires_at'>, now: number): number {
  const left = Date.parse(session.expires_at) - now;
  if (!Number.isFinite(left)) return MIN_RENEW_DELAY_MS;
  const delay = Math.min(left - RENEW_MARGIN_MS, left * 0.8);
  return Math.max(MIN_RENEW_DELAY_MS, Math.round(delay));
}

/** The base64 salt of an encrypted channel as bytes (Agora `setEncryptionConfig`). */
export function decodeSalt(salt: string): Uint8Array {
  const binary = atob(salt.trim());
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** The Agora uid of a camera: its own when known, otherwise null (first video publisher). */
export function cameraUid(camera: CameraInfo | undefined): number | null {
  return camera?.uid ?? null;
}

/**
 * Same channel and provider: a token renewal is enough, no rejoin. Two relay sessions are the
 * same only with the same lease (a new session request always opens a new lease).
 */
export function sameChannel(a: CameraSession, b: CameraSession): boolean {
  if (a.provider === 'relay' || b.provider === 'relay') {
    return !!a.relay && a.relay.lease_id === b.relay?.lease_id;
  }
  return a.provider === b.provider && a.app_id === b.app_id && a.channel === b.channel;
}

/** A session of MowDeck's camera relay (ADR 0045): WHEP per camera, a lease, no vendor token. */
export function isRelay(session: CameraSession | null | undefined): boolean {
  return session?.provider === 'relay' && !!session.relay;
}

/** Older sessions without `placement`: Mammotion slot ids and the simulator's ids. */
const PLACEMENT_BY_ID: Record<string, CameraPlacement> = {
  '0': 'front_left',
  '1': 'front_right',
  front_left: 'front_left',
  front_right: 'front_right',
};

/** Where a camera sits: its `placement`, else derived from well-known ids (null: unknown). */
export function placementOf(camera: Pick<CameraInfo, 'id' | 'placement'>): CameraPlacement | null {
  return camera.placement ?? PLACEMENT_BY_ID[camera.id.trim().toLowerCase()] ?? null;
}

/**
 * The stereo pair of a session: a front left and a front right camera with two different
 * uids; null when the mower has no such pair (then only single views exist).
 */
export function stereoPair(cameras: readonly CameraInfo[]): StereoPair | null {
  const withUid = cameras.filter((c): c is UidCamera => c.uid !== null && c.uid !== undefined);
  const left = withUid.find((c) => placementOf(c) === 'front_left');
  const right = withUid.find((c) => placementOf(c) === 'front_right');
  if (!left || !right || left.uid === right.uid) return null;
  return { left, right };
}

/** `left` or `right` for a camera of the pair, else null. */
export function sideOf(pair: StereoPair | null, uid: number | null): 'left' | 'right' | null {
  if (!pair || uid === null) return null;
  if (uid === pair.left.uid) return 'left';
  if (uid === pair.right.uid) return 'right';
  return null;
}
