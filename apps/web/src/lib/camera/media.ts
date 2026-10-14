/**
 * Our own `<video>` element as the sink of remote tracks (ADR 0032). The Agora SDK's
 * `track.play(element)` builds its own player and applies inline styles, which the CSP refuses
 * (`style-src` has no 'unsafe-inline'). Instead the panel's element gets a `MediaStream` of the
 * raw `MediaStreamTrack`s and plays it itself, muted and inline. No SDK import here, so the
 * helpers are unit tested with fakes.
 */

/** A track that hands out its browser track (`IRemoteVideoTrack`, `IRemoteAudioTrack`). */
export interface TrackSource {
  getMediaStreamTrack(): MediaStreamTrack;
}

/** Builds the stream (replaced in tests, where jsdom has no `MediaStream`). */
export type StreamFactory = (tracks: MediaStreamTrack[]) => MediaStream;

export const browserStream: StreamFactory = (tracks) => new MediaStream(tracks);

/**
 * Show `sources` in `video`: a new stream of their browser tracks, `muted` as asked, inline
 * on phones. Resolves when playback started (or the browser refused autoplay, which a muted
 * inline video normally never hits).
 */
export async function attachTracks(
  video: HTMLVideoElement,
  sources: (TrackSource | null | undefined)[],
  muted: boolean,
  stream: StreamFactory = browserStream,
): Promise<void> {
  const tracks = sources
    .filter((source): source is TrackSource => !!source)
    .map((source) => source.getMediaStreamTrack());
  video.muted = muted;
  video.playsInline = true;
  video.setAttribute('playsinline', '');
  video.srcObject = stream(tracks);
  try {
    await video.play();
  } catch {
    // Autoplay refused or the element went away: the picture starts with the next attach.
  }
}

/** Empty `video` (unsubscribe, camera gone, leave). */
export function detachTracks(video: HTMLVideoElement): void {
  try {
    video.pause();
  } catch {
    // jsdom and detached elements
  }
  video.srcObject = null;
}

/** The uids that publish video right now, in join order (`without`: a user that just left). */
export function videoPublishers(
  users: readonly { uid: number | string; hasVideo: boolean }[],
  without?: number | string,
): number[] {
  return users
    .filter((user) => user.hasVideo && user.uid !== without)
    .map((user) => Number(user.uid))
    .filter((uid) => Number.isFinite(uid));
}
