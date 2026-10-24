/**
 * The pattern settings of a native start (`StartMow.pattern`, the contracts' `MowPattern`):
 * which patterns the start sheet offers, what it pre-fills (the mower's saved mow defaults,
 * else stripes at 0° with one border lap) and what it sends, including the job options a mower
 * declares (`./job-options`). Pure, unit tested (`mow-pattern.test.ts`); the labels live in
 * `#lib/i18n` (`patternSummary`).
 */
import {
  OBSTACLE_MODES,
  PATH_ORDERS,
  initialOptions,
  optionKeys,
  type JobOptionKeys,
  type JobOptionsDraft,
  type JobOptionsSpec,
} from './job-options';

/** Pattern names in the order the start sheet shows them. */
export const PATTERN_NAMES = [
  'stripes',
  'diagonal',
  'checkerboard',
  'edge_only',
  'random_angle',
] as const;
export type PatternName = (typeof PATTERN_NAMES)[number];

export const ANGLE_MAX = 180;
export const ANGLE_STEP = 15;
export const BORDER_LAPS_MAX = 4;
/** The adapter's spacing when a start names none (`DEFAULT_SPACING_M`), in millimetres. */
export const DEFAULT_SPACING_MM = 250;
export const SPACING_STEP_MM = 10;

/**
 * `MowPattern` as JSON. `spacing_m` and the job options are absent (or null) for the mower's
 * own setting.
 */
export interface MowPattern extends JobOptionKeys {
  name: PatternName;
  angle_deg: number;
  border_laps: number;
  spacing_m?: number | null;
}

/** What the sheet edits; `spacingMm` null keeps the mower's own spacing. */
export interface PatternDraft {
  name: PatternName;
  angle: number;
  laps: number;
  spacingMm: number | null;
  /** The job options, when the mower offers any (`./job-options`). */
  options?: JobOptionsDraft;
}

export type SpacingRange = readonly [min: number, max: number];

/**
 * A draft whose pattern keys may be unset: a schedule's settings, where null means "the mower's
 * saved mow defaults, else its own setting". The start sheet's {@link PatternDraft} is one with
 * every key set; the shared `MowOptions` component edits both.
 */
export interface LoosePatternDraft {
  name: PatternName | null;
  angle: number | null;
  laps: number | null;
  spacingMm: number | null;
  options?: JobOptionsDraft;
}

/** Fallback without saved defaults: stripes, 0°, one border lap (the edges get mowed). */
export const FALLBACK: PatternDraft = { name: 'stripes', angle: 0, laps: 1, spacingMm: null };

function isPatternName(value: unknown): value is PatternName {
  return typeof value === 'string' && (PATTERN_NAMES as readonly string[]).includes(value);
}

