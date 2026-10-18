/**
 * Picks the player for a session's provider: `fake` the test pattern, `relay` the WHEP player
 * of MowDeck's camera relay (ADR 0045), `agora` the direct vendor path (ADR 0032). The Agora
 * player (and with it the Agora Web SDK) is imported dynamically, so it is a separate chunk that
 * only loads when the direct path opens.
 * The targets hand out the canvas or video element of every tile (0: the left or single view,
 * 1: the right view).
 */
import { FakePlayer, type CameraPlayer, type FakeFrameSource, type PlayerEvents } from './player';
import { CameraError, type CameraSession } from './session';
import { createWhepPlayer, type RelayLeaseApi, type WhepDeps } from './whep-player';

/** The video element of a tile. */
export type VideoTarget = (slot: number) => HTMLVideoElement | null;

export interface PlayerTargets {
  canvas(slot?: number): HTMLCanvasElement | null;
  video(slot?: number): HTMLVideoElement | null;
  source: FakeFrameSource;
  /** Fleet's lease endpoints of this mower (renew, detach) for relay sessions. */
  lease?: RelayLeaseApi;
  /** WHEP player internals (replaced in tests). */
  whep?: Omit<WhepDeps, 'lease'>;
  /** Loads the Agora player module (replaced in tests). */
  loadAgora?: () => Promise<{
    createAgoraPlayer(video: VideoTarget, events: PlayerEvents): CameraPlayer;
  }>;
}

export class CameraPlayerFactory {
  constructor(private readonly targets: PlayerTargets) {}

  async create(session: CameraSession, events: PlayerEvents): Promise<CameraPlayer> {
    if (session.provider === 'fake') {
      if (!this.targets.canvas(0))
        throw new CameraError('failed', 'no canvas for the test pattern');
      return new FakePlayer((slot) => this.targets.canvas(slot), this.targets.source, events);
    }
    if (!this.targets.video(0)) throw new CameraError('failed', 'no video element');
    if (session.provider === 'relay') {
      const lease = this.targets.lease;
      if (!lease) throw new CameraError('failed', 'no lease endpoints for a relay session');
      return createWhepPlayer((slot) => this.targets.video(slot), events, {
        ...this.targets.whep,
        lease,
      });
    }
    const load = this.targets.loadAgora ?? (() => import('./agora-player'));
    try {
      const module = await load();
      return module.createAgoraPlayer((slot) => this.targets.video(slot), events);
    } catch (error) {
      if (error instanceof CameraError) throw error;
      throw new CameraError('browser', 'the video player could not be loaded');
    }
  }
}
