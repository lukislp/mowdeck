/**
 * The pattern settings of a native start (`StartMow.pattern`, the contracts' `MowPattern`):
 * which patterns the start sheet offers, what it pre-fills (the mower's saved mow defaults,
 * else stripes at 0° with one border lap) and what it sends. Pure, unit tested
 * (`mow-pattern.test.ts`); the labels live in `$lib/i18n` (`patternSummary`).
 */

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

/** `MowPattern` as JSON. `spacing_m` is absent (or null) for the mower's own default. */
export interface MowPattern {
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
}

export type SpacingRange = readonly [min: number, max: number];

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
  return { name, angle, laps, spacingMm };
}

/** The draft as `StartMow.pattern` (and as the body of `PUT .../mow-defaults`). */
export function toPattern(draft: PatternDraft): MowPattern {
  const pattern: MowPattern = {
    name: draft.name,
    angle_deg: draft.angle,
    border_laps: Math.max(draft.laps, minLaps(draft.name)),
  };
  if (draft.spacingMm !== null) pattern.spacing_m = draft.spacingMm / 1000;
  return pattern;
}

/** Whether two patterns say the same (a missing spacing equals null). */
export function samePattern(a: MowPattern | null | undefined, b: MowPattern | null | undefined) {
  if (!a || !b) return !a && !b;
  return (
    a.name === b.name &&
    a.angle_deg === b.angle_deg &&
    a.border_laps === b.border_laps &&
    (a.spacing_m ?? null) === (b.spacing_m ?? null)
  );
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
  return {
    name: (raw.name as PatternName | undefined) ?? 'stripes',
    angle_deg: finite(raw.angle_deg) ? raw.angle_deg : 0,
    border_laps: finite(raw.border_laps) ? raw.border_laps : 0,
    spacing_m: finite(raw.spacing_m) ? raw.spacing_m : null,
  };
}
