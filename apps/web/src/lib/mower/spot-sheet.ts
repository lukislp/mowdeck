/**
 * Pure rules of the spot sheet (the flow itself is `spot-sheet.svelte.ts`): the limits of the
 * spot, the Planner request for a spot and its pattern, the preview as a mow path for the map,
 * the `spot_mow` command, and the start conditions (green or red rows). Unit tested in
 * `spot-sheet.test.ts`.
 */
import {
  containingArea,
  distanceToSpot,
  fromWgs84,
  spotPolygon,
  thinRing,
  toWgs84,
  type GeoReference,
  type LocalPoint,
  type MapSpot,
} from '@mowdeck/ui';

import type { components as Planner } from '$lib/generated/planner';
import type { components as Telemetry } from '$lib/generated/telemetry';
import { m } from '$lib/paraglide/messages';

import type { MowPath } from './mow-path';
import { isOnline } from './presence';
import { isJobActivity } from './mow-path';
import type { SheetArea } from './start-sheet';

export type PlanRequest = Planner['schemas']['PlanRequestIn'];
export type PreviewOut = Planner['schemas']['PreviewOut'];
export type PlanOut = Planner['schemas']['PlanOut'];
export type SpotRunStatus = Telemetry['schemas']['SpotRunStatus'];
export type SpotRunState = SpotRunStatus['state'];

// --- limits ------------------------------------------------------------------------------------

export const SPOT_RADIUS_DEFAULT_M = 1.5;
export const SPOT_RADIUS_MIN_M = 0.5;
export const SPOT_RADIUS_MAX_M = 3;
export const SPOT_RADIUS_STEP_M = 0.1;
/** Most corners a drawn spot takes on the map. */
export const SPOT_CORNERS_MAX = 8;
/** Most corners the audit record of a run keeps (`SpotPolygon`). */
export const SPOT_AUDIT_CORNERS_MAX = 64;
/** Speed of a run MowDeck drives itself: 0.1 to 0.3 m/s, hard cap 0.3 (`MAX_SPOT_MOW_SPEED_MPS`). */
export const SPOT_SPEED_MIN_MPS = 0.1;
export const SPOT_SPEED_MAX_MPS = 0.3;
export const SPOT_SPEED_DEFAULT_MPS = 0.2;
export const SPOT_SPEED_STEP_MPS = 0.05;
/** The path spacing opens with the cutting width minus this overlap. */
export const SPOT_OVERLAP_MM = 50;
export const SPOT_SPACING_MIN_MM = 50;
export const SPOT_SPACING_STEP_MM = 10;
/** Cutting width assumed when the mower reports none (the Planner's own default). */
export const FALLBACK_CUTTING_WIDTH_MM = 400;
/** The Planner keeps this far from the area edge and the no-go zones (design, section 2). */
export const SPOT_SAFETY_MARGIN_M = 0.4;
/** The mower must stand within this distance of the spot. */
export const SPOT_NEAR_M = 10;
/**
 * The state must be younger than this. A standing mower on a cloud-first connection reports
 * about every 20 s, and the bridge attaches when the run opens its drive session; the gateway's
 * run then pauses until positions are live (pose at most 2.5 s old, waiting up to 30 s).
 */
export const SPOT_FRESH_MS = 60_000;
export const SPOT_BATTERY_MIN_PERCENT = 20;
/** After a start, the run bar says "starting" this long at most until the run shows up. */
export const SPOT_LAUNCH_WAIT_MS = 20_000;
/** The preview is asked for this long after the last change. */
export const SPOT_PREVIEW_DEBOUNCE_MS = 400;

export type SpotPattern = 'spiral' | 'stripes';

/** What the sheet sends besides the spot. */
export interface SpotSettings {
  pattern: SpotPattern;
  /** Stripe angle in degrees (stripes only). */
  angleDeg: number;
  spacingMm: number;
  speedMps: number;
  /** The blades switch is on (needs `confirmBlades` and the capability to count). */
  blades: boolean;
  confirmBlades: boolean;
}

export function defaultSpacingMm(cuttingWidthMm: number | null | undefined): number {
  return clampSpacingMm(widthMm(cuttingWidthMm) - SPOT_OVERLAP_MM, cuttingWidthMm);
}

function widthMm(cuttingWidthMm: number | null | undefined): number {
  return cuttingWidthMm != null && cuttingWidthMm > 0 ? cuttingWidthMm : FALLBACK_CUTTING_WIDTH_MM;
}

