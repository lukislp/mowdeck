<!--
  Manual drive (ADR 0026): a session over the Realtime socket, the joystick, the speed limit, the
  two-step blade switch, the session strip and a small live map that follows the mower. Safety
  stays with the Device-Gateway's dead-man timer: this page only stops sending. It ends the
  session when it is left, when the tab is hidden and when the socket drops (the client reports
  `connection_lost`), and the emergency stop (`StopBar`) is always in view.
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

  import { page } from '$app/state';

  import { fleetApi, telemetryApi, unwrap } from '$lib/api/client';
  import { asProblem, type ApiProblem } from '$lib/api/problem';
  import { clock } from '$lib/clock.svelte';
  import LiveStatus from '$lib/components/LiveStatus.svelte';
  import SlowLoading from '$lib/components/SlowLoading.svelte';
  import StopBar from '$lib/components/StopBar.svelte';
  import { loadConfig, type MapTilesConfig } from '$lib/config';
  import { driveEndLabel, driveErrorLabel } from '$lib/drive/labels';
  import { DrivePilot, SPEED_LIMITS, type SpeedLimit } from '$lib/drive/pilot';
  import type { components as Fleet } from '$lib/generated/fleet';
  import { activityLabel, formatNumber, problemMessage } from '$lib/i18n';
  import { permissions, realtime, revoked } from '$lib/live.svelte';
  import { areaNamer, mapLabels } from '$lib/mower/map-labels';
  import { appendLive } from '$lib/mower/route';
  import { m } from '$lib/paraglide/messages';
  import type { DriveEvent } from '$lib/realtime/client.svelte';
  import { mowers as states } from '$lib/stores/mowers.svelte';
  import { toasts } from '$lib/stores/ui.svelte';

  type Mower = Fleet['schemas']['MowerOut'];
  type DriveCaps = { manual_drive?: boolean; manual_drive_blades?: boolean };

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

  const pilot = new DrivePilot(realtime);

  // --- data -------------------------------------------------------------------------------------

  async function load(mowerId: string): Promise<void> {
    mower = null;
    problem = null;
    notFound = false;
    caps = null;
    canDrive = null;
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
      .check(mowerId, ['can_drive'])
      .then((result) => (canDrive = result.can_drive))
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
  // A mower with a Bluetooth bridge drives over it: while the bridge is down the cloud link
  // keeps the state coming, but a session would be refused (ADR 0038).
  const bridgeDown = $derived(ms?.links?.ble_bridge?.connected === false);
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
      now = Date.now();
    }, 250);
    return () => clearInterval(timer);
  });

  $effect(() => {
    pilot.speedLimit = speedLimit;
  });
  $effect(() => {
    pilot.bladeOn = live && armed && bladesOn && session?.bladeOnAllowed === true;
  });

  function onsample(vector: StickVector): void {
    if (live) pilot.sample(vector);
  }

  function onengage(value: boolean): void {
    engaged = value;
    if (!value && live) pilot.release();
  }

  const interval = $derived(session ? Math.round(1000 / Math.max(1, session.maxRateHz)) : 100);
  const timeLeft = $derived.by(() => {
    if (!session) return null;
    const left = Math.max(0, session.maxSeconds - Math.floor((now - session.startedAt) / 1000));
    return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
  });
  const output = $derived({
    linear: engaged ? Math.round(stick.linear * speedLimit) / 100 : 0,
    angular: engaged ? stick.angular : 0,
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
  <div class="drive" style:--bar-height="{barHeight}px" data-testid="drive-page">
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

    <section class="map" aria-label={m.map_region()}>
      {#if mowerMap && !mapFailed}
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
    </section>

    <section class="console">
      <div class="strip" data-testid="drive-strip" data-state={stateKey}>
        <span class="lamp" aria-hidden="true"></span>
        <span class="state" data-testid="drive-state">{stateWord}</span>
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
      </div>

      {#if startError}
        <div class="notice" data-testid="drive-error">
          <ProblemNotice title={startError} />
        </div>
      {:else if blocker && !live}
        <div class="notice" data-testid="drive-blocked">
          <ProblemNotice tone="info" title={blocker} />
        </div>
      {/if}

      <div class="stick">
        <Joystick
          label={m.drive_pad()}
          hint={m.drive_pad_hint()}
          disabled={!live}
          {interval}
          bind:value={stick}
          {onsample}
          {onengage}
          testid="joystick"
        />
        <SegmentedControl
          label={m.drive_speed_limit()}
          options={speedOptions}
          value={String(speedLimit)}
          onchange={(value) => (speedLimit = Number(value) as SpeedLimit)}
          testid="drive-speed-limit"
        />
        <div class="session-actions">
          {#if live}
            <Button variant="danger" full icon="stop" testid="drive-stop" onclick={end}
              >{m.drive_stop()}</Button
            >
            <p class="hint">{m.drive_stop_hint()}</p>
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
                disabled={!live || session?.bladeOnAllowed !== true}
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

    <StopBar mowerId={id} onbeforestop={end} bind:height={barHeight} />
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

  /* Phones: header, a short map, the console; the command bar with the stop is fixed. */
  .drive {
    display: flex;
    flex-direction: column;
    padding-bottom: var(--bar-height);
  }

  .map {
    position: relative;
    height: min(30dvh, 260px);
    min-height: 180px;
    border-block: var(--md-hairline) solid var(--md-color-line);
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

  .console {
    display: flex;
    flex-direction: column;
    background: var(--md-color-surface);
  }

  .strip {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    gap: var(--md-space-1) var(--md-space-2);
    padding: var(--md-space-3) var(--md-gutter);
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    background: var(--md-color-surface-sunken);
  }

  .lamp {
    width: 10px;
    height: 10px;
    border: var(--md-border-control) solid var(--md-state-unknown);
    background: transparent;
  }

  .strip[data-state='live'] .lamp {
    border-color: var(--md-state-mowing);
    background: var(--md-state-mowing);
  }

  .strip[data-state='starting'] .lamp {
    border-color: var(--md-state-returning);
  }

  .strip[data-state='off'] .lamp {
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

  .strip[data-state='live'] .state {
    color: var(--md-state-mowing);
  }

  .cells {
    grid-column: 1 / -1;
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

  .stick > :global(.md-segmented) {
    width: 100%;
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

  /* Desktop: the map fills the centre, the console is the instrument column. */
  @media (min-width: 1024px) {
    .drive {
      display: grid;
      grid-template-columns: minmax(0, 1fr) var(--md-instrument-column);
      grid-template-rows: auto minmax(0, 1fr);
      grid-template-areas: 'top top' 'map console';
      height: 100dvh;
      padding-bottom: 0;
      overflow: hidden;
    }

    .top {
      grid-area: top;
    }

    .map {
      grid-area: map;
      height: auto;
      min-height: 0;
      border-block: 0;
    }

    .console {
      grid-area: console;
      min-height: 0;
      overflow-y: auto;
      padding-bottom: calc(var(--bar-height) + var(--md-space-4));
      border-inline-start: var(--md-hairline) solid var(--md-color-line);
    }

    .stick :global(.md-joystick) {
      --md-stick-size: 232px;
    }

    .cells dd {
      font-size: var(--md-font-size-14);
    }
  }
</style>
