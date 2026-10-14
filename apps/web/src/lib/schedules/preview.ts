/**
 * The preview of a schedule (`GET /schedules/{id}/preview`) as the pages show it: the next
 * firing (skipped ones flagged), the rain verdict as one of five badges, and the forecast hours
 * as bars for the strip. Pure and unit tested (`preview.test.ts`).
 */
import type { ForecastHour, Preview, PreviewItem, RainDecision, RainPause, Verdict } from './types';

/** What the badge says about the next run's weather. */
export type VerdictBadge = 'run' | 'hold' | 'no_location' | 'unknown' | 'disabled';

/** The badge of a rain verdict (the provider-down variants follow what the run will do). */
export function verdictBadge(verdict: Verdict | null | undefined): VerdictBadge {
  switch (verdict) {
    case 'run':
      return 'run';
    case 'hold':
    case 'unknown_hold':
      return 'hold';
    case 'unknown_run':
      return 'unknown';
    case 'no_location':
      return 'no_location';
    case 'disabled':
      return 'disabled';
    default:
      return 'unknown';
  }
}

export interface PreviewView {
  /** The next firing that is not skipped, or null. */
  next: PreviewItem | null;
  /** A firing before `next` that `skip-next` skips, or null. */
  skipped: PreviewItem | null;
  items: PreviewItem[];
  badge: VerdictBadge;
  decision: RainDecision | null;
  /** Forecast hours ordered by start. */
  hours: ForecastHour[];
}

/** Read a preview; null (not loaded or failed) gives an empty view with an `unknown` badge. */
export function parsePreview(preview: Preview | null | undefined): PreviewView {
  const items = [...(preview?.items ?? [])].sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
  const index = items.findIndex((item) => !item.skipped);
  const next = index >= 0 ? (items[index] ?? null) : null;
  const skipped = (index > 0 ? items[index - 1] : index < 0 ? items[0] : null) ?? null;
  const decision = preview?.weather ?? null;
  return {
    next,
    skipped: skipped?.skipped ? skipped : null,
    items,
    badge: decision ? verdictBadge(decision.verdict) : next ? 'unknown' : 'disabled',
    decision,
    hours: [...(decision?.hours ?? [])].sort((a, b) => Date.parse(a.start) - Date.parse(b.start)),
  };
}

export interface HourBar {
  start: string;
  end: string;
  /** Precipitation probability in `[0, 1]`, or null without a value. */
  probability: number | null;
  /** Precipitation in millimetres, or null without a value. */
  mm: number | null;
  /** The hour reaches a threshold of the rain pause. */
  wet: boolean;
}

/** The forecast hours as bars, marking every hour that reaches a threshold. */
export function hourBars(
  hours: readonly ForecastHour[],
  settings: Pick<RainPause, 'probability_threshold' | 'precipitation_mm_threshold'>,
): HourBar[] {
  return hours.map((hour) => {
    const probability =
      hour.probability_percent == null
        ? null
        : Math.min(1, Math.max(0, hour.probability_percent / 100));
    const mm = hour.precipitation_mm ?? hour.rain_mm ?? null;
    const wet =
      (hour.probability_percent != null &&
        hour.probability_percent >= settings.probability_threshold) ||
      (mm != null && mm >= settings.precipitation_mm_threshold);
    return { start: hour.start, end: hour.end, probability, mm, wet };
  });
}
