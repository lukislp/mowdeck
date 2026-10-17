/**
 * Picks the player for a session's provider. The Agora player (and with it the Agora Web SDK)
 * is imported dynamically, so it is a separate chunk that only loads when a real camera opens.
 * The targets hand out the canvas or video element of every tile (0: the left or single view,
 * 1: the right view).
 */
import { FakePlayer, type CameraPlayer, type FakeFrameSource, type PlayerEvents } from './player';
import { CameraError, type CameraSession } from './session';

/** The video element of a tile. */
export type VideoTarget = (slot: number) => HTMLVideoElement | null;

export interface PlayerTargets {
  canvas(slot?: number): HTMLCanvasElement | null;
  video(slot?: number): HTMLVideoElement | null;
  source: FakeFrameSource;
  /** The test pattern also plays through the tile's `<video>` (debug switch `camfakevideo`). */
  fakeVideo?: () => boolean;
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
      const video = this.targets.fakeVideo?.()
        ? (slot: number) => this.targets.video(slot)
        : undefined;
      return new FakePlayer(
        (slot) => this.targets.canvas(slot),
        this.targets.source,
        events,
        undefined,
        video,
      );
    }
    if (!this.targets.video(0)) throw new CameraError('failed', 'no video element');
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
