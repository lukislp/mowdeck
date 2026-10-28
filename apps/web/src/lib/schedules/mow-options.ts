/**
 * The start settings of a schedule (the Scheduler's `SchedulePattern` plus blade height and
 * speed; ADR 0024 amendment 2026-10-24): what the editor offers for a target, the draft it
 * edits, the request keys it sends and the compact summary of the list.
 *
 * What is offered comes from the capabilities of the target's mowers only. One mower: what it
 * declares. A group: what every member declares (the intersection), never more; a member whose
 * capabilities are unknown offers nothing, so the group offers nothing either. The start
 * progress is never offered: it skips the first part of one job's route, which makes no sense
 * for every run of a schedule. A value left empty (null) is not sent: the run takes the
 * mower's saved mow defaults, else the mower's own setting. Pure, unit tested
 * (`mow-options.test.ts`).
 */
import { formatNumber, obstacleLabel, orderLabel, patternLabel } from '#lib/i18n.js';
import {
  NO_OPTIONS,
  OBSTACLE_MODES,
  PATH_ORDERS,
  jobOptionsSpec,
  type JobOptionsDraft,
  type JobOptionsSpec,
  type OptionRange,
} from '#lib/mower/job-options.js';
import { bladeRange, speedRange, type Range } from '#lib/mower/limits.js';
import {
  ANGLE_MAX,
  BORDER_LAPS_MAX,
  PATTERN_NAMES,
  angleApplies,
  offeredPatterns,
  spacingRange,
  type LoosePatternDraft,
  type PatternName,
  type SpacingRange,
} from '#lib/mower/mow-pattern.js';
import type { SetpointDraft } from '#lib/mower/setpoints.js';
import { m } from '#lib/paraglide/messages.js';

import type { Schedule, ScheduleIn } from './types';

/** The capabilities fields the editor reads (the telemetry API's `MowerCapabilities`). */
export interface CapsLike {
  model?: string | null;
  native_patterns?: readonly string[] | null;
  path_spacing_range_mm?: readonly number[] | null;
  blade_height_range_mm?: readonly number[] | null;
  min_speed_mps?: number | null;
  max_speed_mps?: number | null;
  start_options?: unknown;
}

/** What the editor offers for a target. */
export interface MowOffer {
  offered: PatternName[];
  spacing: SpacingRange | null;
  /** The job options without the start progress; null when none is offered. */
  options: JobOptionsSpec | null;
  blade: Range | null;
  speed: Range | null;
}

export const NO_OFFER: MowOffer = {
  offered: [],
  spacing: null,
  options: null,
  blade: null,
  speed: null,
};

function withoutProgress(spec: JobOptionsSpec | null): JobOptionsSpec | null {
  if (!spec) return null;
  const options = { ...spec, progress: null };
  const any = options.detection.length || options.orders.length || options.obstacleLaps;
  return any ? options : null;
}

/** What one mower offers a schedule. */
export function offerOf(caps: CapsLike | null | undefined): MowOffer {
  if (!caps) return NO_OFFER;
  return {
    offered: offeredPatterns(caps.native_patterns),
    spacing: spacingRange(caps.path_spacing_range_mm),
    options: withoutProgress(jobOptionsSpec(caps.start_options)),
    blade: bladeRange({
      blade_height_range_mm: caps.blade_height_range_mm ? [...caps.blade_height_range_mm] : null,
    }),
    speed: speedRange({ min_speed_mps: caps.min_speed_mps, max_speed_mps: caps.max_speed_mps }),
  };
}

function intersect<T extends readonly [number, number]>(ranges: readonly (T | null)[]): T | null {
  if (!ranges.length || ranges.some((r) => r === null)) return null;
  const low = Math.max(...ranges.map((r) => r![0]));
  const high = Math.min(...ranges.map((r) => r![1]));
  return low <= high ? ([low, high] as unknown as T) : null;
}

function common<T>(lists: readonly (readonly T[])[]): T[] {
  const [first, ...rest] = lists;
  return (first ?? []).filter((item) => rest.every((list) => list.includes(item)));
}

/**
 * What every one of the mowers offers (a group schedule): the patterns, modes and orders all
 * of them offer and the overlap of their ranges. No mowers, or one with unknown capabilities:
 * nothing.
 */