/** The spacing stays between the minimum and the cutting width (no gap between paths). */
export function clampSpacingMm(value: number, cuttingWidthMm: number | null | undefined): number {
  const max = widthMm(cuttingWidthMm);
  const rounded = Math.round(value / SPOT_SPACING_STEP_MM) * SPOT_SPACING_STEP_MM;
  return Math.min(max, Math.max(SPOT_SPACING_MIN_MM, rounded));
}

export function clampSpeedMps(value: number): number {
  if (!Number.isFinite(value)) return SPOT_SPEED_DEFAULT_MPS;
  const stepped = Math.round(value / SPOT_SPEED_STEP_MPS) * SPOT_SPEED_STEP_MPS;
  return (
    Math.round(Math.min(SPOT_SPEED_MAX_MPS, Math.max(SPOT_SPEED_MIN_MPS, stepped)) * 100) / 100
  );
}

export function clampRadiusM(value: number): number {
  if (!Number.isFinite(value)) return SPOT_RADIUS_DEFAULT_M;
  const stepped = Math.round(value / SPOT_RADIUS_STEP_M) * SPOT_RADIUS_STEP_M;
  return Math.round(Math.min(SPOT_RADIUS_MAX_M, Math.max(SPOT_RADIUS_MIN_M, stepped)) * 10) / 10;
}

export function initialSettings(cuttingWidthMm: number | null | undefined): SpotSettings {
  return {
    pattern: 'spiral',
    angleDeg: 0,
    spacingMm: defaultSpacingMm(cuttingWidthMm),
    speedMps: SPOT_SPEED_DEFAULT_MPS,
    blades: false,
    confirmBlades: false,
  };
}

/** The blades run only when the mower allows them while driving, the switch is on and confirmed. */
export function bladesRequested(settings: SpotSettings, allowed: boolean): boolean {
  return allowed && settings.blades && settings.confirmBlades;
}

// --- the Planner request ---------------------------------------------------------------------

/** The pattern chain of the sheet's settings (the Planner's keys and settings). */
export function spotChain(settings: SpotSettings): NonNullable<PlanRequest['chain']> {
  if (settings.pattern === 'stripes') {
    return [{ pattern: 'stripes', settings: { angle_deg: settings.angleDeg } }];
  }
  return [{ pattern: 'spiral', settings: { direction: 'inward' } }];
}

/**
 * `POST /planner/plans/preview` and `/plans` take the same body: the spot as a local polygon
 * inside `areaId` (the Planner clips to that area), the pattern chain and the mower data the
 * sheet decides: the speed for mowing and transfers, the spacing as cutting width minus overlap,
 * and the safety margin. Null without a complete spot inside one area.
 */
export function planRequest(input: {
  mowerId: string;
  spot: MapSpot | null;
  areaId: string | null;
  settings: SpotSettings;
  cuttingWidthMm: number | null | undefined;
}): PlanRequest | null {
  const polygon = spotPolygon(input.spot);
  if (polygon.length < 3 || !input.areaId) return null;
  const cuttingWidthM = widthMm(input.cuttingWidthMm) / 1000;
  const spacingM = clampSpacingMm(input.settings.spacingMm, input.cuttingWidthMm) / 1000;
  const speed = clampSpeedMps(input.settings.speedMps);
  return {
    mower_id: input.mowerId,
    area: { area_id: input.areaId, polygon: polygon.map((p) => [p[0], p[1]]) },
    chain: spotChain(input.settings),
    mower: {
      cutting_width_m: round3(cuttingWidthM),
      overlap_m: round3(Math.max(0, cuttingWidthM - spacingM)),
      max_speed_mps: speed,
      transfer_speed_mps: speed,
      safety_margin_m: SPOT_SAFETY_MARGIN_M,
    },
    wgs84: false,
  };
}

function round3(value: number): number {
  return Math.round(value * 1000) / 1000;
}

/** What a preview was computed for (the whole request, keys sorted). */
export function spotPreviewKey(request: PlanRequest | null): string {
  return request ? stableJson(request) : '';
}

function stableJson(value: unknown): string {
  return JSON.stringify(value, (_key, v: unknown) =>
    v && typeof v === 'object' && !Array.isArray(v)
      ? Object.fromEntries(
          Object.entries(v as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)),
        )
      : v,
  );
}

// --- the preview as a mow path ---------------------------------------------------------------

interface PreviewFeature {
  geometry?: { type?: string; coordinates?: unknown };
  properties?: { kind?: string };
}

/**
 * The Planner's preview (a FeatureCollection of LineStrings in local metres) as the map's mow
 * path: one polyline per segment, `mow` and `turn` as mowing, `transfer` dotted, without
 * progress. The plan hash is the path hash, so a start can seed the map with it.
 */
