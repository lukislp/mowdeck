<!--
  The live camera of one mower (ADR 0032). Opening the panel requests a session from Fleet; a
  `fake` session (the simulator) draws the test pattern on a canvas, an `agora` session loads the
  Agora Web SDK lazily and plays the mower's video in our own <video> element, muted. The token
  is renewed before it expires; leaving the page (or unmounting the panel: the mower page's
  collapse toggle) leaves the channel. The picker lists only cameras that publish video; a
  chosen camera without video says so. With two front cameras (a stereo pair) the picker also
  offers "Both": left and right side by side (stacked when the panel is narrow), each tile in
  its own element with its camera name and live resolution and frame rate; a tile whose camera
  delivers nothing within 8 s says "no data from this camera" instead of showing another
  camera, and two tiles with the same picture say so. Every live tile is marked
  (`data-camera-video`, name, side) for AI assist's frame capture. The data note reminds that the video goes through
  Mammotion's and Agora's servers and counts against the 4G allowance. Two collapsible lines
  follow for real cameras: "Diagnose" (receive statistics every 2 s) and "Advanced" (connection
  options stored per browser).
-->
<script lang="ts">
  import { Button, ProblemNotice, SegmentedControl, Switch, tokenValue } from '@mowdeck/ui';
  import { onDestroy, onMount } from 'svelte';

  import { fleetApi, unwrap } from '$lib/api/client';
  import { type DelaySource, shown, trackSummary } from '$lib/camera/diagnostics';
  import { CameraPlayerFactory } from '$lib/camera/factory';
  import { BOTH, cameraLabel, cameraOptions as optionsOf } from '$lib/camera/labels';
  import type { CameraTile } from '$lib/camera/layout';
  import type { CameraPlayer } from '$lib/camera/player';
  import {
    meanAbsDifference,
    SAME_PICTURE_INTERVAL_MS,
    SamePictureWatch,
    thumbnail,
  } from '$lib/camera/sameness';
  import type { CameraErrorKind, CameraInfo } from '$lib/camera/session';
  import type { PatternColors } from '$lib/camera/test-pattern';
  import { CameraViewer } from '$lib/camera/viewer.svelte';
  import { loadConfig } from '$lib/config';
  import { formatNumber, formatTime } from '$lib/i18n';
  import { m } from '$lib/paraglide/messages';

  interface Props {
    mowerId: string;
    mowerName: string;
    /** The mower's latest position (for the test pattern). */
    position?: { lat: number; lon: number } | null;
    /** The full-screen page: no full-screen link, a taller stage. */
    fullscreen?: boolean;
    /** Show the full-screen link in the panel's own head (off when the host shows it). */
    link?: boolean;
    onclose?: () => void;
  }

  const delayLabel = (source: DelaySource): string =>
    ({
      end2end: m.camera_diag_delay_end2end,
      receive: m.camera_diag_delay_receive,
      transport: m.camera_diag_delay_transport,
      rtt: m.camera_diag_delay_rtt,
    })[source]();

  let {
    mowerId,
    mowerName,
    position = null,
    fullscreen = false,
    link = true,
    onclose,
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
    source: {
      label: (uid: number | null): string => {
        const camera = viewer.cameras.find((c: CameraInfo) => c.uid === uid);
        return camera ? `${mowerName} · ${cameraLabel(camera)}` : mowerName;
      },
      mirror: (uid: number | null): boolean => uid !== null && viewer.pair?.right.uid === uid,
      position: () => position,
      colors,
    },
  });

  const viewer: CameraViewer = new CameraViewer({
    request: (id) =>
      unwrap(
        fleetApi.POST('/api/v1/fleet/mowers/{mower_id}/camera/session', {
          params: { path: { mower_id: id } },
        }),
      ),
    createPlayer: (session, events): Promise<CameraPlayer> => factory.create(session, events),
    secure: () => window.isSecureContext,
    cameraEnabled: async () => (await loadConfig()).cameraEnabled,
  });

  onMount(() => {
    void viewer.open(mowerId);
    const leave = () => void viewer.close();
    window.addEventListener('pagehide', leave);
    return () => window.removeEventListener('pagehide', leave);
  });
  onDestroy(() => void viewer.close());

  const fake = $derived(viewer.session?.provider === 'fake');
  const cameraOptions = $derived(
    optionsOf(viewer.cameras, viewer.publishing, viewer.cameraUid, viewer.pair !== null),
  );
  const pickerValue = $derived(
    viewer.both ? BOTH : String(viewer.cameraUid ?? viewer.activeUid ?? ''),
  );
  const tiles = $derived(viewer.tiles);
  const failed = $derived(viewer.phase === 'error');

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
    if (tile.status !== 'live' || tile.uid === null) return null;
    return trackSummary(viewer.tracks.find((t) => t.uid === tile.uid));
  }

  // Two views must never show the same stream without saying so: compare small thumbnails.
  $effect(() => {
    const live = viewer.both && tiles.every((t) => t.status === 'live');
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

  const status = $derived.by(() => {
    if (viewer.missing && viewer.phase !== 'error') return ''; // the notice on the stage says it
    switch (viewer.phase) {
      case 'requesting':
        return m.camera_requesting();
      case 'connecting':
        return m.camera_connecting();
      case 'waiting':
        return m.camera_waiting();
      case 'live':
        return fake ? m.camera_test_pattern() : m.camera_live();
      default:
        return '';
    }
  });
  const diag = $derived(viewer.diagnostics);
  const stamp = (at: number): string => formatTime(new Date(at).toISOString());
  const quota = $derived(viewer.session?.quota ?? null);
</script>

<div
  class="camera"
  class:fullscreen
  data-testid="camera-panel"
  data-phase={viewer.phase}
  data-provider={viewer.session?.provider ?? ''}
>
  {#if (link && !fullscreen) || onclose}
    <div class="head">
      {#if link && !fullscreen}
        <Button
          variant="ghost"
          icon="expand"
          href="/mowers/{mowerId}/camera"
          label={m.camera_fullscreen()}
          testid="camera-fullscreen"
        />
      {/if}
      {#if onclose}
        <Button
          variant="ghost"
          icon="close"
          label={m.camera_close()}
          testid="camera-close"
          onclick={() => {
            void viewer.close();
            onclose?.();
          }}
        />
      {/if}
    </div>
  {/if}

  <div
    class="stage"
    class:both={viewer.both}
    bind:this={stage}
    data-layout={viewer.both ? 'both' : 'single'}
  >
    {#if viewer.phase === 'error' && viewer.error}
      <div class="problem" data-testid="camera-error" data-kind={viewer.error}>
        <ProblemNotice
          tone={viewer.error === 'unsupported' || viewer.error === 'not_in_plan' ? 'info' : 'error'}
          title={ERRORS[viewer.error]()}
          reference={viewer.correlationId}
          referenceLabel={m.problem_reference()}
        />
        {#if viewer.error !== 'unsupported' && viewer.error !== 'not_in_plan'}
          <Button icon="play" testid="camera-retry" onclick={() => void viewer.open(mowerId)}
            >{m.camera_retry()}</Button
          >
        {/if}
      </div>
    {/if}
    {#each [0, 1] as slot (slot)}
      {@const tile = tileAt(slot)}
      {@const live = !!tile && tile.status === 'live' && !failed}
      {@const shownTile = !!tile && (slot === 0 || viewer.both)}
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
          data-camera-side={viewer.both ? (tile?.side ?? '') : ''}
        ></canvas>
        <video
          bind:this={videos[slot]}
          class="picture"
          class:hidden={fake || !viewer.session || !live || !shownTile}
          autoplay
          muted
          playsinline
          data-testid={slot === 0 ? 'camera-video' : 'camera-video-right'}
          data-camera-video={!fake && live && shownTile ? '' : undefined}
          data-camera-live={live ? 'true' : 'false'}
          data-camera-name={tile?.camera?.name ?? ''}
          data-camera-side={viewer.both ? (tile?.side ?? '') : ''}
        ></video>
        {#if tile && shownTile && !failed}
          {#if tile.status === 'missing'}
            <p class="notice" data-testid="camera-no-picture">{m.camera_no_picture()}</p>
          {:else if tile.status === 'no_data'}
            <p class="notice" data-testid="camera-no-data">{m.camera_no_data()}</p>
          {:else if tile.status === 'connecting' && viewer.both}
            <p class="notice quiet-notice">{m.camera_tile_waiting()}</p>
          {/if}
          {#if viewer.both || tileStats(tile)}
            <figcaption class="caption mono" data-testid="camera-tile-caption">
              {#if tile.side}<span class="side" aria-hidden="true"
                  >{tile.side === 'left'
                    ? m.camera_side_left_short()
                    : m.camera_side_right_short()}</span
                >{/if}
              <span>{tileName(tile)}</span>
              {#if tileStats(tile)}<span class="stats">{tileStats(tile)}</span>{/if}
            </figcaption>
          {/if}
        {/if}
      </figure>
    {/each}
    {#if samePicture && !failed}
      <p class="same" role="status" data-testid="camera-same-picture">{m.camera_same_picture()}</p>
    {/if}
    {#if status}
      <span class="status mono" data-testid="camera-status">
        {#if viewer.phase === 'live'}<span class="dot" aria-hidden="true"></span>{/if}
        {status}
      </span>
    {/if}
  </div>

  <div class="controls">
    {#if cameraOptions.length > 1}
      <SegmentedControl
        label={m.camera_picker()}
        options={cameraOptions}
        value={pickerValue}
        onchange={(value) =>
          void (value === BOTH ? viewer.showBoth() : viewer.select(Number(value)))}
        testid="camera-picker"
      />
    {/if}
    {#if viewer.session && !fake}
      <Button
        variant="ghost"
        icon={viewer.muted ? 'mute' : 'sound'}
        label={viewer.muted ? m.camera_unmute() : m.camera_mute()}
        testid="camera-mute"
        onclick={() => viewer.toggleMute()}
      />
    {/if}
  </div>

  <p class="hint" data-testid="camera-data-note">{m.camera_data_note()}</p>
  {#if viewer.both && !fake}
    <p class="hint" data-testid="camera-both-note">{m.camera_both_note()}</p>
  {/if}
  {#if quota && quota.total_minutes > 0}
    <p class="hint mono" data-testid="camera-quota">
      {m.camera_quota({
        used: formatNumber(quota.used_minutes),
        total: formatNumber(quota.total_minutes),
      })}
    </p>
  {/if}
  {#if viewer.activeUid !== null}
    <p class="hint mono" data-testid="camera-active-uid">
      {m.camera_active_uid({ uid: String(viewer.activeUid) })}
    </p>
  {/if}
  {#if viewer.session}
    <p class="hint mono" data-testid="camera-expiry">
      {m.camera_expires({ time: formatTime(viewer.session.expires_at) })}
      {#if viewer.renewals}· {m.camera_renewed({ count: viewer.renewals })}{/if}
    </p>
  {/if}
  {#if viewer.session && !fake}
    <details
      class="fold"
      data-testid="camera-diagnose"
      ontoggle={(event) =>
        viewer.watchDiagnostics((event.currentTarget as HTMLDetailsElement).open)}
    >
      <summary>{m.camera_diag_summary()}</summary>
      {#if diag}
        <div class="readout mono" data-testid="camera-diagnose-readout">
          <span>{m.camera_diag_state({ state: diag.state })}</span>
          <span>
            {m.camera_diag_video({
              bitrate: shown(diag.bitrateKbps),
              fps: shown(diag.fps),
              size: diag.width && diag.height ? `${diag.width}x${diag.height}` : '-',
            })}
          </span>
          <span>
            {m.camera_diag_quality({
              freeze: shown(diag.freezePercent),
              seconds: shown(diag.freezeSeconds),
            })}
          </span>
          <span>
            {m.camera_diag_network({
              loss: shown(diag.lossPercent),
              total: shown(diag.lossTotalPercent),
              delay: shown(diag.delayMs),
              source: diag.delaySource ? delayLabel(diag.delaySource) : '-',
              rtt: shown(diag.rttMs),
            })}
          </span>
          <span>
            {m.camera_diag_events({
              states: diag.stateChanges,
              published: diag.published,
              unpublished: diag.unpublished,
              rejoins: viewer.rejoins,
            })}
          </span>
          {#if diag.recent.length}
            <span>{m.camera_diag_last()}:</span>
            {#each diag.recent as event (event.at + event.text)}
              <span class="event">{stamp(event.at)} {event.kind} {event.text}</span>
            {/each}
          {/if}
        </div>
      {:else}
        <p class="hint mono">{m.camera_diag_none()}</p>
      {/if}
    </details>
    <details class="fold" data-testid="camera-advanced">
      <summary>{m.camera_advanced()}</summary>
      <div class="options">
        <p class="hint">{m.camera_opt_profile()}: {m.camera_opt_profile_hint()}</p>
        <SegmentedControl
          label={m.camera_opt_profile()}
          options={[
            { value: 'rtc', label: m.camera_opt_profile_rtc() },
            { value: 'live', label: m.camera_opt_profile_live() },
          ]}
          value={viewer.options.profile}
          onchange={(value) =>
            void viewer.setOptions({ profile: value === 'live' ? 'live' : 'rtc' })}
          testid="camera-opt-profile"
        />
        {#if viewer.options.profile === 'live'}
          <p class="hint">{m.camera_opt_latency()}: {m.camera_opt_latency_hint()}</p>
          <SegmentedControl
            label={m.camera_opt_latency()}
            options={[
              { value: '1', label: m.camera_opt_latency_low() },
              { value: '2', label: m.camera_opt_latency_ultra() },
            ]}
            value={String(viewer.options.latency)}
            onchange={(value) => void viewer.setOptions({ latency: value === '2' ? 2 : 1 })}
            testid="camera-opt-latency"
          />
        {/if}
        <Switch
          id="camera-opt-fallback"
          label={m.camera_opt_fallback()}
          hint={m.camera_opt_fallback_hint()}
          states={{ on: m.switch_on(), off: m.switch_off() }}
          checked={viewer.options.fallback}
          onchange={(checked) => void viewer.setOptions({ fallback: checked })}
          testid="camera-opt-fallback"
        />
        <Switch
          id="camera-opt-saver"
          label={m.camera_opt_saver()}
          hint={m.camera_opt_saver_hint()}
          states={{ on: m.switch_on(), off: m.switch_off() }}
          checked={viewer.options.dataSaver}
          onchange={(checked) => void viewer.setOptions({ dataSaver: checked })}
          testid="camera-opt-saver"
        />
        <Switch
          id="camera-opt-rejoin"
          label={m.camera_opt_rejoin()}
          hint={m.camera_opt_rejoin_hint()}
          states={{ on: m.switch_on(), off: m.switch_off() }}
          checked={viewer.options.autoRejoin}
          onchange={(checked) => void viewer.setOptions({ autoRejoin: checked })}
          testid="camera-opt-rejoin"
        />
      </div>
    </details>
  {/if}
</div>

<style>
  .camera {
    display: grid;
    gap: var(--md-space-2);
    container-type: inline-size;
  }

  .head,
  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2);
  }

  .head {
    justify-content: flex-end;
  }

  .controls:empty {
    display: none;
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

  .stage {
    position: relative;
    display: grid;
    gap: 2px;
    overflow: hidden;
    border-radius: var(--md-radius-2);
    background: var(--md-color-ink);
  }

  /* Side by side when the panel is wide enough, stacked (left above right) otherwise. */
  @container (min-width: 560px) {
    .stage.both {
      grid-template-columns: 1fr 1fr;
    }
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

  .fullscreen .stage:not(.both) .tile {
    max-height: calc(100dvh - 260px);
  }

  .picture {
    grid-area: 1 / 1;
    width: 100%;
    height: 100%;
    object-fit: contain;
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

  .side {
    display: inline-grid;
    place-items: center;
    min-width: 1.5em;
    padding: 0 var(--md-space-1);
    border: var(--md-hairline) solid currentcolor;
    border-radius: var(--md-radius-1);
    font-weight: var(--md-weight-semibold);
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

  .problem {
    grid-area: 1 / 1 / -1 / -1;
    place-self: center;
    z-index: 3;
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
    z-index: 2;
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

  .dot {
    width: 8px;
    height: 8px;
    border-radius: var(--md-radius-round);
    background: var(--md-state-error);
  }

  .hint {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
  }

  .mono {
    font-family: var(--md-font-mono);
  }

  .fold summary {
    min-height: var(--md-target);
    display: flex;
    align-items: center;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-13);
    cursor: pointer;
  }

  .readout {
    display: grid;
    gap: var(--md-space-1);
    font-size: var(--md-font-size-12);
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }

  .event {
    color: var(--md-color-ink-muted);
  }

  .options {
    display: grid;
    gap: var(--md-space-2);
    padding-bottom: var(--md-space-2);
  }
</style>