export function commonOffer(capabilities: readonly (CapsLike | null | undefined)[]): MowOffer {
  if (!capabilities.length || capabilities.some((caps) => !caps)) return NO_OFFER;
  const offers = capabilities.map(offerOf);
  const specs = offers.map((o) => o.options);
  let options: JobOptionsSpec | null = null;
  if (specs.every((spec) => spec !== null)) {
    options = withoutProgress({
      detection: common(specs.map((s) => s!.detection)),
      obstacleLaps: intersect<OptionRange>(specs.map((s) => s!.obstacleLaps)),
      orders: common(specs.map((s) => s!.orders)),
      progress: null,
    });
  }
  return {
    offered: common(offers.map((o) => o.offered)),
    spacing: intersect<SpacingRange>(offers.map((o) => o.spacing)),
    options,
    blade: intersect<Range>(offers.map((o) => o.blade)),
    speed: intersect<Range>(offers.map((o) => o.speed)),
  };
}

/** Whether the offer has anything to set at all. */
export function offersAnything(offer: MowOffer): boolean {
  return offer.offered.length > 0 || offer.blade !== null || offer.speed !== null;
}

/** The editor's draft: the pattern keys (null: unset) and the setpoints. */
export interface MowDraft {
  pattern: LoosePatternDraft & { options: JobOptionsDraft };
  setpoints: SetpointDraft;
}

export function emptyMow(): MowDraft {
  return {
    pattern: { name: null, angle: null, laps: null, spacingMm: null, options: { ...NO_OPTIONS } },
    setpoints: { bladeMm: null, speedMps: null },
  };
}

type StoredPattern = NonNullable<Schedule['pattern']>;

/** The start settings of a stored schedule (or of a request body). */
export interface StoredSettings {
  pattern?: Partial<StoredPattern> | null;
  blade_height_mm?: number | null;
  speed_mps?: number | null;
}