export function previewMowPath(preview: PreviewOut, plannedAt: string): MowPath {
  const features = Array.isArray((preview.preview as { features?: unknown }).features)
    ? ((preview.preview as { features: PreviewFeature[] }).features ?? [])
    : [];
  const polylines: number[][][] = [];
  const kinds: string[] = [];
  for (const feature of features) {
    if (feature.geometry?.type !== 'LineString') continue;
    const coords = feature.geometry.coordinates;
    if (!Array.isArray(coords)) continue;
    const points = coords
      .filter((c): c is number[] => Array.isArray(c) && c.length >= 2)
      .map((c) => [Number(c[0]), Number(c[1])]);
    if (points.length < 2) continue;
    polylines.push(points);
    kinds.push(feature.properties?.kind === 'transfer' ? 'transfer' : 'mow');
  }
  return {
    path_hash: preview.plan_hash,
    polylines,
    kinds,
    points_total: polylines.reduce((sum, line) => sum + line.length, 0),
    progress: null,
    planned_at: plannedAt,
  };
}

// --- the command -----------------------------------------------------------------------------

export type SpotTarget =
  { center_wgs84: [number, number]; radius_m: number } | { polygon_wgs84: [number, number][] };

/** The spot as the user chose it, in WGS84 for the audit record (at most 64 corners). */
export function spotTarget(spot: MapSpot, reference: GeoReference): SpotTarget {
  if (spot.kind === 'circle') {
    return {
      center_wgs84: toWgs84(reference, spot.center[0], spot.center[1]),
      radius_m: spot.radius_m,
    };
  }
  return {
    polygon_wgs84: thinRing(spot.points, SPOT_AUDIT_CORNERS_MAX).map((p) =>
      toWgs84(reference, p[0], p[1]),
    ),
  };
}

/** A type alias, not an interface: the command store takes any record with a `kind`. */
export type SpotMowCommand = {
  kind: 'spot_mow';
  plan: PlanOut['spot_plan'];
  blades: boolean;
  confirm_blades: boolean;
  speed_mps: number;
  spot: SpotTarget | null;
};

export function spotCommand(input: {
  plan: PlanOut;
  settings: SpotSettings;
  bladesAllowed: boolean;
  target: SpotTarget | null;
}): SpotMowCommand {
  const blades = bladesRequested(input.settings, input.bladesAllowed);
  return {
    kind: 'spot_mow',
    plan: input.plan.spot_plan,
    blades,
    confirm_blades: blades,
    speed_mps: clampSpeedMps(input.settings.speedMps),
    spot: input.target,
  };
}

// --- the run ---------------------------------------------------------------------------------

const FINAL_RUN_STATES: ReadonlySet<string> = new Set(['done', 'aborted', 'failed']);

/** True while the gateway drives the run (preparing, mowing, paused for RTK, stopping). */
export function isSpotRunActive(run: SpotRunStatus | null | undefined): boolean {
  return !!run && !FINAL_RUN_STATES.has(run.state);
}

export function isSpotRunFinal(run: SpotRunStatus | null | undefined): boolean {
  return !!run && FINAL_RUN_STATES.has(run.state);
}

/** The state of a run in words. */
export function spotRunLabel(state: SpotRunState): string {
  switch (state) {
    case 'preparing':
      return m.spot_run_preparing();
    case 'spot_mowing':
      return m.spot_run_mowing();
    case 'paused_rtk':
      return m.spot_run_paused_rtk();
    case 'stopping':
      return m.spot_run_stopping();
    case 'done':
      return m.spot_run_done();
    case 'aborted':
      return m.spot_run_aborted();
    case 'failed':
      return m.spot_run_failed();
  }
}

/** Why a run ended or paused, in words (an unknown reason is shown as it came). */
export function spotReasonLabel(reason: string | null | undefined): string | null {
  switch (reason) {
    case null:
    case undefined:
    case '':
      return null;
    case 'rtk_lost':
      return m.spot_reason_rtk_lost();
    case 'geofence':
      return m.spot_reason_geofence();
    case 'link_lost':
      return m.spot_reason_link_lost();
    case 'stopped_by_user':
      return m.spot_reason_stopped_by_user();
    case 'emergency_stop':
      return m.spot_reason_emergency_stop();
    case 'sensor_stop':
      return m.spot_reason_sensor_stop();
    case 'timeout':
      return m.spot_reason_timeout();
    case 'heading_mismatch':
      return m.spot_reason_heading_mismatch();
    default:
      return reason;
  }
}

// --- start conditions ------------------------------------------------------------------------

export type SpotConditionId =
  'spot' | 'link' | 'rtk' | 'near' | 'idle' | 'free' | 'battery' | 'fresh';

