<!--
  The picture of a mower's live camera (ADR 0032, ADR 0044): it owns the viewer (one camera
  session while it is mounted; unmounting or leaving the page leaves the channel) and draws its
  tiles. A `fake` session (the simulator) draws the test pattern on a canvas, a `relay` session
  (ADR 0045) plays MowDeck's camera relay over WHEP and an `agora` session the vendor's channel,
  both in our own muted <video> elements. With both front cameras the tiles
  sit side by side (stacked when the stage is narrow); a tile whose camera delivers nothing
  within 8 s says so instead of showing another camera, and two tiles with the same picture say
  so. Every live tile is marked (`data-camera-video`, name, side) for AI assist's frame capture.

  Freeze: a few times a second the stage reads every shown tile's frame counter (`freeze.ts`:
  presented frames through requestVideoFrameCallback for a video). Two signals with their own
  thresholds: the hold (a picture without a new frame for longer than 1.5 s, up to 3 s for a
  steady slow stream, or one that went away after it was live) is reported through the bindable
  `stalled` and holds the mower on the drive page; the label greys the picture out under a
  hatched "picture frozen" plate (a standing picture must never look live) only after a gap of
  at least 2.5 s on a live tile (5 s right after the start) and clears at the next frame, so a
  bursty 4G stream does not flicker and a tile that is connecting says so instead. The viewer is bindable too, so
  the hosts (`CameraPanel` with its picker and diagnostics, the drive page's HUD) read the same
  session.

  Transport: the session request names the browser's camera option (`auto` unless forced to
  `relay` or `direct`). A hidden page suspends a relay session (its lease is detached at once,
  the relay stops the stream after its grace) and requests a new one when the page is visible
  again (also on `pageshow` and `focus`, which a WebKit app view may send without a
  `visibilitychange`); leaving the page closes it. A session that arrives when it is no longer
  wanted is discarded at once (its lease detached).

  Status: "requesting", "connecting", "camera starting" (the relay's source has not published
  yet: a LUBA camera takes 6 to 8 s, after a 4G stall up to about 35 s), "camera not reachable,
  trying again" (a session request that did not get through), "live", "picture frozen"; a tile
  of both cameras says "this camera is starting" on its own.

  `compact` is the drive view: the stage fills its box, the tile captions keep only the side
  badge, the status sits small in a corner. Video and canvas never take pointer events, so a
  joystick floating over the stage gets every touch.
