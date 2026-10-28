<!--
  Manual drive (ADR 0026) with the live camera (ADR 0044): a session over the Realtime socket,
  the joystick, the speed limit, the two-step blade switch, the session readouts and either the
  mower's camera (with the map as a corner thumbnail) or a live map that follows the mower. The
  view is decided from the capabilities only (`lib/drive/layout.ts`): a mower with a `camera`
  capability starts on the camera, the "Camera | Map" switch is remembered per browser, and a
  mower without a camera, or a camera that failed, gets the map.

  Three layouts: phones upright (camera 16:9 on top, a one-line readout, the stick, speed limit
  and session Stop, the details folded), phones sideways (`(orientation: landscape) and
  (max-height: 520px)`: the picture full-bleed with a translucent stick for the right thumb, the
  emergency stop and the session Stop in a rail on the left, nothing scrolls) and desktops (the
  picture fills the centre, the console column on the right). Over the camera, with the view
  switch, sits the light chip (`LightChip`) when the mower has the `light` capability: the same
  rules, permission (`can_control`) and command (`set_light`) as the tile on the mower page; the
  controller keeps the drive session open while the light is switched.

  Safety stays with the Device-Gateway's dead-man timer: this page only stops sending. It ends
  the session when it is left, when the tab is hidden and when the socket drops (the client
  reports `connection_lost`), and the emergency stop (`StopBar`) is always in view. While the
  camera picture stands (`CameraStage` reports it frozen) the pilot holds the mower: zero frames
  only, the blades locked, the session open; when the picture runs again the stick drives only
  after it was centred or touched again.
