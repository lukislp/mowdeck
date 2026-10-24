/**
 * Pure rules of the start sheet (the flow itself is `start-sheet.svelte.ts`): what a start or a
 * preview sends, when a start saves the mow defaults, why a preview failed, how long the picked
 * areas take and when docking needs the "end the job?" choice. Unit tested (`start-sheet.test.ts`).
 */
import type { Activity } from '@mowdeck/ui';

import type { ApiProblem } from '$lib/api/problem';
import { problemMessage } from '$lib/i18n';
import { m } from '$lib/paraglide/messages';
import type { Verdict } from '$lib/schedules/types';

import { polygonArea } from './commands';
import { estimateMinutes } from './duration';
import type { JobOptionsSpec } from './job-options';
import {
  defaultsOf,
  initialDraft,
  samePattern,
  toPattern,
  type MowPattern,
  type PatternName,
  type SpacingRange,
} from './mow-pattern';

/** An area of the map snapshot as the sheet needs it. */
export interface SheetArea {
  id: string;
  boundary: ReadonlyArray<ReadonlyArray<number>>;
}

/** Body of `POST /mowers/{id}/path-preview`. */
export type PreviewRequest = { area_ids: string[]; pattern?: MowPattern };
/** The `start_mow` command (without its kind). */
export type StartRequest = { area_ids: string[]; pattern?: MowPattern };

/** The areas in order and, with a pattern, a copy of it. */
export function startRequest(areaIds: readonly string[], pattern: MowPattern | null): StartRequest {
  return pattern ? { area_ids: [...areaIds], pattern: { ...pattern } } : { area_ids: [...areaIds] };
}

/** The message of a failed preview: timeout, unsupported by the adapter, else the problem's. */
export function previewFailure(problem: ApiProblem): string {
  if (problem.status === 504) return m.start_preview_timeout();
  if (problem.slug === 'path-preview-unsupported') return m.start_preview_unsupported();
  return problemMessage(problem);
}

/**
 * The defaults change only when the started pattern differs from what the sheet opened with:
 * the saved defaults, else the fallback for the offered patterns. The start progress is never
 * part of the defaults (`defaultsOf`).
 */
export function defaultsChanged(
  pattern: MowPattern,
  saved: MowPattern | null,
  offered: readonly PatternName[],
  spacing: SpacingRange | null,
  options: JobOptionsSpec | null = null,
): boolean {
  const before = saved ?? toPattern(initialDraft(null, offered, spacing, options));
  return !samePattern(defaultsOf(pattern), defaultsOf(before));
}

export interface JobEstimate {
  areas: readonly SheetArea[];
  picked: readonly string[];
  /** `points_total` of a preview for exactly these areas and this pattern, else null. */
  pointsTotal: number | null;
  cuttingWidthMm: number | null | undefined;
  speedMps: number | null | undefined;
}

/** Estimated job length in minutes for the picked areas (null: nothing picked or unknown). */
export function jobMinutes(input: JobEstimate): number | null {
  if (input.picked.length === 0) return null;
  const area = input.areas
    .filter((a) => input.picked.includes(a.id))
    .reduce((sum, a) => sum + polygonArea(a.boundary), 0);
  return estimateMinutes({
    pointsTotal: input.pointsTotal,
    areaM2: area,
    cuttingWidthM: input.cuttingWidthMm != null ? input.cuttingWidthMm / 1000 : null,
    speedMps: input.speedMps ?? null,
  });
}

/** What the rain readout shows for the Scheduler's verdict (null: no weather answer). */
export type RainStatus = 'ok' | 'no_location' | 'disabled' | 'unavailable';

export function rainStatusOf(verdict: Verdict | null): RainStatus {
  if (verdict === null) return 'no_location';
  switch (verdict) {
    case 'disabled':
      return 'disabled';
    case 'unknown_run':
    case 'unknown_hold':
      return 'unavailable';
    default:
      return 'ok';
  }
}

/** A job exists on the mower (running, paused or interrupted), so docking needs a choice. */
export function jobActive(activity: Activity | null): boolean {
  return activity === 'mowing' || activity === 'paused' || activity === 'error';
}