export interface SpotCondition {
  id: SpotConditionId;
  ok: boolean;
}

/** The parts of a mower state the conditions read (`MowerState` of Telemetry). */
export interface ConditionState {
  connection: string;
  links?: Record<string, { connected: boolean } | undefined> | null;
  rtk_fix: string;
  position_source: string;
  position?: { lat: number; lon: number } | null;
  activity: string;
  battery_percent: number;
  observed_at: string;
  /** The gateway's link heartbeat (ADR 0040); absent on states from before it. */
  heartbeat_at?: string | null;
  /** False once the gateway reported the link lost. */
  connected?: boolean | null;
  spot_run?: SpotRunStatus | null;
}

export interface SpotConditionInput {
  spot: MapSpot | null;
  areaId: string | null;
  areas: readonly SheetArea[];
  reference: GeoReference | null;
  state: ConditionState | null;
  /** `capabilities.links_supported` of the mower. */
  linksSupported: readonly string[] | null | undefined;
  /** This browser drives the mower with the joystick right now. */
  driveSession: boolean;
  /** Epoch ms of now. */
  now: number;
}

/**
 * The drive link is up: the bridge link when the mower reports one (`links.ble_bridge`), the
 * connection itself when it is the bridge, else the primary connection carries the driving
 * unless the connection expects a bridge for it (`links_supported` lists `ble_bridge` but none
 * is reported yet).
 */
export function driveLinkUp(
  state: Pick<ConditionState, 'connection' | 'links'> | null,
  linksSupported: readonly string[] | null | undefined,
): boolean {
  if (!state) return false;
  const bridge = state.links?.ble_bridge;
  if (bridge) return bridge.connected === true;
  if (state.connection === 'ble_bridge') return true;
  return !(linksSupported ?? []).includes('ble_bridge');
}

/** The mower's position in local metres, or null without a position or reference. */
export function mowerLocal(
  state: Pick<ConditionState, 'position'> | null,
  reference: GeoReference | null,
): LocalPoint | null {
  if (!state?.position || !reference) return null;
  return fromWgs84(reference, state.position.lat, state.position.lon);
}

export function spotConditions(input: SpotConditionInput): SpotCondition[] {
  const s = input.state;
  const area = input.areaId ? (input.areas.find((a) => a.id === input.areaId) ?? null) : null;
  const here = mowerLocal(s, input.reference);
  const near =
    !!area &&
    !!here &&
    containingArea({ kind: 'polygon', points: [here, here, here] }, [area]) === area.id &&
    distanceToSpot(here, input.spot) <= SPOT_NEAR_M;
  const age = s ? input.now - Date.parse(s.observed_at) : Number.POSITIVE_INFINITY;
  return [
    { id: 'spot', ok: !!area },
    { id: 'link', ok: driveLinkUp(s, input.linksSupported) },
    { id: 'rtk', ok: !!s && s.rtk_fix === 'fixed' && s.position_source === 'rtk' },
    { id: 'near', ok: near },
    { id: 'idle', ok: s?.activity === 'idle' },
    {
      id: 'free',
      ok: !!s && !isJobActivity(s.activity) && !input.driveSession && !isSpotRunActive(s.spot_run),
    },
    { id: 'battery', ok: !!s && s.battery_percent > SPOT_BATTERY_MIN_PERCENT },
    // Online by the link heartbeat (presence.ts) and values younger than SPOT_FRESH_MS.
    {
      id: 'fresh',
      ok: !!s && isOnline(s, input.now) && Number.isFinite(age) && age <= SPOT_FRESH_MS,
    },
  ];
}

export function allGreen(conditions: readonly SpotCondition[]): boolean {
  return conditions.every((c) => c.ok);
}

/** The words of a condition row. */
export function conditionLabel(id: SpotConditionId): string {
  switch (id) {
    case 'spot':
      return m.spot_cond_spot();
    case 'link':
      return m.spot_cond_link();
    case 'rtk':
      return m.spot_cond_rtk();
    case 'near':
      return m.spot_cond_near();
    case 'idle':
      return m.spot_cond_idle();
    case 'free':
      return m.spot_cond_free();
    case 'battery':
      return m.spot_cond_battery();
    case 'fresh':
      return m.spot_cond_fresh();
  }
}

/** The polygon of a missed spot as a drawn spot. */
export function spotFromGap(gap: { polygon: readonly (readonly number[])[] }): MapSpot {
  const points = gap.polygon
    .filter((p) => p.length >= 2)
    .map((p) => [p[0] as number, p[1] as number] as LocalPoint);
  return { kind: 'polygon', points };
}