-->
<script lang="ts">
  import {
    BatteryBar,
    Button,
    Icon,
    Joystick,
    MowerMap,
    Panel,
    ProblemNotice,
    Readout,
    SegmentedControl,
    StatusBadge,
    Switch,
    TopBar,
    type Activity,
    type MapSnapshot,
    type RoutePoint,
    type StickVector,
  } from '@mowdeck/ui';
  import { onDestroy, untrack } from 'svelte';
  import { MediaQuery } from 'svelte/reactivity';

  import { page } from '$app/state';

  import { fleetApi, telemetryApi, unwrap } from '#lib/api/client.js';
  import { asProblem, type ApiProblem } from '#lib/api/problem.js';
  import type { CameraViewer } from '#lib/camera/viewer.svelte.js';
  import { clock } from '#lib/clock.svelte.js';
  import CameraStage from '#lib/components/CameraStage.svelte';
  import LightChip from '#lib/components/LightChip.svelte';
  import LiveStatus from '#lib/components/LiveStatus.svelte';
  import SlowLoading from '#lib/components/SlowLoading.svelte';
  import StopBar from '#lib/components/StopBar.svelte';
  import { loadConfig, type MapTilesConfig } from '#lib/config.js';
  import { driveEndLabel, driveErrorLabel } from '#lib/drive/labels.js';
  import {
    driveView,
    hasCamera,
    loadDriveCameraLayout,
    loadThumb,
    loadView,
    saveDriveCameraLayout,
    saveThumb,
    saveView,
    type DriveView,
  } from '#lib/drive/layout.js';
  import { DrivePilot, SPEED_LIMITS, clampReverse, type SpeedLimit } from '#lib/drive/pilot.js';
  import type { components as Fleet } from '#lib/generated/fleet.js';
  import { activityLabel, formatNumber, problemMessage } from '#lib/i18n.js';
  import { commands, permissions, realtime, revoked, sendCommand } from '#lib/live.svelte.js';
  import { lightCapable as isLightCapable, setLight } from '#lib/mower/light.js';
  import { areaNamer, mapLabels } from '#lib/mower/map-labels.js';
  import { appendLive } from '#lib/mower/route.js';
  import { m } from '#lib/paraglide/messages.js';
  import type { DriveEvent } from '#lib/realtime/client.svelte.js';
  import { mowers as states } from '#lib/stores/mowers.svelte.js';
  import { toasts } from '#lib/stores/ui.svelte.js';

  type Mower = Fleet['schemas']['MowerOut'];
  type DriveCaps = {
    manual_drive?: boolean;
    manual_drive_blades?: boolean;
    drive_reverse?: boolean;
    camera?: string | null;
    light?: boolean;
  };

  // Units are not translated.
  const PER_SECOND = '/s';
  const MILLIS = 'ms';
  const HERTZ = 'Hz';

  const id = $derived(page.params.id ?? '');

  let mower = $state<Mower | null>(null);
  let problem = $state<ApiProblem | null>(null);
  let notFound = $state(false);
  let caps = $state<DriveCaps | null>(null);
  let canDrive = $state<boolean | null>(null);
  /** The viewer may command the mower (the light switch). */
  let canControl = $state(false);
  let mowerMap = $state.raw<MapSnapshot | null>(null);
  let mapFailed = $state(false);
  let follow = $state(true);
  let tiles = $state.raw<MapTilesConfig | null>(null);
  let route = $state.raw<RoutePoint[]>([]);
  const routeSince = new Date();

  let startError = $state<string | null>(null);
  let engaged = $state(false);
  let stick = $state<StickVector>({ linear: 0, angular: 0 });
  let speedLimit = $state<SpeedLimit>(25);
  let armed = $state(false);
  let bladesOn = $state(false);
  let rates = $state({ sent: 0, acked: 0 });
  let now = $state(Date.now());
  let barHeight = $state(120);

  // The view: camera or map (remembered per browser), the map thumbnail over the camera.
  let storedView = $state<DriveView | null>(loadView());
  let thumbShown = $state(loadThumb());
  /** The camera failed during this visit: the map until the driver asks for the camera again. */
  let cameraFailed = $state(false);
  let viewer = $state<CameraViewer | null>(null);
  /** The camera picture stands (or went away after it was live). */
  let stalled = $state(false);
  /** The pilot holds the mower or keeps the stick latched after a hold. */
  let pilotHeld = $state(false);
  const driveCameraLayout = loadDriveCameraLayout();

  // A phone held sideways: the cockpit (picture full-bleed, the stop rail on the left).
  const sideways = new MediaQuery('(orientation: landscape) and (max-height: 520px)');
  const wide = new MediaQuery('(min-width: 1024px)');

  const pilot = new DrivePilot(realtime);

  // --- data -------------------------------------------------------------------------------------

  async function load(mowerId: string): Promise<void> {
    mower = null;
    problem = null;
    notFound = false;
    caps = null;
    canDrive = null;
    canControl = false;
    mowerMap = null;
    try {
      mower = await unwrap(
        fleetApi.GET('/api/v1/fleet/mowers/{mower_id}', {
          params: { path: { mower_id: mowerId } },
        }),
      );
    } catch (error) {
      const p = asProblem(error);
      if (p.status === 404) notFound = true;
      else problem = p;
      return;
    }
    const path = { params: { path: { mower_id: mowerId } } };
    void unwrap(telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/state', path))
      .then((snap) => states.upsert(mowerId, snap.state, 'rest'))
      .catch(() => undefined);
    void unwrap(telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/capabilities', path))
      .then((snap) => (caps = snap.capabilities as DriveCaps))
      .catch(() => (caps = {}));
    void unwrap(telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/map', path))
      .then((snap) => (mowerMap = snap.map))
      .catch(() => undefined);
    void permissions
      .check(mowerId, ['can_drive', 'can_control'])
      .then((result) => {
        canDrive = result.can_drive;
        canControl = result.can_control;
      })
      .catch(() => (canDrive = false));
  }

  $effect(() => {
    void load(id);
  });
  $effect(() => realtime.watch([id]));
  $effect(() => {
    void loadConfig().then((config) => (tiles = config.mapTiles));
  });
  $effect(() => clock.hold(5_000));

  const snap = $derived(states.get(id));
  const ms = $derived(snap?.state ?? null);
  const activity = $derived((ms?.activity ?? null) as Activity | null);
  const stale = $derived(states.isOffline(id, clock.now));

  // The route of this visit: the live positions since the page opened.
  $effect(() => {
    const latest = ms;
    const current = untrack(() => route);
    const next = appendLive(current, latest, routeSince);
    if (next !== current) route = next;
  });

  // --- session ----------------------------------------------------------------------------------

  const session = $derived(realtime.drive?.mowerId === id ? realtime.drive : null);
  const live = $derived(session !== null);
  const starting = $derived(realtime.driveStarting && !live);
  const busyJob = $derived(activity === 'mowing' || activity === 'returning');
  const offline = $derived(activity === 'offline' || (snap !== undefined && stale));
  const bladesCapable = $derived(caps?.manual_drive_blades === true);
  // The drive link can drive backwards (ADR 0043); otherwise the pilot cuts every backwards
  // deflection and the pad's lower half is marked off.
  const reverseCapable = $derived(caps?.drive_reverse === true);
  // A mower with a Bluetooth bridge drives over it. While the bridge is down a session goes
  // over the Mammotion cloud when the mower offers that (`manual_drive` stays true, without
  // reverse, ADR 0049); otherwise it would be refused (ADR 0038).
  const bridgeDown = $derived(
    ms?.links?.ble_bridge?.connected === false && caps?.manual_drive !== true,
  );
  const blocker = $derived.by((): string | null => {
    if (canDrive === false) return m.drive_no_permission();
    if (caps && !caps.manual_drive && !bridgeDown) return m.drive_unsupported();
    if (realtime.phase !== 'ready') return m.drive_socket_down();
    if (offline) return m.drive_offline();
    if (bridgeDown) return m.drive_bridge_disconnected();
    if (busyJob) return m.drive_busy_job();
    return null;
  });
  const canStart = $derived(
    !live && !starting && blocker === null && caps?.manual_drive === true && canDrive === true,
  );

  const cameraCapable = $derived(hasCamera(caps));
  const view = $derived(driveView(caps, storedView, cameraFailed));
  const cameraView = $derived(caps !== null && view === 'camera');
  const cockpit = $derived(sideways.current);
  /** Phones upright with the camera: the one-line readout and the folded details. */
  const compactConsole = $derived(cameraView && !cockpit && !wide.current);
  const holding = $derived(cameraView && stalled);
  // The camera light: only over the camera, only with the capability (never decided by vendor).
  const lightShown = $derived(cameraView && isLightCapable(caps));
  const lightPending = $derived(commands.pending(id, 'set_light'));

  function chooseView(next: DriveView): void {
    if (next === 'camera') cameraFailed = false;
    storedView = next;
    saveView(next);
  }

  function toggleThumb(): void {
    thumbShown = !thumbShown;
    saveThumb(thumbShown);
  }

  function toggleBoth(): void {
    const pair = viewer?.pair;
    if (!viewer || !pair) return;
    if (viewer.both) void viewer.select(pair.left.uid);
    else void viewer.showBoth();
  }

  // A camera error falls back to the map (with a note) for the rest of the visit.
  $effect(() => {
    if (viewer?.phase === 'error') cameraFailed = true;
  });

  // The picture stands: hold the mower (zero frames, session open) and lock the blades.
  $effect(() => {
    pilot.hold(holding);
    pilotHeld = pilot.holding;
    if (holding) untrack(disarm);
  });

  // The stick moves between the console and the picture when the phone turns: a stick that
  // was held there is let go (one zero frame), never carried over.
  $effect(() => {
    void cockpit;
    untrack(() => {
      if (!engaged) return;
      engaged = false;
      if (realtime.drive) pilot.release();
      pilotHeld = pilot.holding;
    });
  });

  function start(): void {
    startError = null;
    pilot.reset();
    armed = false;
    bladesOn = false;
    if (!realtime.startDrive(id, bladesCapable)) startError = m.drive_socket_down();
  }

  /** Halt and end: a zero frame first (the stick may be held), then `drive_stop`. */
  function end(): void {
    disarm();
    if (realtime.drive) {
      pilot.release();
      realtime.stopDrive();
    } else if (realtime.driveStarting) {
      realtime.stopDrive();
    }
    engaged = false;
  }

  function disarm(): void {
    armed = false;
    bladesOn = false;
    pilot.bladeOn = false;
  }

  function onDrive(event: DriveEvent): void {
    switch (event.type) {
      case 'started':
        if (event.session.mowerId !== id) return;
        pilot.reset();
        startError = null;
        break;
      case 'ack':
        pilot.acked();
        break;
      case 'ended':
        disarm();
        engaged = false;
        toasts.push(
          event.reason === 'client_stop' ? 'ok' : 'error',
          driveEndLabel(event.reason),
          event.detail ?? undefined,
        );
        break;
      case 'error':
        disarm();
        startError = driveErrorLabel(event.code);
        break;
    }
  }

  $effect(() => realtime.onDrive(onDrive));

  // Leaving the page or hiding the tab ends the session; frames stop either way.
  $effect(() => {
    const hidden = () => {
      if (document.visibilityState === 'hidden') end();
    };
    document.addEventListener('visibilitychange', hidden);
    window.addEventListener('pagehide', end);
    return () => {
      document.removeEventListener('visibilitychange', hidden);
      window.removeEventListener('pagehide', end);
    };
  });
  onDestroy(end);

  // Keep the screen on while driving (where the browser offers it).
  $effect(() => {
    if (!live || typeof navigator === 'undefined' || !('wakeLock' in navigator)) return;
    let lock: WakeLockSentinel | null = null;
    let released = false;
    navigator.wakeLock
      .request('screen')
      .then((sentinel) => {
        if (released) void sentinel.release();
        else lock = sentinel;
      })
      .catch(() => undefined);
    return () => {
      released = true;
      void lock?.release().catch(() => undefined);
    };
  });

  // The session strip: rates and time left, four times a second while driving.
  $effect(() => {
    if (!live) {
      rates = { sent: 0, acked: 0 };
      return;
    }
    const timer = setInterval(() => {
      rates = pilot.rates();
      pilotHeld = pilot.holding;
      now = Date.now();
    }, 250);
    return () => clearInterval(timer);
  });

  $effect(() => {
    pilot.speedLimit = speedLimit;
  });
  $effect(() => {
    pilot.reverseAllowed = reverseCapable;
  });
  $effect(() => {
    pilot.bladeOn = live && armed && bladesOn && session?.bladeOnAllowed === true;
  });

  function onsample(vector: StickVector): void {
    if (live) pilot.sample(vector);
    pilotHeld = pilot.holding;
  }

  function onengage(value: boolean): void {
    engaged = value;
    if (!value && live) pilot.release();
    pilotHeld = pilot.holding;
  }

  const interval = $derived(session ? Math.round(1000 / Math.max(1, session.maxRateHz)) : 100);
  const timeLeft = $derived.by(() => {
    if (!session) return null;
    const left = Math.max(0, session.maxSeconds - Math.floor((now - session.startedAt) / 1000));
    return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
  });
  const output = $derived.by(() => {
    const sent = clampReverse(stick, reverseCapable);
    const moving = engaged && !pilotHeld;
    return {
      linear: moving ? Math.round(sent.linear * speedLimit) / 100 : 0,
      angular: moving ? sent.angular : 0,
    };
  });
  const stateWord = $derived(
    live
      ? m.drive_state_live()
      : starting
        ? m.drive_state_starting()
        : blocker
          ? m.drive_state_off()
          : m.drive_state_idle(),
  );
  const stateKey = $derived(live ? 'live' : starting ? 'starting' : blocker ? 'off' : 'idle');
  const speedOptions = $derived(SPEED_LIMITS.map((v) => ({ value: String(v), label: `${v} %` })));
  const labels = $derived(mapLabels());
  const delay = $derived.by(() => {
    const value = viewer?.tracks[0]?.delayMs;
    return value == null ? '—' : formatNumber(value);
  });
  const batteryText = $derived(ms ? `${formatNumber(ms.battery_percent)} %` : '—');
  const speedText = $derived(ms?.speed_mps != null ? formatNumber(ms.speed_mps, 2) : '—');
  const heldText = $derived(stalled && cameraView ? m.drive_held() : m.drive_recentre());
  const areaName = $derived(areaNamer(mowerMap?.areas, mower?.area_names));
</script>

<svelte:head><title>{m.drive_title()} · {mower?.name ?? m.app_name()}</title></svelte:head>

{#if notFound || revoked.has(id)}
  <TopBar title={m.detail_not_found_title()} backHref="/mowers" backLabel={m.back()} />
  <div class="pad">
    <ProblemNotice tone="info" title={m.detail_not_found_title()} />
  </div>
{:else if problem}
  <TopBar title={m.drive_title()} backHref="/mowers/{id}" backLabel={m.back()} />
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if mower}
  {#snippet lamp(withTestid: boolean)}
    <span class="lamp-cell" data-state={stateKey}>
      <span class="lamp" aria-hidden="true"></span>
      {#if withTestid}
        <span class="state" data-testid="drive-state">{stateWord}</span>
      {:else}
        <span class="state">{stateWord}</span>
      {/if}
    </span>
  {/snippet}

  {#snippet cells()}
    <dl class="cells">
      <div>
        <dt>{m.drive_session()}</dt>
        <dd class="mono" data-testid="drive-session-id">
          {session ? session.sessionId.slice(0, 8) : '—'}
        </dd>
      </div>
      <div>
        <dt>{m.drive_rate_sent()}</dt>
        <dd class="mono" data-testid="drive-rate-sent">
          {rates.sent}<small>{PER_SECOND}</small>
        </dd>
      </div>
      <div>
        <dt>{m.drive_rate_acked()}</dt>
        <dd class="mono" data-testid="drive-rate-acked">
          {rates.acked}<small>{PER_SECOND}</small>
        </dd>
      </div>
      <div>
        <dt>{m.drive_ttl()}</dt>
        <dd class="mono">
          {session ? formatNumber(session.ttlMs) : '—'}<small>{MILLIS}</small>
        </dd>
      </div>
      <div>
        <dt>{m.drive_max_rate()}</dt>
        <dd class="mono">
          {session ? formatNumber(session.maxRateHz) : '—'}<small>{HERTZ}</small>
        </dd>
      </div>
      <div>
        <dt>{m.drive_time_left()}</dt>
        <dd class="mono">{timeLeft ?? '—'}</dd>
      </div>
    </dl>
  {/snippet}

  {#snippet sessionButton()}
    {#if live}
      <Button variant="danger" full icon="stop" testid="drive-stop" onclick={end}
        >{m.drive_stop()}</Button
      >
    {:else}
      <Button
        variant="primary"
        full
        icon="joystick"
        pending={starting}
        disabled={!canStart}
        testid="drive-start"
        onclick={start}>{m.drive_start()}</Button
      >
    {/if}
  {/snippet}

  {#snippet speedControl()}
    <SegmentedControl
      label={m.drive_speed_limit()}
      options={speedOptions}
      value={String(speedLimit)}
      onchange={(value) => (speedLimit = Number(value) as SpeedLimit)}
      testid="drive-speed-limit"
    />
  {/snippet}

  {#snippet pad(overlay: boolean)}
    <div
      class="stick-pad"
      class:overlay
      data-reverse={reverseCapable ? 'true' : 'false'}
      data-held={pilotHeld ? 'true' : 'false'}
      data-testid="drive-pad"
    >
      <Joystick
        label={m.drive_pad()}
        hint={overlay || compactConsole ? undefined : m.drive_pad_hint()}
        disabled={!live}
        {overlay}
        {interval}
        bind:value={stick}
        {onsample}
        {onengage}
        testid="joystick"
      />
      {#if !reverseCapable && !overlay}
        <p class="no-reverse" data-testid="drive-no-reverse">
          <Icon name="alert" size={14} />{m.drive_no_reverse()}
        </p>
      {/if}
    </div>
  {/snippet}

  {#snippet notices()}
    {#if startError}
      <div class="notice" data-testid="drive-error">
        <ProblemNotice title={startError} />
      </div>
    {:else if blocker && !live}
      <div class="notice" data-testid="drive-blocked">
        <ProblemNotice tone="info" title={blocker} />
      </div>
    {/if}
  {/snippet}

  <div
    class="drive"
    class:cockpit
    class:camera-view={cameraView}
    class:compact-console={compactConsole}
    style:--bar-height="{barHeight}px"
    data-testid="drive-page"
    data-view={caps === null ? 'pending' : view}
    data-layout={cockpit ? 'sideways' : wide.current ? 'desktop' : 'upright'}
  >
    {#if !cockpit}
      <div class="top">
        <TopBar
          title={m.drive_title()}
          meta={mower.name}
          backHref="/mowers/{id}"
          backLabel={m.back()}
        >
          {#snippet status()}
            <LiveStatus phase={realtime.phase} tooMany={realtime.tooMany} />
          {/snippet}
        </TopBar>
      </div>
    {/if}

    <section class="view" aria-label={cameraView ? m.camera_title() : m.map_region()}>
      <div class="screen">
        {#if caps === null}
          <div class="map-empty"><span><SlowLoading /></span></div>
        {:else if cameraView}
          <CameraStage
            mowerId={id}
            mowerName={mower.name}
            position={ms?.position ?? null}
            compact
            frontLeft
            layout={driveCameraLayout}
            saveLayout={saveDriveCameraLayout}
            bind:viewer
            bind:stalled
          />
          {#if mowerMap && !mapFailed}
            {#if thumbShown}
              <div class="thumb" data-testid="drive-map-thumb">
                <MowerMap
                  map={mowerMap}
                  state={ms}
                  {route}
                  {stale}
                  {tiles}
                  {labels}
                  {areaName}
                  follow
                  onfail={() => (mapFailed = true)}
                  testid="drive-map"
                />
                <button
                  type="button"
                  class="thumb-hit"
                  aria-label={m.drive_map_thumb_hide()}
                  data-testid="drive-map-thumb-hide"
                  onclick={toggleThumb}
                ></button>
              </div>
            {:else}
              <button
                type="button"
                class="thumb-show"
                data-testid="drive-map-thumb-show"
                onclick={toggleThumb}
              >
                <Icon name="layers" size={16} />{m.drive_map_thumb_show()}
              </button>
            {/if}
          {/if}
        {:else if mowerMap && !mapFailed}
          <MowerMap
            map={mowerMap}
            state={ms}
            {route}
            {stale}
            {tiles}
            {labels}
            {areaName}
            bind:follow
            onfail={() => (mapFailed = true)}
            testid="drive-map"
          />
        {:else}
          <div class="map-empty"><span>{m.drive_map_empty()}</span></div>
        {/if}

        {#if cameraCapable || cameraFailed}
          <div class="tools">
            {#if cameraFailed}
              <p class="camera-note" role="status" data-testid="drive-camera-unavailable">
                <Icon name="alert" size={14} />{m.drive_camera_unavailable()}
              </p>
            {/if}
            {#if cameraCapable}
              <SegmentedControl
                label={m.drive_view()}
                options={[
                  { value: 'camera', label: m.drive_view_camera() },
                  { value: 'map', label: m.drive_view_map() },
                ]}
                value={view}
                onchange={(value) => chooseView(value === 'map' ? 'map' : 'camera')}
                testid="drive-view"
              />
            {/if}
            {#if cameraView && viewer?.pair}
              <button
                type="button"
                class="tool"
                aria-pressed={viewer.both}
                data-testid="drive-camera-both"
                aria-label={m.drive_camera_both()}
                onclick={toggleBoth}
                ><Icon name="camera" size={16} /><span class="tool-text"
                  >{m.drive_camera_both()}</span
                ></button
              >
            {/if}
            {#if lightShown}
              <div class="light-slot">
                <LightChip
                  on={ms?.light_on ?? null}
                  capable
                  online={activity !== null && !offline}
                  allowed={canControl}
                  pending={lightPending}
                  compact={compactConsole}
                  testid="drive-light"
                  onchange={(on) => void sendCommand(id, setLight(on))}
                />
              </div>
            {/if}
          </div>
        {/if}

        {#if cockpit}
          <div class="cockpit-foot">
            {@render speedControl()}
          </div>
          <div class="cockpit-stick">
            {@render pad(true)}
          </div>
        {/if}
      </div>

      {#if cameraView || cockpit}
        <div class="hud" role="group" aria-label={m.drive_hud()} data-testid="drive-hud">
          {#if cockpit}
            <Button variant="ghost" icon="back" href="/mowers/{id}" label={m.back()} />
          {/if}
          {#if cockpit || compactConsole}
            {@render lamp(true)}
          {:else}
            {@render lamp(false)}
          {/if}
          <span class="hud-cell" data-testid="drive-hud-battery">
            <small>{m.detail_battery()}</small><b class="mono">{batteryText}</b>
          </span>
          <span class="hud-cell" data-testid="drive-hud-speed">
            <small>{m.drive_speed()}</small><b class="mono">{speedText}<i>m/s</i></b>
          </span>
          {#if cameraView}
            <span class="hud-cell" data-testid="drive-hud-delay">
              <small>{m.drive_delay()}</small><b class="mono">{delay}<i>{MILLIS}</i></b>
            </span>
          {/if}
          {#if armed && bladesOn}
            <button type="button" class="blades-on" data-testid="drive-hud-blades" onclick={disarm}
              ><Icon name="blade" size={14} />{m.drive_blades_disarm()}</button
            >
          {/if}
        </div>
      {/if}
      {#if pilotHeld && live}
        <p class="held" role="alert" data-testid="drive-held">
          <Icon name="pause" size={16} />{heldText}
        </p>
      {/if}
      {#if cockpit}
        <div class="cockpit-notices">{@render notices()}</div>
      {/if}
    </section>

    {#if !cockpit}
      <section class="console">
        {#if compactConsole}
          <details class="details" data-testid="drive-details">
            <summary>{m.drive_details()}</summary>
            {@render cells()}
          </details>
        {:else}
          <div class="strip" data-testid="drive-strip" data-state={stateKey}>
            {@render lamp(true)}
            {@render cells()}
          </div>
        {/if}

        {@render notices()}

        <div class="stick">
          {@render pad(false)}
          {@render speedControl()}
          <div class="session-actions">
            {@render sessionButton()}
            {#if live && !compactConsole}<p class="hint">{m.drive_stop_hint()}</p>{/if}
          </div>
        </div>

        <div class="status">
          {#if activity}
            <StatusBadge {activity} label={activityLabel(activity)} {stale} />
          {:else}
            <StatusBadge activity="unknown" label={m.activity_no_telemetry()} />
          {/if}
          <div class="readouts">
            <Readout
              label={m.drive_speed()}
              value={ms?.speed_mps != null ? formatNumber(ms.speed_mps, 2) : null}
              unit="m/s"
              emptyText={m.no_data()}
              testid="drive-speed"
            />
            <Readout
              label={m.detail_battery()}
              value={ms ? formatNumber(ms.battery_percent) : null}
              unit="%"
              emptyText={m.no_data()}
            >
              <BatteryBar percent={ms?.battery_percent ?? null} charging={ms?.charging} />
            </Readout>
            <Readout
              label={m.drive_linear()}
              value={formatNumber(output.linear, 2)}
              emptyText={m.no_data()}
              size="inline"
              testid="drive-out-linear"
            />
            <Readout
              label={m.drive_angular()}
              value={formatNumber(output.angular, 2)}
              emptyText={m.no_data()}
              size="inline"
              testid="drive-out-angular"
            />
          </div>
        </div>

        <div class="controls">
          <Panel label={m.drive_blades()} testid="drive-blades">
            {#if bladesCapable}
              <div class="arm" data-armed={armed}>
                <span class="arm-state">
                  <Icon name={armed ? 'alert' : 'blade'} size={18} />
                  {armed ? m.drive_blades_armed() : m.drive_blades_locked()}
                </span>
                <Button
                  variant={armed ? 'ghost' : 'secondary'}
                  icon={armed ? 'close' : 'blade'}
                  disabled={!live || session?.bladeOnAllowed !== true || pilotHeld}
                  testid="drive-blades-arm"
                  onclick={() => (armed ? disarm() : (armed = true))}
                  >{armed ? m.drive_blades_disarm() : m.drive_blades_arm()}</Button
                >
              </div>
              <Switch
                id="drive-blades"
                testid="drive-blades-switch"
                label={m.drive_blades_switch()}
                hint={live ? m.drive_blades_switch_hint() : m.drive_blades_session()}
                states={{ on: m.switch_on(), off: m.switch_off() }}
                disabled={!live || !armed}
                bind:checked={bladesOn}
              />
            {:else}
              <p class="hint">{m.drive_blades_unsupported()}</p>
            {/if}
          </Panel>

          <Panel label={m.drive_safety_title()}>
            <p class="hint">
              {m.drive_safety({ ttl: formatNumber(session?.ttlMs ?? 1000) })}
            </p>
          </Panel>
        </div>
      </section>
    {/if}

    <StopBar mowerId={id} onbeforestop={end} rail={cockpit} bind:height={barHeight}>
      {@render sessionButton()}
    </StopBar>
  </div>
{:else}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{/if}

<style>
  .pad {
    padding: var(--md-space-4) var(--md-gutter);
  }

  .quiet {
    color: var(--md-color-ink-muted);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  /* Phones upright: header, the view (camera 16:9 or a short map), the console; the bar is fixed. */
  .drive {
    display: flex;
    flex-direction: column;
    padding-bottom: var(--bar-height);
  }

  /* Phones: the wrapper steps aside, so the top bar stays pinned while the page scrolls. */
  .top {
    display: contents;
  }

  .view {
    position: relative;
    display: flex;
    flex-direction: column;
  }

  .screen {
    position: relative;
    height: min(calc(var(--md-viewport-height) * 0.3), 260px);
    min-height: 180px;
    overflow: hidden;
    border-block: var(--md-hairline) solid var(--md-color-line);
  }

  .camera-view .screen {
    height: auto;
    min-height: 0;
    aspect-ratio: 16 / 9;
    border-block: 0;
    background: var(--md-hud-glass-strong);
  }

  .map-empty {
    display: grid;
    place-items: center;
    height: 100%;
    padding: var(--md-space-4);
    background: var(--md-color-surface-sunken);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    text-align: center;
  }

  /* --- the tools over the picture: view switch, both cameras, camera note ------------------- */

  .tools {
    position: absolute;
    top: var(--md-space-2);
    right: var(--md-space-2);
    z-index: 5;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: var(--md-space-1);
    max-width: calc(100% - var(--md-space-4));
    pointer-events: none;
  }

  .tools > :global(*) {
    pointer-events: auto;
  }

  .tools :global(.md-segmented) {
    background: var(--md-color-surface);
  }

  .tool,
  .thumb-show {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    min-height: var(--md-target);
    padding: 0 var(--md-space-3);
    border: var(--md-hairline) solid var(--md-hud-line);
    border-radius: var(--md-radius-2);
    background: var(--md-hud-glass);
    color: var(--md-hud-ink);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    cursor: pointer;
    touch-action: manipulation;
  }

  /*
   * The light chip takes a row of its own at the right edge, under the view switch: clear of the
   * readout on the desktop and, sideways, right above the stick (in reach of the right thumb).
   * Phones upright keep one row of small chips.
   */
  .light-slot {
    display: flex;
    flex-basis: 100%;
    justify-content: flex-end;
  }

  /* The row itself lets taps through to the picture; only the chip takes them. */
  .tools > .light-slot {
    pointer-events: none;
  }

  .light-slot > :global(*) {
    pointer-events: auto;
  }

  .compact-console .light-slot {
    display: contents;
  }

  /*
   * Pressed ("both cameras" on, e.g. the remembered layout on load): a lit plate with dark ink.
   * The ink is a HUD token, not --md-color-ink: that one turns light in the dark theme and left
   * the label and icon white on white.
   */
  .tool[aria-pressed='true'] {
    border-color: var(--md-hud-ink);
    background: var(--md-hud-ink);
    color: var(--md-hud-ink-inverse);
  }

  .camera-note {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    margin: 0;
    padding: var(--md-space-1) var(--md-space-2);
    border: var(--md-hairline) solid var(--md-state-paused);
    border-radius: var(--md-radius-1);
    background: var(--md-color-surface);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
  }

  /* --- the map thumbnail in a corner of the picture ------------------------------------------ */

  .thumb {
    position: absolute;
    right: var(--md-space-2);
    bottom: var(--md-space-2);
    z-index: 4;
    width: clamp(112px, 32%, 240px);
    aspect-ratio: 4 / 3;
    overflow: hidden;
    border: var(--md-border-control) solid var(--md-hud-ink);
    border-radius: var(--md-radius-2);
    box-shadow: var(--md-shadow-float);
  }

  .thumb :global(.controls),
  .thumb :global(.legend),
  .thumb :global(.layers),
  .thumb :global(.scale),
  .thumb :global(.north),
  .thumb :global(.chip),
  .thumb :global(.attribution) {
    display: none;
  }

  .thumb-hit {
    position: absolute;
    inset: 0;
    z-index: 10;
    border: 0;
    background: transparent;
    cursor: pointer;
  }

  .thumb-hit:focus-visible {
    outline: 3px solid var(--md-color-focus);
    outline-offset: -3px;
  }

  .thumb-show {
    position: absolute;
    right: var(--md-space-2);
    bottom: var(--md-space-2);
    z-index: 4;
  }

  /* --- the one-line readout (under the picture upright, glass over it otherwise) ------------- */

  .hud {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-1) var(--md-space-3);
    min-height: 40px;
    padding: var(--md-space-1) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface-sunken);
  }

  .hud-cell {
    display: inline-flex;
    align-items: baseline;
    gap: var(--md-space-1);
    min-width: 0;
  }

  .hud-cell small {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .hud-cell b {
    color: var(--md-color-ink);
    font-size: var(--md-font-size-14);
    font-weight: var(--md-weight-semibold);
  }

  .hud-cell i {
    margin-inline-start: var(--md-space-0_5);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-style: normal;
  }

  .blades-on {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-1);
    min-height: 32px;
    padding: 0 var(--md-space-2);
    border: 0;
    border-radius: var(--md-radius-1);
    background: var(--md-state-paused);
    color: var(--md-color-ink-inverse);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    cursor: pointer;
  }

  .lamp-cell {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-2);
  }

  .lamp {
    width: 10px;
    height: 10px;
    flex: none;
    border: var(--md-border-control) solid var(--md-state-unknown);
    background: transparent;
  }

  .lamp-cell[data-state='live'] .lamp {
    border-color: var(--md-state-mowing);
    background: var(--md-state-mowing);
  }

  .lamp-cell[data-state='starting'] .lamp {
    border-color: var(--md-state-returning);
  }

  .lamp-cell[data-state='off'] .lamp {
    border-style: dashed;
    border-color: var(--md-state-offline);
  }

  .state {
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .lamp-cell[data-state='live'] .state {
    color: var(--md-state-mowing);
  }

  /* The pilot holds the mower (picture frozen) or waits for the stick to be centred. */
  .held {
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    margin: 0;
    padding: var(--md-space-2) var(--md-gutter);
    background: var(--md-state-error);
    color: var(--md-color-ink-inverse);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-13);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  /*
   * Phones upright with the camera: the tools shrink to small glass chips so the picture stays
   * readable, and the readout is one line.
   */
  .compact-console .tools :global(.md-segmented button) {
    min-height: 36px;
    padding: 0 var(--md-space-2);
    font-size: var(--md-font-size-12);
  }

  .compact-console .tool {
    min-height: 36px;
    padding: 0 var(--md-space-2);
  }

  .compact-console .tool-text {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  .compact-console .hud {
    flex-wrap: nowrap;
    gap: var(--md-space-2) var(--md-space-3);
    overflow: hidden;
    white-space: nowrap;
  }

  .compact-console .hud-cell small {
    font-size: var(--md-font-size-11);
  }

  .compact-console .hud-cell b {
    font-size: var(--md-font-size-13);
  }

  /* --- the console ---------------------------------------------------------------------------- */

  .console {
    display: flex;
    flex-direction: column;
    background: var(--md-color-surface);
  }

  .strip {
    display: grid;
    gap: var(--md-space-2);
    padding: var(--md-space-3) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface-sunken);
  }

  .details {
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface-sunken);
  }

  .details summary {
    display: flex;
    align-items: center;
    min-height: 40px;
    padding: 0 var(--md-gutter);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
    cursor: pointer;
  }

  .details .cells {
    padding: 0 var(--md-gutter) var(--md-space-3);
  }

  .cells {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: var(--md-space-2) var(--md-space-3);
    margin: 0;
  }

  .cells div {
    min-width: 0;
  }

  .cells dt {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .cells dd {
    margin: 0;
    overflow: hidden;
    color: var(--md-color-ink);
    font-size: var(--md-font-size-16);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .cells small {
    margin-inline-start: var(--md-space-0_5);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-11);
  }

  .status {
    display: grid;
    gap: var(--md-space-2);
    padding: var(--md-space-3) var(--md-gutter);
  }

  .readouts {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--md-space-0_5);
  }

  .notice {
    padding: 0 var(--md-gutter) var(--md-space-3);
  }

  .stick {
    display: grid;
    gap: var(--md-space-3);
    padding: var(--md-space-4) var(--md-gutter);
    border-block: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-bg);
  }

  .compact-console .stick {
    --md-stick-size: 240px;

    gap: var(--md-space-2);
    padding-block: var(--md-space-3);
    border-top: 0;
  }

  .stick > :global(.md-segmented),
  .cockpit-foot > :global(.md-segmented) {
    width: 100%;
  }

  /*
   * A drive link without reverse: the pad's lower (backwards) half is hatched off under the
   * thumb and its arrow mark hidden; the pilot cuts backwards deflections to zero either way.
   */
  .stick-pad {
    display: grid;
    gap: var(--md-space-2);
  }

  .stick-pad[data-reverse='false'] :global(.md-joystick)::before {
    position: absolute;
    inset: 0;
    border-radius: inherit;
    background: repeating-linear-gradient(-45deg, var(--md-color-line) 0 2px, transparent 2px 9px);
    clip-path: inset(50% 0 0 0);
    content: '';
    pointer-events: none;
  }

  .stick-pad.overlay[data-reverse='false'] :global(.md-joystick)::before {
    background: repeating-linear-gradient(-45deg, var(--md-hud-line) 0 2px, transparent 2px 9px);
  }

  .stick-pad[data-reverse='false'] :global(.md-joystick path:nth-of-type(2)) {
    visibility: hidden;
  }

  /* Held: the pad is ringed in the error colour, so the stick's silence is visible. */
  .stick-pad[data-held='true'] :global(.md-joystick) {
    outline: 3px solid var(--md-state-error);
    outline-offset: 3px;
  }

  .no-reverse {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--md-space-2);
    margin: 0;
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .controls {
    display: flex;
    flex-direction: column;
    gap: var(--md-space-2);
    padding-top: var(--md-space-3);
  }

  .arm {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-3);
    min-height: var(--md-target);
  }

  .arm-state {
    display: inline-flex;
    align-items: center;
    gap: var(--md-space-2);
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .arm[data-armed='true'] .arm-state {
    color: var(--md-state-paused);
  }

  .session-actions {
    display: grid;
    gap: var(--md-space-1);
  }

  .hint {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  /*
   * A phone held sideways: the cockpit. The page covers the screen (and the bottom navigation),
   * the picture or the map fills it, the stop rail takes the left edge, the stick floats over
   * the right; nothing scrolls.
   */
  .cockpit {
    position: fixed;
    inset: var(--md-safe-top) 0 0;
    z-index: 21;
    display: block;
    height: var(--md-viewport-height);
    padding: 0 0 0 calc(var(--md-stop-rail, 112px) + env(safe-area-inset-left, 0px));
    overflow: hidden;
    background: var(--md-hud-glass-strong);
  }

  .cockpit .view {
    height: 100%;
  }

  .cockpit .screen,
  .cockpit.camera-view .screen {
    height: 100%;
    min-height: 0;
    aspect-ratio: auto;
    border: 0;
  }

  .cockpit .hud {
    position: absolute;
    top: var(--md-space-2);
    left: var(--md-space-2);
    z-index: 6;
    max-width: calc(100% - var(--md-space-4));
    min-height: 0;
    padding: var(--md-space-1) var(--md-space-3) var(--md-space-1) var(--md-space-1);
    border: var(--md-hairline) solid var(--md-hud-line);
    border-radius: var(--md-radius-2);
    background: var(--md-hud-glass);
  }

  .cockpit .hud :global(.md-button) {
    min-height: 40px;
    color: var(--md-hud-ink);
  }

  .cockpit .hud-cell small,
  .cockpit .hud-cell i {
    color: var(--md-hud-ink-muted);
  }

  .cockpit .hud-cell b,
  .cockpit .lamp-cell .state {
    color: var(--md-hud-ink);
  }

  .cockpit .tools {
    top: calc(var(--md-space-2) + 52px);
  }

  .cockpit .thumb,
  .cockpit .thumb-show {
    right: auto;
    left: var(--md-space-2);
    bottom: calc(var(--md-space-2) + var(--md-target) + var(--md-space-2));
    width: clamp(112px, 24%, 180px);
  }

  .cockpit .thumb-show {
    width: auto;
  }

  .cockpit-foot {
    position: absolute;
    bottom: calc(var(--md-space-2) + env(safe-area-inset-bottom, 0px));
    left: var(--md-space-2);
    z-index: 5;
    width: min(252px, 42%);
  }

  .cockpit-foot :global(.md-segmented button) {
    white-space: nowrap;
  }

  .cockpit-foot :global(.md-segmented) {
    background: var(--md-color-surface);
  }

  .cockpit-stick {
    --md-stick-size: min(calc(var(--md-viewport-height) * 0.56), 210px);

    position: absolute;
    right: calc(var(--md-space-4) + env(safe-area-inset-right, 0px));
    bottom: calc(var(--md-space-3) + env(safe-area-inset-bottom, 0px));
    z-index: 6;
  }

  .cockpit .held {
    position: absolute;
    top: calc(var(--md-space-2) + 52px);
    left: var(--md-space-2);
    z-index: 7;
    border-radius: var(--md-radius-2);
  }

  .cockpit-notices {
    position: absolute;
    top: calc(var(--md-space-2) + 52px);
    left: var(--md-space-2);
    z-index: 6;
    width: min(360px, 55%);
  }

  .cockpit-notices .notice {
    padding: 0;
  }

  /* Desktop: the view fills the centre, the console is the instrument column. */
  @media (min-width: 1024px) {
    .drive:not(.cockpit) {
      display: grid;
      grid-template-columns: minmax(0, 1fr) var(--md-instrument-column);
      grid-template-rows: auto minmax(0, 1fr);
      grid-template-areas: 'top top' 'view console';
      height: var(--md-viewport-height);
      padding-bottom: 0;
      overflow: hidden;
    }

    .top {
      display: block;
      grid-area: top;
    }

    .view {
      grid-area: view;
      min-height: 0;
    }

    .drive:not(.cockpit) .screen {
      flex: 1;
      height: auto;
      min-height: 0;
      aspect-ratio: auto;
      border-block: 0;
    }

    /* The readout floats over the picture, glass, top left. */
    .camera-view:not(.cockpit) .hud {
      position: absolute;
      top: var(--md-space-3);
      left: var(--md-space-3);
      z-index: 6;
      min-height: 0;
      padding: var(--md-space-2) var(--md-space-3);
      border: var(--md-hairline) solid var(--md-hud-line);
      border-radius: var(--md-radius-2);
      background: var(--md-hud-glass);
    }

    .camera-view:not(.cockpit) .hud-cell small,
    .camera-view:not(.cockpit) .hud-cell i {
      color: var(--md-hud-ink-muted);
    }

    .camera-view:not(.cockpit) .hud-cell b,
    .camera-view:not(.cockpit) .hud .state {
      color: var(--md-hud-ink);
    }

    .camera-view:not(.cockpit) .held {
      position: absolute;
      bottom: var(--md-space-3);
      left: 50%;
      z-index: 7;
      border-radius: var(--md-radius-2);
      transform: translateX(-50%);
    }

    .camera-view:not(.cockpit) .thumb,
    .camera-view:not(.cockpit) .thumb-show {
      right: var(--md-space-3);
      bottom: var(--md-space-3);
    }

    .camera-view:not(.cockpit) .tools {
      top: var(--md-space-3);
      right: var(--md-space-3);
    }

    .console {
      grid-area: console;
      min-height: 0;
      overflow-y: auto;
      padding-bottom: calc(var(--bar-height) + var(--md-space-4));
      border-inline-start: var(--md-hairline) solid var(--md-color-line);
    }

    .stick {
      --md-stick-size: 232px;
    }

    .cells dd {
      font-size: var(--md-font-size-14);
    }
  }
</style>