function finite(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function patternName(value: unknown): PatternName | null {
  return (PATTERN_NAMES as readonly unknown[]).includes(value) ? (value as PatternName) : null;
}

/** The draft of a stored schedule. */
export function mowDraftOf(schedule: StoredSettings): MowDraft {
  const p: Partial<StoredPattern> = schedule.pattern ?? {};
  const draft = emptyMow();
  draft.pattern = {
    name: patternName(p.name),
    angle: finite(p.angle_deg) ? p.angle_deg : null,
    laps: finite(p.border_laps) ? p.border_laps : null,
    spacingMm: finite(p.spacing_m) ? Math.round(p.spacing_m * 1000) : null,
    options: {
      detection: (OBSTACLE_MODES as readonly unknown[]).includes(p.obstacle_detection)
        ? (p.obstacle_detection as JobOptionsDraft['detection'])
        : null,
      obstacleLaps: finite(p.obstacle_laps) ? p.obstacle_laps : null,
      order: (PATH_ORDERS as readonly unknown[]).includes(p.order)
        ? (p.order as JobOptionsDraft['order'])
        : null,
      progress: 0,
    },
  };
  draft.setpoints = {
    bladeMm: finite(schedule.blade_height_mm) ? schedule.blade_height_mm : null,
    speedMps: finite(schedule.speed_mps) ? schedule.speed_mps : null,
  };
  return draft;
}

export type MowBody = Pick<ScheduleIn, 'pattern' | 'blade_height_mm' | 'speed_mps'>;

/** The request keys of the draft: only what is set; no pattern key set sends `null`. */
export function mowBody(draft: MowDraft): MowBody {
  const p = draft.pattern;
  const o = p.options;
  const pattern: NonNullable<ScheduleIn['pattern']> = {};
  if (p.name !== null) pattern.name = p.name;
  if (p.angle !== null) pattern.angle_deg = p.angle;
  if (p.laps !== null) pattern.border_laps = p.laps;
  if (p.spacingMm !== null) pattern.spacing_m = p.spacingMm / 1000;
  if (o.detection !== null) pattern.obstacle_detection = o.detection;
  if (o.obstacleLaps !== null) pattern.obstacle_laps = o.obstacleLaps;
  if (o.order !== null) pattern.order = o.order;
  return {
    pattern: Object.keys(pattern).length ? pattern : null,
    blade_height_mm: draft.setpoints.bladeMm,
    speed_mps: draft.setpoints.speedMps,
  };
}

function inRange(value: number | null, range: readonly [number, number] | null): number | null {
  if (value === null || range === null) return null;
  return value >= range[0] && value <= range[1] ? value : null;
}

/**
 * The draft without what the offer does not contain (another target): a value the new target
 * does not offer is cleared, never moved into its range. The angle and border laps stay while
 * the target runs any native pattern.
 */
export function fitMowDraft(draft: MowDraft, offer: MowOffer): MowDraft {
  const p = draft.pattern;
  const o = p.options;
  const spec = offer.options;
  const native = offer.offered.length > 0;
  return {
    pattern: {
      name: p.name !== null && offer.offered.includes(p.name) ? p.name : null,
      angle: native ? p.angle : null,
      laps: native ? p.laps : null,
      spacingMm: native ? inRange(p.spacingMm, offer.spacing) : null,
      options: {
        detection:
          o.detection !== null && spec?.detection.includes(o.detection) ? o.detection : null,
        obstacleLaps: inRange(o.obstacleLaps, spec?.obstacleLaps ?? null),
        order: o.order !== null && spec?.orders.includes(o.order) ? o.order : null,
        progress: 0,
      },
    },
    setpoints: {
      bladeMm: inRange(draft.setpoints.bladeMm, offer.blade),
      speedMps: inRange(draft.setpoints.speedMps, offer.speed),
    },
  };
}

const MM = 'mm';
const MPS = 'm/s';
const CM = 'cm';

/**
 * The settings a schedule sets, as short parts for the list and the editor ("Diagonal",
 * "45°", "2 Randrunden", "Empfindlich", "Zuerst Rand", "Sperrzone 1×", "40 mm", "0,6 m/s");
 * empty when it sets nothing (the mow defaults, else the mower's own). With a rotation list the
 * angles replace the angle ("0°/90°"); with step rotation the angle is followed by the step
 * ("30° · +45°/Lauf").
 */
export function mowSummary(
  schedule: StoredSettings & {
    rotation_angles?: readonly number[];
    rotation_step_deg?: number | null;
  },
): string[] {
  const p: Partial<StoredPattern> = schedule.pattern ?? {};
  const parts: string[] = [];
  const name = patternName(p.name);
  if (name) parts.push(patternLabel(name));
  const rotation = schedule.rotation_angles ?? [];
  const directed = !name || angleApplies(name);
  if (rotation.length) {
    parts.push(rotation.map((a) => `${formatNumber(((a % 180) + 180) % 180)}°`).join('/'));
  } else if (finite(p.angle_deg) && directed) {
    parts.push(`${formatNumber(Math.min(ANGLE_MAX, p.angle_deg))}°`);
  }
  const step = schedule.rotation_step_deg;
  if (!rotation.length && finite(step) && directed) {
    parts.push(m.sched_rotation_step_summary({ step: formatNumber(step, step % 1 ? 1 : 0) }));
  }
  if (finite(p.border_laps)) {
    const laps = Math.min(BORDER_LAPS_MAX, p.border_laps);
    parts.push(
      laps <= 0
        ? m.start_laps_none()
        : laps === 1
          ? m.start_laps_one()
          : m.start_laps_many({ count: laps }),
    );
  }
  if (finite(p.spacing_m)) parts.push(`${formatNumber(p.spacing_m * 100)} ${CM}`);
  if ((OBSTACLE_MODES as readonly unknown[]).includes(p.obstacle_detection)) {
    parts.push(obstacleLabel(p.obstacle_detection as (typeof OBSTACLE_MODES)[number]));
  }
  if ((PATH_ORDERS as readonly unknown[]).includes(p.order)) {
    parts.push(orderLabel(p.order as (typeof PATH_ORDERS)[number]));
  }
  if (finite(p.obstacle_laps)) parts.push(m.sched_mow_nogo_laps({ count: p.obstacle_laps }));
  if (finite(schedule.blade_height_mm)) {
    parts.push(`${formatNumber(schedule.blade_height_mm)} ${MM}`);
  }
  if (finite(schedule.speed_mps)) parts.push(`${formatNumber(schedule.speed_mps, 2)} ${MPS}`);
  return parts;
}
