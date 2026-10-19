/**
 * Stand-in for `agora-player.ts` in a build without the Agora Web SDK (the default; the
 * published `web` image is always built this way, see `vite/release-licences.ts`). The SDK is
 * proprietary and is never part of a MowDeck release, so the direct vendor path (ADR 0032) is
 * not available: the camera relay (ADR 0045) serves every browser. Opening an `agora` session
 * reports `disabled`, exactly like an installation with `MOWDECK_WEB_CAMERA_ENABLED=false`.
 * An operator who wants the direct path builds the image themselves with
 * `--build-arg WEB_AGORA_SDK=include` (apps/web/README.md, "Agora Web SDK").
 */
import type { VideoTarget } from './factory';
import type { CameraPlayer, PlayerEvents } from './player';
import { CameraError } from './session';

/** Always refuses: this build has no Agora Web SDK. */
export function createAgoraPlayer(
  _video: VideoTarget | HTMLVideoElement,
  _events: PlayerEvents,
): CameraPlayer {
  throw new CameraError('disabled', 'this build of the web app has no Agora Web SDK');
}