function finite(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/** The patterns the mower runs natively, in sheet order (empty: the sheet sends none). */
export function offeredPatterns(native: readonly string[] | null | undefined): PatternName[] {
  const set = new Set(native ?? []);
  return PATTERN_NAMES.filter((name) => set.has(name));
}

/** The path spacing range in mm from the capabilities, or null (the control stays hidden). */
export function spacingRange(range: readonly number[] | null | undefined): SpacingRange | null {
  if (!range || range.length !== 2) return null;
  const [min, max] = range;
  if (!finite(min) || !finite(max) || min <= 0 || min > max) return null;
  return [min, max];
}

/** The angle only matters for patterns with stripes at a chosen angle. */
export function angleApplies(name: PatternName): boolean {
  return name !== 'edge_only' && name !== 'random_angle';
}

/** Edge only runs at least one lap (the adapter raises 0 to 1 anyway). */
export function minLaps(name: PatternName): number {
  return name === 'edge_only' ? 1 : 0;
}

/**
 * The settings the sheet opens with: the saved defaults when they are readable, else
 * {@link FALLBACK}; a pattern the mower cannot run becomes the first one it can (stripes when
 * offered), values are clamped to the ranges the sheet offers.
 */
export function initialDraft(
  saved: Partial<MowPattern> | null | undefined,
  offered: readonly PatternName[],
  spacing: SpacingRange | null,
  options: JobOptionsSpec | null = null,
): PatternDraft {
  const wanted = isPatternName(saved?.name) ? saved.name : FALLBACK.name;
  const name = offered.includes(wanted)
    ? wanted
    : offered.includes('stripes')
      ? 'stripes'
      : (offered[0] ?? wanted);
  const angle = finite(saved?.angle_deg)
    ? clamp(Math.round(saved.angle_deg), 0, ANGLE_MAX)
    : FALLBACK.angle;
  const laps = finite(saved?.border_laps)
    ? clamp(Math.round(saved.border_laps), minLaps(name), BORDER_LAPS_MAX)
    : Math.max(FALLBACK.laps, minLaps(name));
  const spacingMm =
    spacing && finite(saved?.spacing_m)
      ? clamp(Math.round(saved.spacing_m * 1000), spacing[0], spacing[1])
      : null;
  const draft: PatternDraft = { name, angle, laps, spacingMm };
  if (options) draft.options = initialOptions(saved, options);
  return draft;
}

/** The draft as `StartMow.pattern` (and as the body of `PUT .../mow-defaults`). */
export function toPattern(draft: PatternDraft): MowPattern {
  const pattern: MowPattern = {
    name: draft.name,
    angle_deg: draft.angle,
    border_laps: Math.max(draft.laps, minLaps(draft.name)),
  };
  if (draft.spacingMm !== null) pattern.spacing_m = draft.spacingMm / 1000;
  return { ...pattern, ...optionKeys(draft.options) };
}

/**
 * The pattern as mow defaults: without the start progress, which belongs to one start (the
 * sheet always opens at 0 %).
 */
export function defaultsOf(pattern: MowPattern): MowPattern {
  const defaults = { ...pattern };
  delete defaults.start_progress_pct;
  return defaults;
}

/** Whether two patterns say the same (a missing spacing equals null). */
export function samePattern(a: MowPattern | null | undefined, b: MowPattern | null | undefined) {
  if (!a || !b) return !a && !b;
  return (
    a.name === b.name &&
    a.angle_deg === b.angle_deg &&
    a.border_laps === b.border_laps &&
    (a.spacing_m ?? null) === (b.spacing_m ?? null) &&
    (a.obstacle_detection ?? null) === (b.obstacle_detection ?? null) &&
    (a.obstacle_laps ?? null) === (b.obstacle_laps ?? null) &&
    (a.order ?? null) === (b.order ?? null) &&
    (a.start_progress_pct || null) === (b.start_progress_pct || null)
  );
}

/** One angle step; from an unset angle the first step goes to 0° (down) or 15° (up). */
export function stepAngle(current: number | null, direction: 1 | -1): number {
  if (current === null) return direction > 0 ? ANGLE_STEP : 0;
  return clamp(current + direction * ANGLE_STEP, 0, ANGLE_MAX);
}

/** One border lap more or fewer; from unset laps the first step goes to 1 (up) or 0 (down). */
export function stepLaps(current: number | null, direction: 1 | -1, floor: number): number {
  if (current === null) return clamp(direction > 0 ? 1 : 0, floor, BORDER_LAPS_MAX);
  return clamp(current + direction, floor, BORDER_LAPS_MAX);
}

/** A spacing step from the mower's own default starts at the adapter's default, clamped. */
export function stepSpacing(current: number | null, direction: 1 | -1, range: SpacingRange) {
  if (current === null) return clamp(DEFAULT_SPACING_MM, range[0], range[1]);
  return clamp(current + direction * SPACING_STEP_MM, range[0], range[1]);
}

/** Read a `CommandView.pattern` (or any JSON) as a pattern, or null when it is none. */
export function readPattern(value: unknown): MowPattern | null {
  if (!value || typeof value !== 'object') return null;
  const raw = value as Record<string, unknown>;
  if (!isPatternName(raw.name ?? 'stripes')) return null;
  const pattern: MowPattern = {
    name: (raw.name as PatternName | undefined) ?? 'stripes',
    angle_deg: finite(raw.angle_deg) ? raw.angle_deg : 0,
    border_laps: finite(raw.border_laps) ? raw.border_laps : 0,
    spacing_m: finite(raw.spacing_m) ? raw.spacing_m : null,
  };
  if ((OBSTACLE_MODES as readonly unknown[]).includes(raw.obstacle_detection)) {
    pattern.obstacle_detection = raw.obstacle_detection as MowPattern['obstacle_detection'];
  }
  if (finite(raw.obstacle_laps)) pattern.obstacle_laps = raw.obstacle_laps;
  if ((PATH_ORDERS as readonly unknown[]).includes(raw.order)) {
    pattern.order = raw.order as MowPattern['order'];
  }
  if (finite(raw.start_progress_pct) && raw.start_progress_pct > 0) {
    pattern.start_progress_pct = raw.start_progress_pct;
  }
  return pattern;
}
