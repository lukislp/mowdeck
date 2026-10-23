/**
 * The job card of the mower page (`MowerState.job`): progress, the area the mower works in,
 * elapsed and remaining time and the mowed area. Pure and unit tested (`job.test.ts`).
 *
 * The values are the mower's own report (Mammotion: whole minutes); nothing here estimates.
 */
import type { components } from '$lib/generated/telemetry';

export type JobProgress = components['schemas']['JobProgress'];

/** Where the mower works: a named map area, between areas (a transfer), or not reported. */
export type JobArea =
  { kind: 'area'; id: string; name: string } | { kind: 'transit' } | { kind: 'unknown' };

export interface JobView {
  /** 0..100, whole percent; null when the mower reports no progress. */
  percent: number | null;
  area: JobArea;
  elapsedS: number | null;
  remainingS: number | null;
  mowedM2: number | null;
  /** The job is paused (shown dimmed, the bar stops). */
  paused: boolean;
}

/** Activities in which a reported job counts as running (the card shows). */
const JOB_ACTIVITIES: ReadonlySet<string> = new Set(['mowing', 'paused', 'returning', 'error']);

const finite = (value: number | null | undefined): value is number =>
  typeof value === 'number' && Number.isFinite(value);

/**
 * The card's content, or null when no job runs: no `job` on the state, or an activity in which a
 * left-over job means nothing (docked, charging, idle, offline, updating). `areaName` names a map
 * area (`areaNamer` of the page); an id the map does not know is still named by it.
 */
export function jobView(
  job: JobProgress | null | undefined,
  activity: string | null | undefined,
  areas: readonly { id: string; name?: string | null }[] | null | undefined,
  areaName: (area: { id: string; name?: string | null }) => string,
): JobView | null {
  if (!job || !activity || !JOB_ACTIVITIES.has(activity)) return null;
  const percent = finite(job.progress_percent)
    ? Math.round(Math.min(100, Math.max(0, job.progress_percent)))
    : null;
  let area: JobArea;
  if (job.area_id) {
    const found = (areas ?? []).find((a) => a.id === job.area_id) ?? { id: job.area_id };
    area = { kind: 'area', id: job.area_id, name: areaName(found) };
  } else {
    // A job without an area id drives between areas (or the vendor names none).
    area =
      activity === 'mowing' || activity === 'returning' ? { kind: 'transit' } : { kind: 'unknown' };
  }
  return {
    percent,
    area,
    elapsedS: finite(job.elapsed_s) ? Math.max(0, job.elapsed_s) : null,
    remainingS: finite(job.remaining_s) ? Math.max(0, job.remaining_s) : null,
    mowedM2: finite(job.mowed_m2) ? Math.max(0, job.mowed_m2) : null,
    paused: activity === 'paused' || activity === 'error',
  };
}

/** "2 h 05", "47 min", "< 1 min": a running time in whole minutes, the way a clock reads. */
export function formatRunTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 60) return '< 1 min';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return `${h} h ${String(rest).padStart(2, '0')}`;
}

/** Ticks of the segmented progress bar (one per 5 %). */
export const JOB_TICKS = 20;

/** How many of the bar's ticks are lit for `percent` (a started tick counts from 1 %). */
export function litTicks(percent: number | null): number {
  if (percent === null || percent <= 0) return 0;
  return Math.min(JOB_TICKS, Math.ceil((percent / 100) * JOB_TICKS));
}