-->
<script lang="ts">
  import { Button, ProblemNotice, tokenValue } from '@mowdeck/ui';
  import { onDestroy, onMount } from 'svelte';

  import { fleetApi, unwrap } from '#lib/api/client.js';
  import { trackSummary } from '#lib/camera/diagnostics.js';
  import { CameraPlayerFactory } from '#lib/camera/factory.js';
  import { FREEZE_POLL_MS, FreezeWatch, frameCounter } from '#lib/camera/freeze.js';
  import { cameraLabel } from '#lib/camera/labels.js';
  import type { CameraLayout, CameraTile } from '#lib/camera/layout.js';
  import type { CameraPlayer } from '#lib/camera/player.js';
  import {
    meanAbsDifference,
    SAME_PICTURE_INTERVAL_MS,
    SamePictureWatch,
    thumbnail,
  } from '#lib/camera/sameness.js';
  import type { CameraErrorKind, CameraInfo } from '#lib/camera/session.js';
  import type { PatternColors } from '#lib/camera/test-pattern.js';
  import { CameraViewer } from '#lib/camera/viewer.svelte.js';
  import { loadConfig } from '#lib/config.js';
  import { m } from '#lib/paraglide/messages.js';

  interface Props {
    mowerId: string;
    mowerName: string;
    /** The mower's latest position (for the test pattern). */
    position?: { lat: number; lon: number } | null;
    /** The drive view: fill the box, minimal captions, a small status. */
    compact?: boolean;
    /** The full-screen camera page: a taller single tile. */
    fullscreen?: boolean;
    /** The camera to show first (null: the first that publishes video). */
    cameraUid?: number | null;
    /** With a stereo pair and no explicit choice, start on the front left camera. */
    frontLeft?: boolean;
    /** The stored layout and where a change goes (default: the camera panel's per browser). */
    layout?: CameraLayout;
    saveLayout?: (layout: CameraLayout) => void;
    /** The viewer of this stage (bindable, read only for the host). */
    viewer?: CameraViewer | null;
    /** The shown picture stands or went away after it was live (bindable, read only). */
    stalled?: boolean;
  }

  let {
    mowerId,
    mowerName,
    position = null,
    compact = false,
    fullscreen = false,
    cameraUid = null,
    frontLeft = false,
    layout,
    saveLayout,
    viewer = $bindable(null),
    stalled = $bindable(false),
  }: Props = $props();

  /** The canvas and video element of each tile (0: left or single view, 1: right view). */
  let canvases = $state<(HTMLCanvasElement | null)[]>([null, null]);
  let videos = $state<(HTMLVideoElement | null)[]>([null, null]);
  let stage = $state<HTMLDivElement | null>(null);
  /** Both tiles have shown the same picture for a few readings. */
  let samePicture = $state(false);

  const ERRORS: Record<CameraErrorKind, () => string> = {
    unsupported: () => m.camera_error_unsupported(),
    not_in_plan: () => m.camera_error_not_in_plan(),
    unavailable: () => m.camera_error_unavailable(),
    not_found: () => m.camera_error_not_found(),
    insecure: () => m.camera_error_insecure(),
    disabled: () => m.camera_error_disabled(),
    browser: () => m.camera_error_browser(),
    expired: () => m.camera_error_expired(),
    failed: () => m.camera_error_failed(),
  };

  function colors(): PatternColors {
    const read = (name: Parameters<typeof tokenValue>[0], fallback: string) =>
      (stage ? tokenValue(name, stage) : '') || fallback;
    return {
      bars: [
        read('--md-color-surface', 'white'),
        read('--md-state-paused', 'goldenrod'),
        read('--md-state-returning', 'teal'),
        read('--md-state-mowing', 'green'),
        read('--md-state-charging', 'royalblue'),
        read('--md-state-error', 'firebrick'),
        read('--md-color-ink', 'black'),
      ],
      background: read('--md-color-surface-sunken', 'gainsboro'),
      ink: read('--md-color-ink', 'black'),
      inkInverse: read('--md-color-surface', 'white'),
      sweep: read('--md-color-focus', 'blue'),
    };
  }

  const factory: CameraPlayerFactory = new CameraPlayerFactory({
    canvas: (slot = 0) => canvases[slot] ?? null,
    video: (slot = 0) => videos[slot] ?? null,
    lease: {
      renew: (leaseId) =>
        unwrap(
          fleetApi.POST('/api/v1/fleet/mowers/{mower_id}/camera/session/{lease_id}/renew', {
            params: { path: { mower_id: mowerId, lease_id: leaseId } },
          }),
        ),
      detach: async (leaseId) => {
        await unwrap(
          fleetApi.DELETE('/api/v1/fleet/mowers/{mower_id}/camera/session/{lease_id}', {
            params: { path: { mower_id: mowerId, lease_id: leaseId } },
            // Sent while the page goes away (pagehide) as well.
            keepalive: true,
          }),
        );
      },
    },
    source: {
      label: (uid: number | null): string => {
        const camera = cam.cameras.find((c: CameraInfo) => c.uid === uid);
        return camera ? `${mowerName} · ${cameraLabel(camera)}` : mowerName;
      },
      mirror: (uid: number | null): boolean => uid !== null && cam.pair?.right.uid === uid,
      position: () => position,
      colors,
    },
  });

  // Created once per mounted stage: one session per stage (the props are read at creation).
  const initial = (() => ({ layout, saveLayout, cameraUid }))();
  const cam: CameraViewer = new CameraViewer({
    request: (id, transport) =>
      unwrap(
        fleetApi.POST('/api/v1/fleet/mowers/{mower_id}/camera/session', {
          params: { path: { mower_id: id } },
          body: { transport },
        }),
      ),
    // A relay lease nobody plays: end it now, not after the relay's unbound time.
    discard: async (session) => {
      const leaseId = session.relay?.lease_id;
      if (session.provider === 'relay' && leaseId) await factory.detachLease(leaseId);
    },
    createPlayer: (session, events): Promise<CameraPlayer> => factory.create(session, events),
    secure: () => window.isSecureContext,
    cameraEnabled: async () => (await loadConfig()).cameraEnabled,
    ...(initial.layout ? { layout: initial.layout } : {}),
    ...(initial.saveLayout ? { saveLayout: initial.saveLayout } : {}),
  });
  if (initial.cameraUid !== null) cam.cameraUid = initial.cameraUid;

  onMount(() => {
    // Hand the viewer to the host (its picker, diagnostics and readouts read the same session).
    if (viewer !== cam) viewer = cam;
    void cam.open(mowerId);
    const leave = () => void cam.close();
    // Back from the back/forward cache: the session was closed on pagehide.
    const back = (event: PageTransitionEvent) => {
      if (event.persisted) void cam.open(mowerId);
    };
    // A hidden tab or app holds no relay stream (ADR 0045): detach now, reconnect on return.
    const visibility = () =>
      void (document.visibilityState === 'hidden' ? cam.suspend() : cam.resume());
    // A WebKit app view coming back to the foreground may send only `pageshow` or `focus`: a
    // suspended session resumes on whichever arrives first (resume is a no-op otherwise).
    const shown = () => {
      if (document.visibilityState !== 'hidden') void cam.resume();
    };
    window.addEventListener('pagehide', leave);
    window.addEventListener('pageshow', back);
    window.addEventListener('pageshow', shown);
    window.addEventListener('focus', shown);
    document.addEventListener('visibilitychange', visibility);
    return () => {
      window.removeEventListener('pagehide', leave);
      window.removeEventListener('pageshow', back);
      window.removeEventListener('pageshow', shown);
      window.removeEventListener('focus', shown);
      document.removeEventListener('visibilitychange', visibility);
    };
  });
  onDestroy(() => {
    stalled = false;
    frozenShown = false;
    void cam.close();
  });

  // The drive view starts on the front left camera of a stereo pair (not on whichever camera
  // happens to publish first), unless the driver chose a camera or both.
  $effect(() => {
    const pair = cam.pair;
    if (!frontLeft || !pair || cam.cameraUid !== null || cam.both) return;
    void cam.select(pair.left.uid);
  });

  const fake = $derived(cam.session?.provider === 'fake');
  /**
   * A relay tile is live at its first presented frame, and WebKit (Safari) presents no frame of
   * a `<video>` that is not rendered: the relay's video stays rendered while it connects.
   */
  const relay = $derived(cam.session?.provider === 'relay');
  const tiles = $derived(cam.tiles);
  const failed = $derived(cam.phase === 'error');
  const layoutName = $derived(cam.both ? 'both' : 'single');

  /** The tile in `slot` (the right one only in the side-by-side view). */
  function tileAt(slot: number): CameraTile | undefined {
    return tiles[slot];
  }

  /** The tile's picture element (canvas for the simulator, video otherwise). */
  function pictureOf(slot: number): HTMLCanvasElement | HTMLVideoElement | null {
    return (fake ? canvases[slot] : videos[slot]) ?? null;
  }

  function tileName(tile: CameraTile): string {
    return tile.camera ? cameraLabel(tile.camera) : '';
  }

  function tileStats(tile: CameraTile): string | null {
    if (compact || tile.status !== 'live' || tile.uid === null) return null;
    return trackSummary(cam.tracks.find((t) => t.uid === tile.uid));
  }

  // Two views must never show the same stream without saying so: compare small thumbnails.
  $effect(() => {
    const live = cam.both && tiles.every((t) => t.status === 'live');
    samePicture = false;
    if (!live) return;
    const watch = new SamePictureWatch();
    const timer = setInterval(() => {
      const left = pictureOf(0);
      const right = pictureOf(1);
      const a = left ? thumbnail(left) : null;
      const b = right ? thumbnail(right) : null;
      samePicture = watch.observe(a && b ? meanAbsDifference(a, b) : null);
    }, SAME_PICTURE_INTERVAL_MS);
    return () => clearInterval(timer);
  });

  // A standing picture must never look live: read the frame counters a few times a second.
  const watch = new FreezeWatch();
  /** "Bild steht" is shown (the label, with hysteresis; `stalled` is the stricter hold). */
  let frozenShown = $state(false);
  $effect(() => {
    const timer = setInterval(() => {
      const shown = cam.tiles.slice(0, cam.both ? 2 : 1);
      const frozen = watch.observe(
        shown.map((tile, slot) => ({
          live: tile.status === 'live' && cam.phase === 'live',
          frames: frameCounter(pictureOf(slot)),
        })),
        Date.now(),
      );
      if (frozen !== stalled) stalled = frozen;
      if (watch.labelled !== frozenShown) frozenShown = watch.labelled;
    }, FREEZE_POLL_MS);
    return () => clearInterval(timer);
  });

  /** A shown tile has its picture (fake sessions always draw). */
  const showingPicture = $derived(fake || tiles.some((tile) => tile.status === 'live'));

  const status = $derived.by(() => {
    if (frozenShown && !failed) return m.camera_frozen();
    if (cam.missing && cam.phase !== 'error') return ''; // the notice on the stage says it
    switch (cam.phase) {
      case 'requesting':
        return cam.unreachable ? m.camera_unreachable_retrying() : m.camera_requesting();
      case 'connecting':
        if (cam.unreachable) return m.camera_unreachable_retrying();
        return cam.starting.length ? m.camera_starting() : m.camera_connecting();
      case 'waiting':
        return m.camera_waiting();
      case 'live':
        if (fake) return m.camera_test_pattern();
        // Every shown tile lost its picture and reconnects (a muxer restart after a stall, a
        // media server restart): say what happens, never "Live" over a picture that is gone.
        if (!showingPicture)
          return cam.starting.length ? m.camera_starting() : m.camera_connecting();
        return m.camera_live();
      default:
        return '';
    }
  });
