<script lang="ts">
  import {
    BatteryBar,
    Button,
    CommandBar,
    Icon,
    MowerMap,
    Panel,
    ProblemNotice,
    Readout,
    SegmentedControl,
    Sheet,
    StatusBadge,
    Stepper,
    Switch,
    TopBar,
    areasOfGap,
    isPositionInexact,
    rtkSummary,
    type HeatGap,
    type Activity,
    type CommandAction,
    type MapSnapshot,
    type RoutePoint,
    type RtkFix,
    type StopState,
  } from '@mowdeck/ui';
  import { untrack } from 'svelte';

  import { goto } from '$app/navigation';
  import { page } from '$app/state';

  import {
    apiFetch,
    fleetApi,
    fleetSlowApi,
    schedulerApi,
    telemetryApi,
    unwrap,
  } from '$lib/api/client';
  import { asProblem, type ApiProblem } from '$lib/api/problem';
  import { clock } from '$lib/clock.svelte';
  import AreaPlan from '$lib/components/AreaPlan.svelte';
  import AreaRenameForm from '$lib/components/AreaRenameForm.svelte';
  import CameraPanel from '$lib/components/CameraPanel.svelte';
  import HeatmapControls, { type HeatStatus } from '$lib/components/HeatmapControls.svelte';
  import HeatmapGaps from '$lib/components/HeatmapGaps.svelte';
  import LightToggle from '$lib/components/LightToggle.svelte';
  import AssistSheet from '$lib/components/AssistSheet.svelte';
  import LiveStatus from '$lib/components/LiveStatus.svelte';
  import MowSettings from '$lib/components/MowSettings.svelte';
  import PlanBanner from '$lib/components/PlanBanner.svelte';
  import RainReadout from '$lib/components/RainReadout.svelte';
  import SlowLoading from '$lib/components/SlowLoading.svelte';
  import ToneBadge from '$lib/components/ToneBadge.svelte';
  import { loadConfig, type MapTilesConfig } from '$lib/config';
  import type { components as Fleet } from '$lib/generated/fleet';
  import {
    activityLabel,
    commandLabel,
    connectionLabel,
    formatAge,
    formatClock,
    formatDateTime,
    formatNumber,
    formatTime,
    outcomeLabel,
    problemMessage,
    rtkFixShort,
    rtkSourceLabel,
    rtkSourceShort,
    type RtkSource,
  } from '$lib/i18n';
  import { commands, permissions, realtime, revoked, sendCommand } from '$lib/live.svelte';
  import { formatDuration } from '$lib/mower/duration';
  import { rainNow, readoutBars, stripBars } from '$lib/mower/rain';
  import type { RainDecision } from '$lib/schedules/types';
  import { availability, polygonArea, primaryCommand, type BarCommand } from '$lib/mower/commands';
  import {
    bladeRange as bladeLimits,
    clampTo,
    displayModel,
    speedRange as speedLimits,
  } from '$lib/mower/limits';
  import { areaNamer, mapLabels } from '$lib/mower/map-labels';
  import {
    offeredPatterns,
    readPattern,
    spacingRange,
    type MowPattern,
  } from '$lib/mower/mow-pattern';
  import {
    PATH_POLL_MS,
    donePercent,
    isJobActivity,
    loadPathShown,
    reconcilePath,
    savePathShown,
    shownPath,
    type MowPath,
  } from '$lib/mower/mow-path';
  import { RTK_PAGE, isRtkRange, loadRange, rangeStart, type RtkRange } from '$lib/mower/rtk-range';
  import {
    ROUTE_LIMIT,
    appendLive,
    isRouteWindow,
    windowStart,
    type RouteWindow,
  } from '$lib/mower/route';
  import { StartSheetFlow } from '$lib/mower/start-sheet.svelte';
  import { jobActive, rainStatusOf } from '$lib/mower/start-sheet';
  import { assistProposal } from '$lib/notifications/kinds';
  import {
    fetchHeatmap,
    heatLayerData,
    isNoMapYet,
    jobAnchor,
    loadHeatPrefs,
    periodWindow,
    saveHeatPrefs,
    sortedGaps,
    type HeatCellSize,
    type HeatLayer,
    type HeatPeriod,
    type HeatmapResponse,
  } from '$lib/map/heatmap';
  import { m } from '$lib/paraglide/messages';
  import { getLocale } from '$lib/paraglide/runtime';
  import { verdictLook } from '$lib/schedules/labels';
  import { parsePreview, type PreviewView } from '$lib/schedules/preview';
  import { formatCountdown, formatFiring } from '$lib/schedules/recurrence';
  import { loadPreview, schedulesOfMower } from '$lib/schedules/runtime';
  import type { Locale, Schedule } from '$lib/schedules/types';
  import type { CommandKind, CommandOutcome } from '$lib/stores/commands.svelte';
  import { lastLoad } from '$lib/stores/last-load';
  import { mowers as states } from '$lib/stores/mowers.svelte';
  import { toasts } from '$lib/stores/ui.svelte';
  import type { VisionTask } from '$lib/vision/api';

  type Mower = Fleet['schemas']['MowerOut'];
  type CommandView = Fleet['schemas']['CommandView'];
  /** The map snapshot (`MowerMap` of the contracts) as the map components read it. */
  type MowerMapData = MapSnapshot;
  type Capabilities = {
    blade_height_range_mm?: number[] | null;
    min_speed_mps?: number | null;
    max_speed_mps?: number | null;
    cutting_width_mm?: number | null;
    native_patterns?: string[];
    path_spacing_range_mm?: number[] | null;
    model?: string | null;
    manual_drive?: boolean;
    camera?: string;
    light?: boolean;
  };

  const id = $derived(page.params.id ?? '');

  let mower = $state<Mower | null>(null);
  let notFound = $state(false);
  let problem = $state<ApiProblem | null>(null);
  let mowerMap = $state.raw<MowerMapData | null>(null);
  let mapEtag: string | null = null;
  let mapFailed = $state(false);
  let mapExpanded = $state(false);
  let phone = $state(false);
  let tiles = $state.raw<MapTilesConfig | null>(null);
  let route = $state.raw<RoutePoint[]>([]);
  let routeSince = $state.raw(new Date());
  let routeTruncated = $state(false);
  let routeWindow = $state<RouteWindow>(storedRouteWindow());
  let caps = $state<Capabilities | null>(null);
  let recent = $state<CommandView[]>([]);
  let canControl = $state(false);
  let canManage = $state(false);
  let canDrive = $state(false);
  // The live camera panel (ADR 0032), inline in the side column (after the status block, before the areas) whenever the mower has
  // a camera; open unless the viewer collapsed it (remembered per browser). Collapsed means no
  // session: the panel unmounts and leaves the channel.
  let cameraOpen = $state(storedCameraOpen());
  // Renaming an area: in the rename sheet (from a map label) or inline in an area list.
  let renaming = $state<string | null>(null);
  let renamingInline = $state<string | null>(null);
  let stopState = $state<StopState>('idle');
  let barHeight = $state(208);
  // RTK summary in the side column: its own time range, loaded in pages. The RTK cells on the
  // map come from the heatmap endpoint like the other heat layers.
  let rtkRange = $state<RtkRange>(storedRtkRange());
  let rtkPoints = $state.raw<RoutePoint[]>([]);
  let rtkTruncated = $state(false);
  let rtkLoading = $state(false);
  // Heat layers of the map: the choice is remembered per browser; the answer belongs to it.
  const heatPrefs = loadHeatPrefs();
  let heatLayer = $state<HeatLayer>(heatPrefs.layer);
  let heatPeriod = $state<HeatPeriod>(heatPrefs.period);
  let heatCell = $state<HeatCellSize>(heatPrefs.cell);
  let heatAnswer = $state.raw<HeatmapResponse | null>(null);
  let heatStatus = $state<HeatStatus>('idle');
  let heatError = $state<string | null>(null);
  let heatFallback = $state(false);
  let heatReload = $state(0);
  /** Index (in the answer's `gaps`) of the highlighted missed spot. */
  let heatGap = $state<number | null>(null);
  // AI assist: the sheet, what a new request asks for, and the proposal a link opened.
  let assisting = $state(false);
  let assistTask = $state<VisionTask>('recover');
  // The mower's planned mow path: the running job's (polled from Telemetry), the last job's
  // (kept until the next start) and a preview computed in the start sheet (`sheet.preview`); the
  // toggle is remembered per browser.
  let pathShown = $state(loadPathShown());
  let livePath = $state.raw<MowPath | null>(null);
  let lastPath = $state.raw<MowPath | null>(null);
  // The last path came from the mower (a preview seed is not polled and not cleared by a null).
  let lastFromJob = $state(false);
  // The path whose job the owner ended ("Zur Station, Aufgabe abbrechen"): not shown again.
  let endedHash = $state<string | null>(null);
  // The next schedule that starts this mower (null: none; undefined: Scheduler not reachable).
  let nextSchedule = $state<Schedule | null | undefined>(undefined);
  let nextPreview = $state<PreviewView | null>(null);
  // The weather at the mower (Scheduler): loaded once and every 15 min. `unreachable` hides the
  // readout, `no_site` is a 404 or a verdict without coordinates.
  type Weather =
    | { kind: 'loading' }
    | { kind: 'unreachable' }
    | { kind: 'no_site' }
    | { kind: 'ok'; decision: RainDecision };
  let weather = $state.raw<Weather>({ kind: 'loading' });
  // The start sheet (areas, pattern and setpoint drafts, preview, rain confirmation, start) and
  // the dock choice: `$lib/mower/start-sheet.svelte` holds the flow, this page the API calls.
  const sheet = new StartSheetFlow({
    preview: (request, signal) =>
      unwrap(
        fleetSlowApi.POST('/api/v1/fleet/mowers/{mower_id}/path-preview', {
          params: { path: { mower_id: id } },
          body: request.pattern
            ? { area_ids: request.area_ids, pattern: { ...request.pattern } }
            : { area_ids: request.area_ids },
          signal,
        }),
      ),
    setpoint: (command) => sendCommand(id, command),
    start: (request) => sendCommand(id, { kind: 'start_mow', ...request }),
    dock: (endJob) => sendCommand(id, { kind: 'return_to_dock', end_job: endJob }),
    saveDefaults: (pattern) => saveMowDefaults(pattern),
    paths: {
      replace: (seed) => {
        dropPath(false);
        lastPath = seed;
      },
      ended: () => dropPath(true),
    },
    now: () => clock.now,
    context: () => ({
      areas: mowerMap?.areas ?? [],
      offered,
      spacing: spacingLimits,
      savedPattern,
      bladeRange,
      speedRange,
      reportedBladeMm: ms?.blade_height_mm,
      reportedSpeedMps: ms?.speed_mps,
      cuttingWidthMm: caps?.cutting_width_mm,
      rain:
        weather.kind === 'ok' && rainStatus === 'ok'
          ? {
              bars: rainBars,
              wetUntil: weather.decision.wet ? (weather.decision.wet_until ?? null) : null,
            }
          : null,
    }),
  });

  async function loadMower(mowerId: string): Promise<void> {
    // The last answer of this tab shows at once; the page never blanks while it reloads.
    const cached = lastLoad.get<Mower>(`mower:${mowerId}`) ?? null;
    mower = cached;
    notFound = false;
    problem = null;
    try {
      const loaded = await unwrap(
        fleetApi.GET('/api/v1/fleet/mowers/{mower_id}', {
          params: { path: { mower_id: mowerId } },
        }),
      );
      if (mowerId !== id) return;
      mower = loaded;
      lastLoad.set(`mower:${mowerId}`, loaded);
    } catch (error) {
      if (mowerId !== id) return;
      const p = asProblem(error);
      if (p.status === 404) {
        mower = null;
        notFound = true;
      } else if (cached) {
        // Keep showing what we had; say that it could not be refreshed.
        toasts.push('error', problemMessage(p), undefined, p.correlationId ?? undefined);
      } else {
        problem = p;
      }
      return;
    }
    const path = { params: { path: { mower_id: mowerId } } };
    void unwrap(telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/state', path))
      .then((snap) => states.upsert(mowerId, snap.state, 'rest'))
      .catch(() => undefined);
    void permissions
      .check(mowerId, ['can_control', 'can_manage_mowers', 'can_drive'])
      .then((result) => {
        canControl = result.can_control;
        canManage = result.can_manage_mowers;
        canDrive = result.can_drive;
      })
      .catch(() => undefined);
    void loadRecent(mowerId);
    void loadNextSchedule(mowerId, mower.group_ids);
  }

  async function loadWeather(
    mowerId: string,
    pause: Schedule['rain_pause'] | undefined,
  ): Promise<void> {
    try {
      // The next schedule's rain pause decides what counts as wet and how long drying takes.
      const query = pause
        ? {
            probability_threshold: pause.probability_threshold,
            precipitation_mm_threshold: pause.precipitation_mm_threshold,
            lookahead_hours: pause.lookahead_hours,
            cooldown_hours: pause.cooldown_hours,
          }
        : {};
      const answer = await unwrap(
        schedulerApi.GET('/api/v1/scheduler/weather/{mower_id}', {
          params: { path: { mower_id: mowerId }, query },
        }),
      );
      if (mowerId !== id) return;
      weather =
        answer.decision.verdict === 'no_location'
          ? { kind: 'no_site' }
          : { kind: 'ok', decision: answer.decision };
    } catch (error) {
      if (mowerId !== id) return;
      weather = asProblem(error).status === 404 ? { kind: 'no_site' } : { kind: 'unreachable' };
    }
  }

  async function loadNextSchedule(mowerId: string, groupIds: string[]): Promise<void> {
    try {
      const list = await unwrap(schedulerApi.GET('/api/v1/scheduler/schedules'));
      if (mowerId !== id) return;
      const first = schedulesOfMower(list.items, mowerId, groupIds)[0] ?? null;
      nextSchedule = first;
      nextPreview = first ? parsePreview(await loadPreview(first.id, 2)) : null;
    } catch {
      // No Scheduler (or no access): the readout stays hidden.
      nextSchedule = undefined;
    }
  }

  function storedCameraOpen(): boolean {
    try {
      return localStorage.getItem('mowdeck.cameraOpen') !== 'false';
    } catch {
      return true;
    }
  }

  function toggleCamera(): void {
    cameraOpen = !cameraOpen;
    try {
      localStorage.setItem('mowdeck.cameraOpen', String(cameraOpen));
    } catch {
      // A per-viewer convenience only.
    }
  }

  function storedRtkRange(): RtkRange {
    try {
      const value = localStorage.getItem('mowdeck.rtkRange');
      return isRtkRange(value) ? value : '7d';
    } catch {
      return '7d';
    }
  }

  function setRtkRange(value: string): void {
    if (!isRtkRange(value)) return;
    rtkRange = value;
    try {
      localStorage.setItem('mowdeck.rtkRange', value);
    } catch {
      // A per-viewer convenience only.
    }
  }

  /** Fetch the map snapshot; with the last ETag a 304 keeps the current map. */
  async function loadMap(mowerId: string): Promise<void> {
    const header = mapEtag ? { 'if-none-match': mapEtag } : undefined;
    try {
      const { data, response } = await telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/map', {
        params: { path: { mower_id: mowerId }, header },
      });
      if (mowerId !== id || response.status === 304 || !response.ok || !data) return;
      mapEtag = response.headers.get('etag');
      mowerMap = data.map;
      lastLoad.set(`map:${mowerId}`, data.map);
    } catch {
      // Keep the last map; the next poll tries again.
    }
  }

  async function loadSnapshots(mowerId: string): Promise<boolean> {
    const path = { params: { path: { mower_id: mowerId } } };
    const [, capsResult] = await Promise.allSettled([
      loadMap(mowerId),
      unwrap(telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/capabilities', path)),
    ]);
    if (capsResult.status === 'fulfilled' && mowerId === id) {
      caps = capsResult.value.capabilities;
      lastLoad.set(`caps:${mowerId}`, caps);
    }
    return mowerMap !== null && caps !== null;
  }

  async function loadRecent(mowerId: string): Promise<void> {
    try {
      const result = await unwrap(
        fleetApi.GET('/api/v1/fleet/mowers/{mower_id}/commands', {
          params: { path: { mower_id: mowerId }, query: { limit: 8 } },
        }),
      );
      recent = result.items;
    } catch {
      recent = [];
    }
  }

  function storedRouteWindow(): RouteWindow {
    try {
      const value = localStorage.getItem('mowdeck.routeWindow');
      return isRouteWindow(value) ? value : 'today';
    } catch {
      return 'today';
    }
  }

  function setRouteWindow(value: string): void {
    if (!isRouteWindow(value)) return;
    routeWindow = value;
    try {
      localStorage.setItem('mowdeck.routeWindow', value);
    } catch {
      // A per-viewer convenience only.
    }
  }

  $effect(() => {
    const mowerId = id;
    mowerMap = lastLoad.get<MowerMapData>(`map:${mowerId}`) ?? null;
    mapEtag = null;
    caps = lastLoad.get<Capabilities>(`caps:${mowerId}`) ?? null;
    sheet.reset();
    renaming = null;
    renamingInline = null;
    livePath = null;
    lastPath = null;
    lastFromJob = false;
    endedHash = null;
    void loadMower(mowerId);
    // The gateway publishes map and capabilities right after connecting: poll briefly for them.
    let tries = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      tries += 1;
      if (!(await loadSnapshots(mowerId)) && tries < 20) timer = setTimeout(poll, 3_000);
    };
    void poll();
    // Afterwards revalidate the map once a minute (304 while unchanged), so zones edited in the
    // vendor app show up without a reload.
    const refresh = setInterval(() => void loadMap(mowerId), 60_000);
    return () => {
      clearTimeout(timer);
      clearInterval(refresh);
    };
  });
  $effect(() => {
    void id;
    weather = { kind: 'loading' };
  });
  $effect(() => {
    const mowerId = id;
    const pause = nextSchedule?.rain_pause;
    untrack(() => void loadWeather(mowerId, pause));
    const refresh = setInterval(() => void loadWeather(mowerId, pause), 15 * 60_000);
    return () => clearInterval(refresh);
  });
  $effect(() => realtime.watch([id]));
  $effect(() => {
    void loadConfig().then((config) => (tiles = config.mapTiles));
  });
  // Phones get a full-screen map mode; leaving the phone layout closes it.
  $effect(() => {
    const query = window.matchMedia('(max-width: 1023px)');
    const update = () => {
      phone = query.matches;
      if (!phone) mapExpanded = false;
    };
    update();
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  });
  // Toasts live in the root layout: lift them above the command bar on phones. There the page
  // scrolls as a whole, so focus scrolling keeps clear of the fixed bar, too.
  $effect(() => {
    const root = document.documentElement;
    const isPhone = !window.matchMedia('(min-width: 1024px)').matches;
    root.style.setProperty('--md-toast-offset', isPhone ? `${barHeight + 8}px` : '16px');
    root.style.setProperty('scroll-padding-bottom', isPhone ? `${barHeight}px` : '');
    return () => {
      root.style.removeProperty('--md-toast-offset');
      root.style.removeProperty('scroll-padding-bottom');
    };
  });
  $effect(() => clock.hold(5_000));
  $effect(() => {
    void permissions.version;
    if (mower) {
      void permissions
        .check(id, ['can_control', 'can_manage_mowers', 'can_drive'])
        .then((result) => {
          canControl = result.can_control;
          canManage = result.can_manage_mowers;
          canDrive = result.can_drive;
        });
    }
  });
  // Refresh the recent commands whenever one of this mower's commands becomes final.
  $effect(() =>
    commands.onFinal((command) => {
      if (command.mowerId === id) void loadRecent(id);
    }),
  );

  /** A theft alarm links here with the position it reported (`/mowers/<id>/map?lat=&lon=`). */
  const alarmAt = $derived.by(() => {
    const lat = Number(page.url.searchParams.get('lat'));
    const lon = Number(page.url.searchParams.get('lon'));
    const valid =
      page.url.searchParams.has('lat') &&
      Number.isFinite(lat) &&
      Number.isFinite(lon) &&
      Math.abs(lat) <= 90 &&
      Math.abs(lon) <= 180;
    return valid ? { lat: lat.toFixed(6), lon: lon.toFixed(6) } : null;
  });
  const canOpenDrive = $derived(canDrive && caps?.manual_drive === true);
  /** Anybody who sees the mower may watch its camera; Fleet checks the plan feature. */
  const hasCamera = $derived(!!caps?.camera && caps.camera !== 'none');

  const snap = $derived(states.get(id));
  const ms = $derived(snap?.state ?? null);
  const activity = $derived((ms?.activity ?? null) as Activity | null);
  const stale = $derived(states.isStale(id, clock.now));
  const accessEnded = $derived(revoked.has(id));

  // Commands from other devices or integrations show up when the activity changes.
  $effect(() => {
    if (activity && mower) void loadRecent(id);
  });

  // --- AI assist ("KI-Hilfe") -------------------------------------------------------------------

  /** The inbox notification `vision_proposal` links here with `?assist=<proposal id>`. */
  const assistLink = $derived(assistProposal(page.url.search));
  $effect(() => {
    if (assistLink && mower) {
      untrack(() => {
        sheet.close();
        assisting = true;
      });
    }
  });
  /** Stuck (error): recover. Standing away from the dock (idle or paused, not charging): find it. */
  const canRecover = $derived(canDrive && activity === 'error');
  const canFindDock = $derived(
    canDrive && (activity === 'idle' || activity === 'paused') && ms?.charging !== true,
  );

  function openAssist(task: VisionTask): void {
    assistTask = task;
    sheet.close();
    assisting = true;
  }

  function closeAssist(): void {
    assisting = false;
    if (assistLink) {
      const url = new URL(page.url);
      url.searchParams.delete('assist');
      void goto(`${url.pathname}${url.search}`, {
        replaceState: true,
        noScroll: true,
        keepFocus: true,
      });
    }
  }

  // --- planned mow path: polled while a job runs and while its path is held; the job's path stays
  // while the job exists on the mower and goes when the gateway has none and the mower rests ---

  const jobRunning = $derived(isJobActivity(activity));
  const pollPath = $derived(jobRunning || (lastPath !== null && lastFromJob));

  function applyPolled(polled: MowPath | null): void {
    const next = reconcilePath(
      { live: livePath, last: lastPath },
      polled,
      untrack(() => activity),
      endedHash,
    );
    livePath = next.live;
    lastPath = next.last;
    lastFromJob = next.last !== null;
  }

  $effect(() => {
    const mowerId = id;
    if (!pollPath) {
      livePath = null;
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async (): Promise<void> => {
      try {
        const { data, response } = await telemetryApi.GET(
          '/api/v1/telemetry/mowers/{mower_id}/path',
          { params: { path: { mower_id: mowerId } } },
        );
        if (cancelled) return;
        if (response.ok && data) applyPolled(data.path ?? null);
        else if (response.status === 404) applyPolled(null);
      } catch {
        // Keep what is shown; the next poll tries again.
      }
      if (!cancelled) timer = setTimeout(() => void load(), PATH_POLL_MS);
    };
    void load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  });

  function setPathShown(shown: boolean): void {
    pathShown = shown;
    savePathShown(shown);
  }

  // --- driven route: the track of the chosen window plus the live positions -------------------

  $effect(() => {
    const mowerId = id;
    const since = windowStart(routeWindow, new Date());
    let cancelled = false;
    routeSince = since;
    route = [];
    routeTruncated = false;
    void unwrap(
      telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/positions', {
        params: {
          path: { mower_id: mowerId },
          query: { from: since.toISOString(), limit: ROUTE_LIMIT },
        },
      }),
    )
      .then((track) => {
        if (cancelled) return;
        const last = track.positions[track.positions.length - 1];
        const after = last ? Date.parse(last.observed_at) : -Infinity;
        // Live points that arrived while the track loaded stay at the end.
        const live = untrack(() => route).filter((p) => Date.parse(p.observed_at) > after);
        route = [...track.positions, ...live].slice(-ROUTE_LIMIT);
        routeTruncated = track.truncated;
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  });

  $effect(() => {
    const latest = ms;
    const current = untrack(() => route);
    const next = appendLive(
      current,
      latest,
      untrack(() => routeSince),
    );
    if (next !== current) route = next;
  });

  // --- RTK signal map: positions of the chosen range, newest first in pages -------------------

  $effect(() => {
    const mowerId = id;
    const range = rtkRange;
    let cancelled = false;
    rtkLoading = true;
    const now = new Date();
    void loadRange(
      async (query) => {
        const track = await unwrap(
          telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/positions', {
            params: { path: { mower_id: mowerId }, query },
          }),
        );
        return { positions: track.positions, truncated: track.truncated };
      },
      rangeStart(range, now),
      now,
      { limit: RTK_PAGE },
    )
      .then((loaded) => {
        if (cancelled) return;
        rtkPoints = loaded.points;
        rtkTruncated = loaded.truncated;
      })
      .catch(() => {
        if (!cancelled) rtkPoints = [];
      })
      .finally(() => {
        if (!cancelled) rtkLoading = false;
      });
    return () => {
      cancelled = true;
    };
  });

  // --- heat layers: one answer of the heatmap endpoint for the chosen layer and period --------

  function rememberHeat(): void {
    saveHeatPrefs({ layer: heatLayer, period: heatPeriod, cell: heatCell });
  }

  function setHeatLayer(layer: HeatLayer): void {
    heatLayer = layer;
    heatGap = null;
    rememberHeat();
  }

  function setHeatPeriod(period: HeatPeriod): void {
    heatPeriod = period;
    rememberHeat();
  }

  function setHeatCell(cell: HeatCellSize): void {
    heatCell = cell;
    rememberHeat();
  }

  /** The last job among the recent commands; the `job` period reloads when it changes. */
  const heatJob = $derived(heatPeriod === 'job' ? jobAnchor(recent) : '');
  const heatMapReady = $derived(!!mowerMap);

  $effect(() => {
    const mowerId = id;
    const layer = heatLayer;
    const period = heatPeriod;
    const cell = heatCell;
    void heatReload;
    void heatJob;
    if (layer === 'off' || !heatMapReady) {
      heatAnswer = null;
      heatStatus = 'idle';
      heatFallback = false;
      return;
    }
    const window = periodWindow(
      period,
      new Date(),
      untrack(() => recent),
    );
    // Another layer's cells would read wrong under this legend: drop them while loading.
    if (untrack(() => heatAnswer)?.layer !== layer) heatAnswer = null;
    heatFallback = !!window.fallback;
    heatStatus = 'loading';
    heatError = null;
    const abort = new AbortController();
    fetchHeatmap(apiFetch, mowerId, layer, window, cell, abort.signal)
      .then((answer) => {
        if (abort.signal.aborted) return;
        heatAnswer = answer;
        heatGap = null;
        heatStatus = answer.cells.length ? 'ready' : 'empty';
      })
      .catch((error: unknown) => {
        if (abort.signal.aborted) return;
        const p = asProblem(error);
        heatAnswer = null;
        heatGap = null;
        if (isNoMapYet(p)) {
          heatStatus = 'no-map';
        } else {
          heatStatus = 'error';
          heatError = problemMessage(p);
        }
      });
    return () => abort.abort();
  });

  const heat = $derived(heatLayer === 'off' ? null : heatLayerData(heatAnswer));
  const heatGaps = $derived(heatLayer === 'coverage' ? sortedGaps(heatAnswer) : []);
  const highlightGap = $derived<HeatGap | null>(
    heatLayer === 'coverage' && heatGap !== null ? (heatAnswer?.gaps?.[heatGap] ?? null) : null,
  );

  /** The mapped area around a missed spot, for "mow this area". */
  function areaOfGap(gap: HeatGap): { id: string; name: string } | null {
    const [areaId] = areasOfGap(gap, mowerMap?.areas);
    const area = (mowerMap?.areas ?? []).find((a) => a.id === areaId);
    return area ? { id: area.id, name: areaName(area) } : null;
  }

  /** Open the area start with the spot's area chosen (spot mowing itself is not in the app). */
  function mowGapArea(areaId: string): void {
    sheet.picked = [areaId];
    void onaction('start');
  }

  const rtk = $derived(rtkSummary(rtkPoints));
  const rtkOptions = $derived([
    { value: 'today', label: m.rtk_map_today() },
    { value: '7d', label: m.rtk_map_7d() },
    { value: '30d', label: m.rtk_map_30d() },
  ]);
  const locale = $derived(getLocale() as Locale);

  // Custom names from Fleet first, then the mower's own name, then "Bereich N" in map order.
  const areaNames = $derived(mower?.area_names ?? {});
  const areaName = $derived(areaNamer(mowerMap?.areas, areaNames));
  const areaIndex = $derived(new Map((mowerMap?.areas ?? []).map((a, i) => [a.id, i] as const)));
  const renamingArea = $derived(
    renaming ? ((mowerMap?.areas ?? []).find((a) => a.id === renaming) ?? null) : null,
  );

  /** A map label was tapped: rename inline while the picker is open, else in the sheet. */
  function openRename(areaId: string): void {
    if (sheet.picking) renamingInline = areaId;
    else renaming = areaId;
  }

  function areaNamesSaved(names: Record<string, string>): void {
    if (mower) {
      mower = { ...mower, area_names: names };
      lastLoad.set(`mower:${mower.id}`, mower);
    }
    renaming = null;
    renamingInline = null;
  }

  // --- commands ---------------------------------------------------------------------------------

  const BAR: {
    id: BarCommand;
    kind: CommandKind;
    label: () => string;
    icon: CommandAction['icon'];
  }[] = [
    { id: 'start', kind: 'start_mow', label: () => m.cmd_start(), icon: 'play' },
    { id: 'pause', kind: 'pause_mow', label: () => m.cmd_pause(), icon: 'pause' },
    { id: 'resume', kind: 'resume_mow', label: () => m.cmd_resume(), icon: 'play' },
    { id: 'dock', kind: 'return_to_dock', label: () => m.cmd_dock(), icon: 'dock' },
  ];
  const REASONS = {
    start: () => m.reason_start(),
    pause: () => m.reason_pause(),
    resume: () => m.reason_resume(),
    dock: () => m.reason_dock(),
    offline: () => m.reason_offline(),
    updating: () => m.reason_updating(),
  } as const;

  const actions = $derived.by((): CommandAction[] => {
    if (!canControl) return [];
    const primary = primaryCommand(activity);
    return BAR.map((item) => {
      const a = availability(item.id, activity);
      const noAreas = item.id === 'start' && a.enabled && !(mowerMap?.areas?.length ?? 0);
      return {
        id: item.id,
        label: item.label(),
        icon: item.icon,
        enabled: a.enabled && !noAreas,
        reason: noAreas ? m.detail_areas_empty() : a.reason ? REASONS[a.reason]() : undefined,
        pending: commands.pending(id, item.kind),
        primary: primary === item.id,
      };
    });
  });

  async function onaction(action: string): Promise<void> {
    if (action === 'start') {
      // Areas tapped on the map are already chosen; the sheet confirms (and can change) them.
      assisting = false;
      renaming = null;
      renamingInline = null;
      sheet.open();
      return;
    }
    if (action === 'dock' && jobActive(activity)) {
      sheet.dockChoice = true;
      return;
    }
    const item = BAR.find((b) => b.id === action);
    if (item) await sendCommand(id, { kind: item.kind });
  }

  /** The owner ended the job (or starts another): the planned path goes from the map. */
  function dropPath(ended: boolean): void {
    endedHash = ended ? (livePath?.path_hash ?? lastPath?.path_hash ?? endedHash) : null;
    livePath = null;
    lastPath = null;
    lastFromJob = false;
  }

  // Patterns the mower runs natively; none (or unknown capabilities): the start names no
  // pattern and the mower uses its own settings, as before.
  const offered = $derived(offeredPatterns(caps?.native_patterns));
  const spacingLimits = $derived(spacingRange(caps?.path_spacing_range_mm));
  const savedPattern = $derived(readPattern(mower?.mow_defaults?.pattern));

  const mapPath = $derived(
    shownPath({ shown: pathShown, preview: sheet.previewPath, live: livePath, last: lastPath }),
  );
  const pathNote = $derived.by(() => {
    const percent = donePercent(livePath);
    if (livePath?.progress && percent !== null) {
      const done = m.path_status_done({
        done: formatNumber(Math.min(livePath.progress.index, livePath.points_total)),
        total: formatNumber(livePath.points_total),
      });
      return `${percent} % · ${done}`;
    }
    return jobRunning && !livePath ? m.path_status_waiting() : null;
  });

  // Rain at the mower: the readout in the side column and the warning in the start sheet. The
  // Scheduler decides (verdict, when the hold ends, the thresholds it applied); the page shows it.
  const rainThresholds = $derived(weather.kind === 'ok' ? weather.decision.settings : null);
  const rainBars = $derived(
    weather.kind === 'ok'
      ? readoutBars(weather.decision.hours, weather.decision.settings, clock.now)
      : [],
  );
  const rainStatus = $derived(
    rainStatusOf(weather.kind === 'ok' ? weather.decision.verdict : null),
  );
  const rainPaused = $derived(
    weather.kind === 'ok' && !!nextSchedule?.rain_pause.enabled && weather.decision.holds,
  );
  const rainPausedUntil = $derived(
    rainPaused && weather.kind === 'ok' ? (weather.decision.pause_until ?? null) : null,
  );

  /** The pattern of a start that went out becomes the mower's mow defaults. */
  async function saveMowDefaults(pattern: MowPattern): Promise<void> {
    const mowerId = id;
    try {
      const saved = await unwrap(
        fleetApi.PUT('/api/v1/fleet/mowers/{mower_id}/mow-defaults', {
          params: { path: { mower_id: mowerId } },
          body: { pattern: { ...pattern } },
        }),
      );
      if (mowerId !== id) return;
      mower = saved;
      lastLoad.set(`mower:${mowerId}`, saved);
    } catch (error) {
      const p = asProblem(error);
      toasts.push(
        'error',
        m.start_defaults_failed(),
        problemMessage(p),
        p.correlationId ?? undefined,
      );
    }
  }

  /** From the start sheet to the blade height and speed steppers. */
  function adjustSetpoints(): void {
    sheet.close();
    document.getElementById('mow-controls')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  const hasMap = $derived(!!mowerMap?.areas?.length);
  const canStart = $derived(
    canControl && availability('start', activity).enabled && !!mowerMap?.areas?.length,
  );
  /** Areas on the map can be tapped whenever a start is possible (or the picker is open). */
  const mapSelectable = $derived(canControl && (canStart || sheet.picking));

  const labels = $derived(mapLabels());
  const routeOptions = $derived([
    { value: '1h', label: m.map_route_1h() },
    { value: 'today', label: m.map_route_today() },
    { value: '24h', label: m.map_route_24h() },
  ]);

  async function onstop(): Promise<void> {
    stopState = 'sending';
    const ok = await sendCommand(id, { kind: 'emergency_stop' });
    // The result can arrive over the socket before the 202 response returns (the simulated
    // mower answers within a millisecond). Never overwrite a state the result already set.
    if (!ok) stopState = 'failed';
    else if (stopState === 'sending') stopState = 'sent';
  }

  const lastStop = $derived(commands.latest(id, 'emergency_stop'));
  $effect(() => {
    if (lastStop?.status === 'executed') stopState = 'executed';
    else if (lastStop && ['rejected', 'failed', 'expired'].includes(lastStop.status)) {
      stopState = 'failed';
    }
  });
  const stopStatus = $derived(
    stopState === 'sending'
      ? m.stop_sending()
      : stopState === 'sent'
        ? m.stop_sent()
        : stopState === 'executed'
          ? m.stop_executed()
          : stopState === 'failed'
            ? m.stop_failed()
            : '',
  );

  // --- setpoints --------------------------------------------------------------------------------

  // Limits come from the model's capabilities; nothing is invented when they are unknown.
  const bladeRange = $derived(bladeLimits(caps));
  const speedRange = $derived(speedLimits(caps));
  const modelName = $derived(displayModel(caps, mower));
  const limitsLine = $derived.by(() => {
    const parts: string[] = [];
    if (bladeRange)
      parts.push(
        m.detail_limits_blade({
          min: formatNumber(bladeRange[0]),
          max: formatNumber(bladeRange[1]),
        }),
      );
    if (speedRange)
      parts.push(
        m.detail_limits_speed({
          min: formatNumber(speedRange[0], 1),
          max: formatNumber(speedRange[1], 1),
        }),
      );
    return parts.join(' · ');
  });
  const bladePending = $derived.by(() => {
    const last = commands.latest(id, 'set_blade_height');
    return !!last && !['executed', 'rejected', 'failed', 'expired'].includes(last.status);
  });
  const speedPending = $derived(commands.pending(id, 'set_speed'));
  const lightCapable = $derived(caps?.light === true);
  const lightPending = $derived(commands.pending(id, 'set_light'));
  const controlsEnabled = $derived(activity !== null && activity !== 'offline');

  function tone(fix: string | undefined): 'mowing' | 'paused' | 'error' | null {
    return fix === 'fixed'
      ? 'mowing'
      : fix === 'float'
        ? 'paused'
        : fix === 'none'
          ? 'error'
          : null;
  }

  function outcomeOf(status: string): CommandOutcome {
    return (
      ['accepted', 'executed', 'rejected', 'failed', 'expired'].includes(status)
        ? status
        : 'pending'
    ) as CommandOutcome;
  }
</script>

<svelte:head><title>{mower?.name ?? m.mowers_title()} · {m.app_name()}</title></svelte:head>

{#snippet lightTile()}
  <LightToggle
    on={ms?.light_on ?? null}
    capable={lightCapable}
    online={controlsEnabled}
    allowed={canControl}
    pending={lightPending}
    onchange={(on) => void sendCommand(id, { kind: 'set_light', on })}
  />
{/snippet}

{#if notFound || accessEnded}
  <TopBar title={m.detail_not_found_title()} backHref="/mowers" backLabel={m.back()} />
  <div class="pad" data-testid="mower-not-found">
    <ProblemNotice
      tone="info"
      title={accessEnded ? m.detail_access_ended() : m.detail_not_found_title()}
      detail={accessEnded ? null : m.detail_not_found_body()}
    />
    <Button href="/mowers" icon="list">{m.detail_back_to_list()}</Button>
  </div>
{:else if problem}
  <TopBar title={m.mowers_title()} backHref="/mowers" backLabel={m.back()} />
  <div class="pad">
    <ProblemNotice
      title={problemMessage(problem)}
      reference={problem.correlationId}
      referenceLabel={m.problem_reference()}
    />
  </div>
{:else if mower}
  <div class="detail" style:--md-commandbar-height="{barHeight}px">
    <div class="top">
      <TopBar
        title={mower.name}
        meta={[connectionLabel(mower.connection_kind), modelName, mower.serial]
          .filter(Boolean)
          .join(' · ')}
        backHref="/mowers"
        backLabel={m.back()}
      >
        {#snippet status()}
          <LiveStatus phase={realtime.phase} tooMany={realtime.tooMany} />
        {/snippet}
        {#snippet actions()}
          <Button
            href="/mowers/{id}/health"
            variant="ghost"
            icon="pulse"
            label={m.health_open()}
            testid="mower-health"
          />
          {#if canOpenDrive}
            <Button
              href="/mowers/{id}/drive"
              variant="ghost"
              icon="joystick"
              label={m.drive_open()}
              testid="mower-drive"
            />
          {/if}
          {#if canManage}
            <Button
              href="/mowers/{id}/settings"
              variant="ghost"
              icon="settings"
              label={m.detail_settings()}
              testid="mower-settings"
            />
          {/if}
        {/snippet}
      </TopBar>
      <PlanBanner />
    </div>

    <section class="plan" class:expanded={mapExpanded} aria-label={m.detail_areas()}>
      {#if mowerMap && hasMap && !mapFailed}
        <div class="map-box">
          <MowerMap
            map={mowerMap}
            state={ms}
            {route}
            selectedAreas={sheet.picked}
            selectable={mapSelectable}
            {stale}
            {tiles}
            {labels}
            {heat}
            {highlightGap}
            path={mapPath.path}
            pathPreview={mapPath.preview}
            {areaName}
            onselectionchange={(ids) => (sheet.picked = ids)}
            onarealabel={canControl ? openRename : undefined}
            onfail={() => (mapFailed = true)}
            expanded={mapExpanded}
            onexpandchange={phone ? (value) => (mapExpanded = value) : undefined}
          />
          {#if sheet.picked.length && !sheet.picking}
            <div class="chosen" data-testid="map-selection">
              <span class="mono">{m.map_selected({ count: sheet.picked.length })}</span>
              <button
                type="button"
                class="chosen-clear"
                aria-label={m.map_clear_selection()}
                onclick={() => (sheet.picked = [])}
              >
                <Icon name="close" size={18} />
              </button>
            </div>
          {/if}
        </div>
        <HeatmapControls
          layer={heatLayer}
          period={heatPeriod}
          cell={heatCell}
          status={heatStatus}
          error={heatError}
          answeredCell={heatAnswer?.cell_m ?? null}
          samples={heatAnswer?.samples ?? null}
          truncated={heatAnswer?.truncated ?? false}
          fallback={heatFallback}
          onlayer={setHeatLayer}
          onperiod={setHeatPeriod}
          oncell={setHeatCell}
          onrefresh={() => (heatReload += 1)}
          {pathShown}
          {pathNote}
          onpath={setPathShown}
        />
        {#if heatLayer === 'coverage' && (heatStatus === 'ready' || heatStatus === 'empty')}
          <HeatmapGaps
            gaps={heatGaps}
            selected={heatGap}
            onselect={(index) => (heatGap = index)}
            areaOf={areaOfGap}
            {canStart}
            onmowarea={mowGapArea}
          />
        {/if}
      {:else if mowerMap && hasMap}
        <div class="map-box">
          <AreaPlan
            map={mowerMap}
            selected={sheet.picking ? sheet.picked : []}
            label={m.detail_areas()}
            {areaName}
          />
        </div>
      {:else}
        <div class="plan-empty">
          <span>{m.detail_areas_empty()}</span>
        </div>
      {/if}
    </section>

    <div class="side">
      <div class="status">
        <div class="state-row" data-testid="mower-state">
          {#if activity}
            <StatusBadge {activity} label={activityLabel(activity)} {stale} size="large" />
          {:else}
            <StatusBadge activity="unknown" label={m.activity_no_telemetry()} size="large" />
          {/if}
          <span class="age">
            {ms ? formatAge(ms.observed_at, clock.now) : m.detail_no_telemetry()}
          </span>
        </div>
        {#if ms?.error_code}
          <ProblemNotice title="{ms.error_code} {ms.error_message ?? ''}" />
        {/if}
        {#if canRecover || canFindDock}
          <div class="assist-entry" data-testid="assist-entry">
            {#if canRecover}
              <Button
                variant="primary"
                icon="send"
                onclick={() => openAssist('recover')}
                testid="assist-open-recover">{m.vision_open_recover()}</Button
              >
            {:else}
              <Button icon="dock" onclick={() => openAssist('find_dock')} testid="assist-open-dock"
                >{m.vision_open_find_dock()}</Button
              >
            {/if}
          </div>
        {/if}
        {#if alarmAt}
          <div class="alarm" data-testid="alarm-position">
            <ProblemNotice
              tone="info"
              title={m.detail_alarm_position()}
              detail={m.detail_alarm_position_body(alarmAt)}
            />
          </div>
        {/if}

        <div class="readouts">
          <Readout
            label={m.detail_battery()}
            value={ms ? formatNumber(ms.battery_percent) : null}
            unit="%"
            size="primary"
            emptyText={m.no_data()}
            testid="readout-battery"
          >
            <BatteryBar percent={ms?.battery_percent ?? null} charging={ms?.charging} />
          </Readout>
          <Readout
            label={m.detail_rtk()}
            value={ms ? rtkFixShort(ms.rtk_fix as RtkFix) : null}
            unit={ms ? rtkSourceShort(ms.rtk_source as RtkSource) : undefined}
            tone={tone(ms?.rtk_fix)}
            emptyText={m.no_data()}
            testid="readout-rtk"
          >
            {#if isPositionInexact(ms)}
              <span class="hint inexact" data-testid="readout-rtk-inexact"
                >{m.detail_rtk_inexact()}</span
              >
            {/if}
          </Readout>
          <Readout
            label={m.detail_blade()}
            value={ms?.blade_height_mm != null ? formatNumber(ms.blade_height_mm) : null}
            unit="mm"
            emptyText={m.no_data()}
            pending={bladePending}
            testid="readout-blade"
          />
          <Readout
            label={m.detail_speed()}
            value={ms?.speed_mps != null ? formatNumber(ms.speed_mps, 2) : null}
            unit="m/s"
            emptyText={m.no_data()}
            pending={speedPending}
            testid="readout-speed"
          />
          <Readout
            label={m.detail_connection()}
            value={ms ? connectionLabel(ms.connection) : connectionLabel(mower.connection_kind)}
            size="inline"
            emptyText={m.no_data()}
            testid="readout-connection"
          />
          <Readout
            label={m.detail_firmware()}
            value={ms?.firmware ?? null}
            size="inline"
            emptyText={m.no_data()}
          />
          <Readout
            label={m.detail_rtk_source()}
            value={ms ? rtkSourceLabel(ms.rtk_source as RtkSource) : null}
            size="inline"
            emptyText={m.no_data()}
          />
          <Readout
            label={m.detail_reported()}
            value={ms ? formatTime(ms.observed_at) : null}
            size="inline"
            emptyText={m.no_data()}
          />
        </div>
      </div>

      <div class="extras">
        {#if hasCamera}
          <div class="camera-slot">
            <Panel label={m.camera_title()} testid="camera">
              {#snippet actions()}
                {#if cameraOpen}
                  <Button
                    variant="ghost"
                    icon="expand"
                    href="/mowers/{id}/camera"
                    label={m.camera_fullscreen()}
                    testid="camera-fullscreen"
                  />
                {/if}
                <Button
                  variant="ghost"
                  icon={cameraOpen ? 'minus' : 'camera'}
                  label={cameraOpen ? m.camera_collapse() : m.camera_expand()}
                  expanded={cameraOpen}
                  testid="camera-toggle"
                  onclick={toggleCamera}
                />
              {/snippet}
              {@render lightTile()}
              {#if cameraOpen}
                {#key id}
                  <CameraPanel
                    mowerId={id}
                    mowerName={mower.name}
                    position={ms?.position ?? null}
                    link={false}
                  />
                {/key}
              {:else}
                <p class="hint camera-off" data-testid="camera-collapsed">{m.camera_collapsed()}</p>
              {/if}
            </Panel>
          </div>
        {/if}
        {#if canControl}
          <div id="mow-controls"></div>
          <Panel label={m.detail_controls()} testid="controls">
            <div class="steppers">
              {#if lightCapable && !hasCamera}
                {@render lightTile()}
              {/if}
              {#if bladeRange}
                <Stepper
                  id="blade"
                  label={m.detail_blade()}
                  value={clampTo(ms?.blade_height_mm, bladeRange)}
                  min={bladeRange[0]}
                  max={bladeRange[1]}
                  step={5}
                  unit="mm"
                  format={(v) => formatNumber(v)}
                  decreaseLabel={m.blade_lower()}
                  increaseLabel={m.blade_raise()}
                  disabled={!controlsEnabled}
                  pending={bladePending}
                  oncommit={(mm) => void sendCommand(id, { kind: 'set_blade_height', mm })}
                />
              {:else}
                <p class="hint" data-testid="blade-unknown">
                  {m.detail_blade()}: {m.detail_limits_unknown()}
                </p>
              {/if}
              {#if speedRange}
                <Stepper
                  id="speed"
                  label={m.detail_speed()}
                  value={clampTo(ms?.speed_mps, speedRange)}
                  min={speedRange[0]}
                  max={speedRange[1]}
                  step={0.1}
                  decimals={2}
                  unit="m/s"
                  format={(v) => formatNumber(v, 2)}
                  decreaseLabel={m.speed_lower()}
                  increaseLabel={m.speed_raise()}
                  disabled={!controlsEnabled}
                  pending={speedPending}
                  oncommit={(mps) => void sendCommand(id, { kind: 'set_speed', mps })}
                />
              {:else}
                <p class="hint" data-testid="speed-unknown">
                  {m.detail_speed()}: {m.detail_limits_unknown()}
                </p>
              {/if}
            </div>
            <p class="hint">{m.detail_controls_hint()}</p>
            {#if limitsLine}
              <p class="hint mono" data-testid="model-limits">
                {m.detail_limits()}: {limitsLine}
              </p>
            {/if}
          </Panel>
        {/if}

        {#if hasMap && !mapFailed}
          <Panel label={m.map_route()} testid="route-panel">
            <SegmentedControl
              label={m.map_route_window()}
              options={routeOptions}
              value={routeWindow}
              onchange={setRouteWindow}
              testid="route-window"
            />
            <p class="hint mono" data-testid="route-points">
              {m.map_route_points({ count: formatNumber(route.length) })}
              {#if routeTruncated}· {m.map_route_truncated()}{/if}
            </p>
          </Panel>
        {/if}

        {#if weather.kind !== 'unreachable'}
          <Panel label={m.rain_title()} testid="rain-panel">
            {#if weather.kind === 'loading'}
              <p class="hint"><SlowLoading /></p>
            {:else}
              <RainReadout
                status={rainStatus}
                now={rainNow(rainBars, clock.now)}
                bars={stripBars(rainBars, clock.now)}
                threshold={rainThresholds?.probability_threshold ?? 0}
                paused={rainPaused}
                pauseUntil={rainPausedUntil}
                clock={clock.now}
                wet={weather.kind === 'ok' && weather.decision.wet}
                wetUntil={weather.kind === 'ok' ? (weather.decision.wet_until ?? null) : null}
                settingsHref="/mowers/{id}/settings"
              />
            {/if}
          </Panel>
        {/if}

        {#if nextSchedule !== undefined}
          <Panel label={m.detail_next_schedule()} testid="next-schedule">
            {#if nextSchedule}
              {@const look = verdictLook(
                nextSchedule.rain_pause.enabled ? (nextPreview?.badge ?? 'unknown') : 'disabled',
              )}
              <a class="next" href="/schedules/{nextSchedule.id}" data-testid="next-schedule-link">
                <span class="next-name">{nextSchedule.name}</span>
                <span class="next-when mono">
                  {formatFiring(
                    nextPreview?.next?.at ?? nextSchedule.next_run_at ?? '',
                    nextSchedule.timezone,
                    locale,
                  )}
                  · {formatCountdown(
                    nextPreview?.next?.at ?? nextSchedule.next_run_at ?? '',
                    clock.now,
                    locale,
                  )}
                </span>
                <ToneBadge {...look} value={nextPreview?.badge} testid="next-schedule-verdict" />
              </a>
            {:else}
              <p class="hint">{m.detail_no_schedule()}</p>
            {/if}
            <div class="next-actions">
              <Button variant="ghost" href="/schedules" icon="calendar">{m.nav_schedules()}</Button>
              {#if canControl && !nextSchedule}
                <Button variant="ghost" href="/schedules/new?mower={id}" icon="plus"
                  >{m.sched_new()}</Button
                >
              {/if}
            </div>
          </Panel>
        {/if}

        {#if hasMap && !mapFailed}
          <Panel label={m.rtk_map_title()} testid="rtk-panel">
            <SegmentedControl
              label={m.rtk_map_range()}
              options={rtkOptions}
              value={rtkRange}
              onchange={setRtkRange}
              testid="rtk-range"
            />
            <div class="rtk-bar" aria-hidden="true">
              <span class="seg seg-fixed" style:flex-grow={rtk.shares.fixed}></span>
              <span class="seg seg-float" style:flex-grow={rtk.shares.float}></span>
              <span class="seg seg-none" style:flex-grow={rtk.shares.none}></span>
              <span class="seg seg-unknown" style:flex-grow={rtk.shares.unknown}></span>
            </div>
            <div class="rtk-readouts" data-testid="rtk-summary">
              <Readout
                label={m.rtk_fixed()}
                value={rtk.total ? formatNumber(rtk.shares.fixed * 100) : null}
                unit="%"
                tone="mowing"
                size="inline"
                emptyText={m.no_data()}
                testid="rtk-share-fixed"
              />
              <Readout
                label={m.rtk_float()}
                value={rtk.total ? formatNumber(rtk.shares.float * 100) : null}
                unit="%"
                tone="paused"
                size="inline"
                emptyText={m.no_data()}
              />
              <Readout
                label={m.rtk_none()}
                value={rtk.total ? formatNumber(rtk.shares.none * 100) : null}
                unit="%"
                tone="error"
                size="inline"
                emptyText={m.no_data()}
              />
            </div>
            <p class="hint mono" data-testid="rtk-points">
              {rtkLoading ? m.loading() : m.map_route_points({ count: formatNumber(rtk.total) })}
              {#if rtkTruncated}· {m.rtk_map_truncated()}{/if}
            </p>
            <Switch
              id="rtk-show"
              testid="rtk-show"
              label={m.rtk_map_show()}
              hint={m.rtk_map_show_hint()}
              states={{ on: m.switch_on(), off: m.switch_off() }}
              checked={heatLayer === 'rtk'}
              onchange={(on) => setHeatLayer(on ? 'rtk' : 'off')}
            />
          </Panel>
        {/if}

        {#if mowerMap && hasMap}
          <Panel label={m.detail_areas()}>
            <ul class="areas">
              {#each mowerMap.areas ?? [] as area (area.id)}
                {@const order = sheet.picked.indexOf(area.id)}
                <li>
                  <span class="area-row">
                    <span class="area-label">
                      {#if order >= 0}<span class="badge mono">{order + 1}</span>{/if}
                      <span data-testid="area-name">{areaName(area)}</span>
                    </span>
                    <span class="mono">
                      {m.detail_area_size({
                        size: formatNumber(polygonArea(area.boundary)),
                      })}
                    </span>
                    {#if canControl}
                      <button
                        type="button"
                        class="rename-open"
                        aria-label={m.area_rename_label({ name: areaName(area) })}
                        aria-expanded={renamingInline === area.id && !sheet.picking}
                        data-testid="area-rename-open"
                        data-rename-area={area.id}
                        onclick={() =>
                          (renamingInline = renamingInline === area.id ? null : area.id)}
                      >
                        <Icon name="edit" size={18} />
                      </button>
                    {/if}
                  </span>
                  {#if canControl && renamingInline === area.id && !sheet.picking}
                    <AreaRenameForm
                      mowerId={id}
                      {area}
                      index={areaIndex.get(area.id) ?? null}
                      names={areaNames}
                      compact
                      onsaved={areaNamesSaved}
                      oncancel={() => (renamingInline = null)}
                    />
                  {/if}
                </li>
              {/each}
            </ul>
            {#if mapSelectable && !mapFailed}
              <p class="hint">{m.map_select_hint()}</p>
            {/if}
          </Panel>
        {:else if mapFailed}
          <p class="hint pad-x">{m.map_fallback()}</p>
        {/if}

        <div id="recent-commands"></div>
        <Panel label={m.detail_recent()} testid="recent-commands">
          {#if recent.length}
            <ol class="recent">
              {#each recent as item (item.command_id)}
                <li data-status={item.status}>
                  <span class="kind"
                    >{commandLabel(
                      item.kind as CommandKind,
                      item.end_job,
                      readPattern(item.pattern),
                    )}</span
                  >
                  <span class="outcome">{outcomeLabel(outcomeOf(item.status))}</span>
                  <span class="when mono">{formatDateTime(item.issued_at)}</span>
                </li>
              {/each}
            </ol>
          {:else}
            <p class="hint">{m.detail_recent_empty()}</p>
          {/if}
        </Panel>
      </div>
    </div>

    <div class="bar" bind:clientHeight={barHeight}>
      <CommandBar
        {actions}
        {onaction}
        label={m.cmd_bar_label()}
        stopLabel={m.cmd_stop()}
        {stopStatus}
        {stopState}
        {onstop}
      />
    </div>
    <Sheet
      open={sheet.picking}
      title={m.area_picker_title()}
      closeLabel={m.close()}
      onclose={() => sheet.close()}
      testid="area-picker"
      wide
    >
      <div class="start-grid">
        <div class="start-areas">
          <p class="hint">{m.area_picker_hint()}</p>
          <ul class="picker">
            {#each mowerMap?.areas ?? [] as area (area.id)}
              {@const order = sheet.picked.indexOf(area.id)}
              <li class="pick-row">
                <div class="pick-line">
                  <button
                    type="button"
                    class="pick"
                    aria-pressed={order >= 0}
                    data-area={area.id}
                    onclick={() => sheet.togglePick(area.id)}
                  >
                    <span class="order mono">{order >= 0 ? order + 1 : ''}</span>
                    <span class="area-name">{areaName(area)}</span>
                    <span class="mono size">
                      {m.detail_area_size({
                        size: formatNumber(polygonArea(area.boundary)),
                      })}
                    </span>
                  </button>
                  {#if canControl}
                    <button
                      type="button"
                      class="rename-open"
                      aria-label={m.area_rename_label({ name: areaName(area) })}
                      aria-expanded={renamingInline === area.id}
                      data-testid="area-rename-open"
                      data-rename-area={area.id}
                      onclick={() => (renamingInline = renamingInline === area.id ? null : area.id)}
                    >
                      <Icon name="edit" size={18} />
                    </button>
                  {/if}
                </div>
                {#if canControl && renamingInline === area.id}
                  <AreaRenameForm
                    mowerId={id}
                    {area}
                    index={areaIndex.get(area.id) ?? null}
                    names={areaNames}
                    compact
                    onsaved={areaNamesSaved}
                    oncancel={() => (renamingInline = null)}
                  />
                {/if}
              </li>
            {/each}
          </ul>
        </div>
        <div class="start-mow">
          {#if offered.length}
            <MowSettings
              {offered}
              spacing={spacingLimits}
              bind:draft={sheet.patternDraft}
              bind:remember={sheet.rememberPattern}
              blade={ms?.blade_height_mm != null ? `${formatNumber(ms.blade_height_mm)} mm` : null}
              speed={ms?.speed_mps != null ? `${formatNumber(ms.speed_mps, 2)} m/s` : null}
              onadjust={adjustSetpoints}
              bind:setpoints={sheet.setpointDraft}
              {bladeRange}
              {speedRange}
            />
          {:else}
            <p class="hint" data-testid="start-no-patterns">{m.start_no_patterns()}</p>
          {/if}
        </div>
      </div>
      {#snippet footer()}
        {@const preview = sheet.preview}
        {@const warning = sheet.startWarning}
        <div class="start-row" data-testid="start-preview" data-state={preview.kind}>
          {#if sheet.jobMinutes !== null}
            <p class="start-duration mono" data-testid="start-duration">
              {m.start_duration({ duration: formatDuration(sheet.jobMinutes) })}
            </p>
          {/if}
          {#if warning}
            <p class="start-rain-warning" role="alert" data-testid="start-rain-warning">
              <Icon name="alert" size={16} />
              {warning.kind === 'wet'
                ? m.start_rain_wet({ time: formatClock(warning.start, locale) })
                : warning.kind === 'now'
                  ? m.start_rain_now({
                      percent: warning.percent === null ? '–' : `${warning.percent} %`,
                    })
                  : m.start_rain_soon({
                      time: formatClock(warning.start, locale),
                      percent: warning.percent === null ? '–' : `${warning.percent} %`,
                    })}
            </p>
          {/if}
          <p
            class="preview-status"
            role="status"
            data-testid="start-preview-status"
            data-state={sheet.previewStale ? 'outdated' : preview.kind}
          >
            {#if preview.kind === 'loading'}
              {m.start_preview_loading()}
            {:else if sheet.previewStale}
              {m.start_preview_outdated()}
            {:else if preview.kind === 'ready'}
              <span class="mono"
                >{m.start_preview_ready({ count: formatNumber(preview.path.points_total) })}</span
              >
            {:else if preview.kind === 'error'}
              <span class="bad">{m.start_preview_failed({ reason: preview.reason })}</span>
            {:else}
              {m.start_preview_hint()}
            {/if}
          </p>
          <div class="start-buttons">
            <Button
              icon="route"
              disabled={sheet.picked.length === 0 || preview.kind === 'loading'}
              pending={preview.kind === 'loading'}
              onclick={() => sheet.computePreview()}
              testid="start-preview-path"
              >{preview.kind === 'ready' && !sheet.previewStale
                ? m.start_preview_again()
                : m.start_preview_path()}</Button
            >
            <div class="start-go">
              <Button
                variant="primary"
                full
                icon="play"
                disabled={sheet.picked.length === 0 || sheet.starting}
                pending={sheet.starting}
                onclick={() => sheet.startClick()}
                testid="area-start"
                >{sheet.starting
                  ? m.start_sending()
                  : warning && !sheet.rainConfirmed
                    ? m.start_rain_anyway()
                    : m.area_picker_start({ count: sheet.picked.length })}</Button
              >
            </div>
          </div>
        </div>
      {/snippet}
    </Sheet>
    <Sheet
      open={sheet.dockChoice}
      title={m.dock_choice_title()}
      closeLabel={m.close()}
      onclose={() => (sheet.dockChoice = false)}
      testid="dock-choice"
    >
      <p class="hint">{m.dock_choice_hint()}</p>
      <div class="dock-choice">
        <Button
          variant="primary"
          full
          icon="dock"
          testid="dock-choice-end"
          onclick={() => sheet.dock(true)}>{m.dock_choice_end()}</Button
        >
        <Button full icon="dock" testid="dock-choice-keep" onclick={() => sheet.dock(false)}
          >{m.dock_choice_keep()}</Button
        >
        <Button variant="ghost" full onclick={() => (sheet.dockChoice = false)}>{m.cancel()}</Button
        >
      </div>
    </Sheet>
    <Sheet
      open={!!renamingArea && !sheet.picking}
      title={m.area_rename_title()}
      closeLabel={m.close()}
      onclose={() => (renaming = null)}
      testid="area-rename-sheet"
    >
      {#if renamingArea && canControl}
        {#key renamingArea.id}
          <AreaRenameForm
            mowerId={id}
            area={renamingArea}
            index={areaIndex.get(renamingArea.id) ?? null}
            names={areaNames}
            onsaved={areaNamesSaved}
            oncancel={() => (renaming = null)}
          />
        {/key}
      {/if}
    </Sheet>
    <AssistSheet
      mowerId={id}
      open={assisting}
      task={assistTask}
      proposalId={assistLink}
      onclose={closeAssist}
    />
  </div>
{:else}
  <p class="pad quiet" role="status"><SlowLoading /></p>
{/if}

<style>
  .pad {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--md-space-4);
    padding: var(--md-space-4) var(--md-gutter);
  }

  .pad-x {
    padding: 0 var(--md-gutter);
  }

  .quiet {
    color: var(--md-color-ink-muted);
  }

  /* Phones: top bar, status and readouts, the map, then the rest (camera first, before the areas); the command bar
     is fixed. The page scrolls as a whole and its padding ends the content where the bar begins
     (--md-commandbar-height is the measured bar). */
  .detail {
    --md-sheet-bottom: var(--md-commandbar-height);
    display: flex;
    flex-direction: column;
    padding-bottom: var(--md-commandbar-height);
  }

  .top {
    order: 0;
  }

  .side {
    display: contents;
  }

  .camera-off {
    margin: 0;
  }

  /* The start sheet: areas left, "Mähen" settings right from 1024 px; the start row is the
     sheet's footer, always visible. */
  .start-grid {
    display: grid;
    gap: var(--md-space-2);
    min-inline-size: 0;
  }

  .start-areas,
  .start-mow {
    min-inline-size: 0;
  }

  @media (min-width: 1024px) {
    .start-grid {
      grid-template-columns: minmax(0, 1fr) minmax(0, 1.15fr);
      gap: var(--md-space-6);
      align-items: start;
    }

    .start-mow {
      padding-block-start: var(--md-space-2);
    }
  }

  /* "Pfad berechnen" and "Starten" with what the mower answered, pinned in the sheet. */
  .start-row {
    display: grid;
    gap: var(--md-space-2);
  }

  .start-buttons {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--md-space-2);
  }

  .start-go {
    flex: 1 1 10rem;
  }

  .start-duration {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  .start-rain-warning {
    display: flex;
    align-items: flex-start;
    gap: var(--md-space-2);
    margin: 0;
    padding: var(--md-space-2) var(--md-space-3);
    border-inline-start: 3px solid var(--md-state-paused);
    background: var(--md-color-surface-sunken);
    color: var(--md-color-ink);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  .preview-status {
    margin: 0;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
    line-height: var(--md-line-16);
  }

  .preview-status .bad {
    color: var(--md-state-error);
  }

  .status {
    order: 2;
    background: var(--md-color-surface);
  }

  /* The map box keeps the phone height; the heat strip and the missed spots follow below it. */
  .plan {
    position: relative;
    order: 3;
    display: flex;
    flex-direction: column;
    border-block: var(--md-hairline) solid var(--md-color-line);
  }

  .map-box {
    position: relative;
    flex: none;
    height: min(60dvh, 520px);
    min-height: 300px;
  }

  .plan > :global(.gaps) {
    max-height: 14rem;
  }

  /* Full-screen map on phones: everything but the command bar (and its stop) is covered. */
  .plan.expanded {
    position: fixed;
    inset: 0 0 var(--md-commandbar-height);
    z-index: 24;
    height: auto;
    min-height: 0;
    border: 0;
  }

  .plan.expanded .map-box {
    flex: 1 1 auto;
    height: auto;
    min-height: 0;
  }

  .plan.expanded > :global(.gaps) {
    max-height: 30dvh;
  }

  .extras {
    order: 4;
    display: flex;
    flex-direction: column;
    background: var(--md-color-surface);
  }

  .plan-empty {
    display: grid;
    place-items: center;
    height: min(60dvh, 520px);
    min-height: 300px;
    padding: var(--md-space-4);
    background-color: var(--md-color-surface-sunken);
    background-image:
      linear-gradient(var(--md-color-line) var(--md-hairline), transparent var(--md-hairline)),
      linear-gradient(
        90deg,
        var(--md-color-line) var(--md-hairline),
        transparent var(--md-hairline)
      );
    background-size: 24px 24px;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    text-align: center;
  }

  .chosen {
    position: absolute;
    top: var(--md-space-3);
    inset-inline-start: var(--md-space-3);
    z-index: 4;
    display: flex;
    align-items: center;
    gap: var(--md-space-2);
    min-height: var(--md-target);
    padding-inline-start: var(--md-space-3);
    border: var(--md-hairline) solid var(--md-color-focus);
    background: var(--md-color-surface);
    color: var(--md-color-focus);
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .chosen-clear {
    display: grid;
    place-items: center;
    width: var(--md-target);
    height: var(--md-target);
    border: 0;
    border-inline-start: var(--md-hairline) solid var(--md-color-line);
    border-radius: var(--md-radius-0);
    background: none;
    color: var(--md-color-ink);
    font-size: var(--md-font-size-20);
    cursor: pointer;
  }

  .chosen-clear:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .alarm {
    padding: 0 var(--md-gutter) var(--md-space-3);
  }

  .assist-entry {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
    padding: var(--md-space-2) var(--md-gutter) var(--md-space-3);
  }

  .state-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--md-space-3);
    padding: var(--md-space-3) var(--md-gutter);
  }

  .age {
    color: var(--md-color-ink-muted);
    font-family: var(--md-font-mono);
    font-size: var(--md-font-size-12);
  }

  .readouts {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--md-space-0_5);
    padding: 0 var(--md-gutter) var(--md-space-4);
  }

  .steppers {
    display: grid;
    gap: var(--md-space-2);
  }

  .hint.inexact {
    margin-top: 0;
    font-size: var(--md-font-size-12, 0.75rem);
    font-style: italic;
  }

  .hint {
    margin-top: var(--md-space-2);
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .mono {
    font-family: var(--md-font-mono);
    font-variant-numeric: tabular-nums;
  }

  .areas,
  .recent,
  .picker {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .areas li {
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .area-row {
    display: flex;
    gap: var(--md-space-2);
    justify-content: space-between;
    min-height: 36px;
    align-items: center;
  }

  .area-label {
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .rename-open {
    display: grid;
    flex: none;
    place-items: center;
    width: var(--md-target);
    height: var(--md-target);
    margin-inline-end: calc(-1 * var(--md-space-2));
    border: 0;
    background: none;
    color: var(--md-color-ink-muted);
    cursor: pointer;
  }

  .rename-open:hover,
  .rename-open[aria-expanded='true'] {
    color: var(--md-color-focus);
  }

  .rename-open:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: -2px;
  }

  .badge {
    display: inline-grid;
    place-items: center;
    min-width: 20px;
    height: 20px;
    margin-inline-end: var(--md-space-2);
    border-radius: var(--md-radius-1);
    background: var(--md-color-focus);
    color: var(--md-color-ink-inverse);
    font-size: var(--md-font-size-12);
  }

  .recent li {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    column-gap: var(--md-space-2);
    padding: var(--md-space-2) 0;
    border-bottom: var(--md-hairline) solid var(--md-color-line);
    font-size: var(--md-font-size-14);
    line-height: var(--md-line-20);
  }

  .recent .outcome {
    font-size: var(--md-font-size-12);
    font-weight: var(--md-weight-semibold);
    letter-spacing: var(--md-label-tracking);
    text-transform: uppercase;
  }

  .recent [data-status='executed'] .outcome {
    color: var(--md-state-mowing);
  }

  .recent [data-status='rejected'] .outcome,
  .recent [data-status='failed'] .outcome,
  .recent [data-status='expired'] .outcome {
    color: var(--md-state-error);
  }

  .recent .when {
    grid-column: 1 / -1;
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-12);
  }

  .next {
    display: grid;
    gap: var(--md-space-1);
    padding: var(--md-space-2) 0;
    color: var(--md-color-ink);
    text-decoration: none;
  }

  .next:focus-visible {
    outline: 2px solid var(--md-color-focus);
    outline-offset: 2px;
  }

  .next > :global(.badge) {
    justify-self: start;
  }

  .next-name {
    font-weight: var(--md-weight-semibold);
  }

  .next:hover .next-name {
    text-decoration: underline;
  }

  .next-when {
    font-size: var(--md-font-size-14);
  }

  .next-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--md-space-2);
  }

  .rtk-bar {
    display: flex;
    height: 8px;
    margin-top: var(--md-space-3);
    background: var(--md-color-surface-sunken);
  }

  .seg {
    flex-basis: 0;
    min-width: 0;
  }

  .seg-fixed {
    background: var(--md-rtk-fixed);
  }

  .seg-float {
    background: var(--md-rtk-float);
  }

  .seg-none {
    background: var(--md-rtk-none);
  }

  .seg-unknown {
    background: var(--md-state-unknown);
  }

  .rtk-readouts {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: var(--md-space-2);
    margin-top: var(--md-space-2);
  }

  .bar {
    position: fixed;
    inset-inline: 0;
    bottom: 0;
    z-index: 26;
  }

  .pick-row {
    border-bottom: var(--md-hairline) solid var(--md-color-line);
  }

  .dock-choice {
    display: grid;
    gap: var(--md-space-2);
    margin-top: var(--md-space-3);
  }

  .pick-line {
    display: flex;
    align-items: center;
  }

  .pick {
    display: grid;
    grid-template-columns: 32px minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--md-space-3);
    width: 100%;
    min-height: 56px;
    padding: 0 var(--md-space-2);
    border: 0;
    background: none;
    color: var(--md-color-ink);
    text-align: start;
    cursor: pointer;
  }

  .pick .order {
    display: grid;
    place-items: center;
    width: 28px;
    height: 28px;
    border: var(--md-border-control) solid var(--md-color-line-strong);
    border-radius: var(--md-radius-1);
  }

  .pick[aria-pressed='true'] .order {
    border-color: var(--md-color-focus);
    background: var(--md-color-focus);
    color: var(--md-color-ink-inverse);
  }

  .pick .size {
    color: var(--md-color-ink-muted);
    font-size: var(--md-font-size-14);
  }

  /* Desktop: the map in the centre, a 360 px instrument column on the right. */
  @media (min-width: 1024px) {
    .detail {
      --md-sheet-bottom: calc(var(--md-commandbar-height) + var(--md-space-3));

      display: grid;
      grid-template-columns: minmax(0, 1fr) var(--md-instrument-column);
      grid-template-rows: auto minmax(0, 1fr);
      grid-template-areas: 'top top' 'plan side';
      height: 100dvh;
      padding-bottom: 0;
      overflow: hidden;
    }

    .top {
      grid-area: top;
    }

    .plan {
      grid-area: plan;
      height: auto;
      min-height: 0;
      border-block: 0;
    }

    .plan .map-box,
    .plan .plan-empty {
      flex: 1 1 auto;
      height: auto;
      min-height: 0;
    }

    .plan > :global(.gaps) {
      max-height: 32%;
    }

    /* The column's scroll box ends where the fixed command bar begins: the margin takes the
       measured bar height out of the grid area, so neither the content nor the scroll bar runs
       underneath the bar. (A padding would keep the scroll box under the bar.) */
    .side {
      grid-area: side;
      display: block;
      min-height: 0;
      margin-bottom: var(--md-commandbar-height);
      padding-bottom: var(--md-space-4);
      scroll-padding-bottom: var(--md-space-4);
      border-inline-start: var(--md-hairline) solid var(--md-color-line);
      background: var(--md-color-surface);
      overflow-y: auto;
      overscroll-behavior: contain;
    }

    .bar {
      inset-inline: auto 0;
      width: var(--md-instrument-column);
      padding: var(--md-space-3) var(--md-space-4) var(--md-space-4);
      border-top: var(--md-hairline) solid var(--md-color-line);
      border-inline-start: var(--md-hairline) solid var(--md-color-line);
      background: var(--md-color-surface);
    }
  }
</style>
