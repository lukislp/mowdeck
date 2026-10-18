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

/** Elements that resume by themselves, with the time of their last resume. */
const resumed = new WeakMap<HTMLVideoElement, number>();
/** A paused live picture is resumed at most this often. */
export const RESUME_MS = 1_000;

/**
 * Resume `video` when something other than {@link detachTracks} pauses it while it still has a
 * stream (WebKit pauses media on its own, e.g. after an interruption, and a muted live picture
 * that stands looks exactly like a frozen stream). At most once per {@link RESUME_MS}, so a
 * browser that insists on pausing does not get a loop.
 */
function keepPlaying(video: HTMLVideoElement, now: () => number): void {
  if (resumed.has(video)) return;
  resumed.set(video, -Infinity);
  video.addEventListener('pause', () => {
    if (!video.srcObject) return;
    const at = now();
    if (at - (resumed.get(video) ?? -Infinity) < RESUME_MS) return;
    resumed.set(video, at);
    void Promise.resolve()
      .then(() => video.play())
      .catch(() => undefined);
  });
}

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
  now: () => number = () => Date.now(),
): Promise<void> {
  keepPlaying(video, now);
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