</script>

<div
  class="stage"
  class:compact
  class:fullscreen
  bind:this={stage}
  data-testid="camera-stage"
  data-layout={layoutName}
  data-phase={cam.phase}
  data-provider={cam.session?.provider ?? ''}
  data-transport={cam.transport ?? ''}
  data-starting={cam.starting.length ? 'true' : 'false'}
  data-frozen={frozenShown && !failed ? 'true' : 'false'}
>
  {#if cam.phase === 'error' && cam.error}
    <div class="problem" data-testid="camera-error" data-kind={cam.error}>
      <ProblemNotice
        tone={cam.error === 'unsupported' || cam.error === 'not_in_plan' ? 'info' : 'error'}
        title={ERRORS[cam.error]()}
        reference={cam.correlationId}
        referenceLabel={m.problem_reference()}
      />
      {#if cam.error !== 'unsupported' && cam.error !== 'not_in_plan'}
        <Button icon="play" testid="camera-retry" onclick={() => void cam.open(mowerId)}
          >{m.camera_retry()}</Button
        >
      {/if}
    </div>
  {/if}
  <div class="tiles" class:both={cam.both}>
    {#each [0, 1] as slot (slot)}
      {@const tile = tileAt(slot)}
      {@const live = !!tile && tile.status === 'live' && !failed}
      {@const shownTile = !!tile && (slot === 0 || cam.both)}
      <figure
        class="tile"
        class:hidden={!shownTile}
        data-testid={slot === 0 ? 'camera-tile' : 'camera-tile-right'}
        data-slot={slot}
        data-status={tile?.status ?? ''}
        data-side={tile?.side ?? ''}
        data-camera-uid={tile?.uid ?? ''}
      >
        <canvas
          bind:this={canvases[slot]}
          class="picture"
          class:hidden={!fake || !live || !shownTile}
          width="640"
          height="360"
          aria-label={tile
            ? `${m.camera_test_pattern()} · ${tileName(tile)}`
            : m.camera_test_pattern()}
          data-testid={slot === 0 ? 'camera-pattern' : 'camera-pattern-right'}
          data-camera-video={fake && live && shownTile ? '' : undefined}
          data-camera-live={live ? 'true' : 'false'}
          data-camera-name={tile?.camera?.name ?? ''}
          data-camera-side={cam.both ? (tile?.side ?? '') : ''}
        ></canvas>
        <video
          bind:this={videos[slot]}
          class="picture"
          class:hidden={fake || !cam.session || !shownTile || (!live && !relay)}
          autoplay
          muted
          playsinline
          data-testid={slot === 0 ? 'camera-video' : 'camera-video-right'}
          data-camera-video={!fake && live && shownTile ? '' : undefined}
          data-camera-live={live ? 'true' : 'false'}
          data-camera-name={tile?.camera?.name ?? ''}
          data-camera-side={cam.both ? (tile?.side ?? '') : ''}
        ></video>
        {#if tile && shownTile && !failed}
          {#if tile.status === 'missing'}
            <p class="notice" data-testid="camera-no-picture">{m.camera_no_picture()}</p>
          {:else if tile.status === 'no_data'}
            <p class="notice" data-testid="camera-no-data">{m.camera_no_data()}</p>
          {:else if tile.status === 'starting' && cam.both}
            <p class="notice quiet-notice" data-testid="camera-tile-starting">
              {m.camera_tile_starting()}
            </p>
          {:else if tile.status === 'connecting' && cam.both}
            <p class="notice quiet-notice">{m.camera_tile_waiting()}</p>
          {/if}
          {#if cam.both || tileStats(tile)}
            <figcaption class="caption mono" data-testid="camera-tile-caption">
              {#if tile.side}<span class="side" aria-hidden="true"
                  >{tile.side === 'left'
                    ? m.camera_side_left_short()
                    : m.camera_side_right_short()}</span
                >{/if}
              <span class:sr-only={compact}>{tileName(tile)}</span>
              {#if tileStats(tile)}<span class="stats">{tileStats(tile)}</span>{/if}
            </figcaption>
          {/if}
        {/if}
      </figure>
    {/each}
  </div>
  {#if frozenShown && !failed}
    <div class="frozen" role="alert" data-testid="camera-frozen">
      <strong>{m.camera_frozen()}</strong>
      {#if compact}<span>{m.camera_frozen_hint()}</span>{/if}
    </div>
  {/if}
  {#if samePicture && !failed}
    <p class="same" role="status" data-testid="camera-same-picture">{m.camera_same_picture()}</p>
  {/if}
  {#if status}
    <span
      class="status mono"
      data-testid="camera-status"
      data-frozen={frozenShown ? 'true' : 'false'}
    >
      {#if cam.phase === 'live' && showingPicture && !frozenShown}<span
          class="dot"
          aria-hidden="true"
        ></span>{/if}
      {status}
    </span>
  {/if}
</div>

<style>
  .stage {
    position: relative;
    display: grid;
    overflow: hidden;
    border-radius: var(--md-radius-2);
    background: var(--md-color-ink);
    container-type: inline-size;
  }

  .tiles {
    grid-area: 1 / 1;
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  /* Side by side when the stage is wide enough, stacked (left above right) otherwise. */
  @container (min-width: 560px) {
    .tiles.both {
      grid-template-columns: 1fr 1fr;
    }
  }

  .stage.compact {
    height: 100%;
    border-radius: 0;
    background: var(--md-hud-glass-strong);
  }

  .compact .tiles {
    min-height: 0;
    grid-auto-rows: minmax(0, 1fr);
  }

  .tile {
    position: relative;
    display: grid;
    place-items: center;
    margin: 0;
    min-width: 0;
    overflow: hidden;
    aspect-ratio: 16 / 9;
    background: var(--md-color-ink);
  }

  .compact .tile {
    min-height: 0;
    aspect-ratio: auto;
    background: var(--md-hud-glass-strong);
  }

  .fullscreen .tiles:not(.both) .tile {
    max-height: calc(var(--md-viewport-height) - 260px);
  }

  .picture {
    grid-area: 1 / 1;
    width: 100%;
    height: 100%;
    object-fit: contain;

    /* Touches go to whatever floats above the picture (the drive view's joystick). */
    pointer-events: none;
  }

  /* A standing picture must never look live: greyed out, dimmed, hatched over. */
  .stage[data-frozen='true'] .picture {
    filter: grayscale(1) brightness(0.55);
  }

  .frozen {
    position: absolute;
    inset: 0;
    z-index: 3;
    display: grid;
    place-content: center;
    justify-items: center;
    gap: var(--md-space-1);
    padding: var(--md-space-3);
    border: 3px solid var(--md-state-error);
    background: repeating-linear-gradient(
      -45deg,
      var(--md-hud-glass) 0 10px,
      transparent 10px 20px
    );
    color: var(--md-hud-ink);
    text-align: center;
    pointer-events: none;
  }

  .frozen strong {
    padding: var(--md-space-1) var(--md-space-3);
    border-radius: var(--md-radius-1);
    background: var(--md-state-error);
    color: var(--md-color-ink-inverse);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-16);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .frozen span {
    max-width: 32ch;
    padding: var(--md-space-1) var(--md-space-2);
    border-radius: var(--md-radius-1);
    background: var(--md-hud-glass-strong);
    font-size: var(--md-font-size-13);
  }

  .notice {
    grid-area: 1 / 1;
    z-index: 1;
    margin: 0;
    padding: var(--md-space-2) var(--md-space-3);
    border-radius: var(--md-radius-1);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
  }

  .quiet-notice {
    background: transparent;
    color: var(--md-color-surface);
    font-size: var(--md-font-size-13);
  }

  .caption {
    position: absolute;
    right: var(--md-space-2);
    bottom: var(--md-space-2);
    left: var(--md-space-2);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-1) var(--md-space-2);
    pointer-events: none;
    color: var(--md-color-surface);
    font-size: var(--md-font-size-12);
    text-shadow: 0 1px 2px var(--md-color-ink);
  }

  .compact .caption {
    color: var(--md-hud-ink);
    text-shadow: none;
  }

  .side {
    display: inline-grid;
    place-items: center;
    min-width: 1.5em;
    padding: 0 var(--md-space-1);
    border: var(--md-hairline) solid currentcolor;
    border-radius: var(--md-radius-1);
    font-weight: var(--md-weight-semibold);
  }

  .compact .side {
    background: var(--md-hud-glass);
  }

  .stats {
    margin-left: auto;
    font-variant-numeric: tabular-nums;
  }

  .same {
    position: absolute;
    right: var(--md-space-2);
    top: var(--md-space-2);
    z-index: 2;
    max-width: 60%;
    margin: 0;
    padding: var(--md-space-1) var(--md-space-2);
    border-radius: var(--md-radius-1);
    background: var(--md-state-error);
    color: var(--md-color-surface);
    font-size: var(--md-font-size-12);
  }

  .hidden {
    display: none;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  .problem {
    grid-area: 1 / 1;
    place-self: center;
    z-index: 4;
    display: grid;
    gap: var(--md-space-2);
    justify-items: start;
    max-width: 90%;
    padding: var(--md-space-3);
    border-radius: var(--md-radius-2);
    background: var(--md-color-surface);
  }

  .status {
    position: absolute;
    z-index: 4;
    top: var(--md-space-2);
    left: var(--md-space-2);
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    padding: var(--md-space-0_5) var(--md-space-2);
    border-radius: var(--md-radius-1);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
  }

  .status[data-frozen='true'] {
    background: var(--md-state-error);
    color: var(--md-color-ink-inverse);
  }

  /* The drive view puts its own HUD on top: the status moves to the lower left, glass. */
  .compact .status {
    top: auto;
    bottom: var(--md-space-2);
    background: var(--md-hud-glass);
    color: var(--md-hud-ink);
    font-size: var(--md-font-size-11);
  }

  .compact .status[data-frozen='true'] {
    background: var(--md-state-error);
    color: var(--md-color-ink-inverse);
  }

  .dot {
    width: 8px;
    height: 8px;
    border-radius: var(--md-radius-round);
    background: var(--md-state-error);
  }

  .mono {
    font-family: var(--md-font-mono);
  }
</style>
