/**
 * Rain readout and start warning of the mower page, from the Scheduler's weather answer
 * (`GET /weather/{mower_id}` -> `decision.hours`). Pure and unit tested (`rain.test.ts`).
 *
 * Presentation only: the verdict, whether it holds, when the hold ends (`decision.pause_until`)
 * and the thresholds applied (`decision.settings`) all come from the Scheduler, which owns the
 * rain rule (`services/scheduler`, `domain/rain.py`). Nothing here re-derives them.
 */
import { hourBars, type HourBar } from '#lib/schedules/preview.js';
import type { ForecastHour, RainPause } from '#lib/schedules/types.js';

/** The thresholds the Scheduler applied, as echoed in `decision.settings`. */
export type Thresholds = Pick<RainPause, 'probability_threshold' | 'precipitation_mm_threshold'>;
/** Precipitation at or above this in the current hour counts as "it rains now". */
export const RAINING_MM = 0.1;
/** Hours shown in the sidebar strip. */
export const STRIP_HOURS = 12;
/** Past hours shown before the "now" mark. */
export const PAST_STRIP_HOURS = 12;

const ms = (iso: string) => Date.parse(iso);

/** The bar whose hour contains `now`, or null. */
export function currentBar(bars: readonly HourBar[], now: number): HourBar | null {
  return bars.find((b) => ms(b.start) <= now && now < ms(b.end)) ?? null;
}

export interface RainNow {
  raining: boolean;
  mm: number | null;
  probability: number | null;
}

/** What falls in the current hour. Without an hour for now: not raining, unknown. */
export function rainNow(bars: readonly HourBar[], now: number): RainNow {
  const bar = currentBar(bars, now);
  if (!bar) return { raining: false, mm: null, probability: null };
  return { raining: (bar.mm ?? 0) >= RAINING_MM, mm: bar.mm, probability: bar.probability };
}

/**
 * Bars for the readout: past hours only count as wet by their precipitation (a past
 * probability decides nothing), the others as the rain pause does.
 */
export function readoutBars(
  hours: readonly ForecastHour[],
  settings: Thresholds,
  now: number,
): HourBar[] {
  return hourBars(hours, settings).map((bar) =>
    ms(bar.end) <= now
      ? { ...bar, wet: bar.mm !== null && bar.mm >= settings.precipitation_mm_threshold }
      : bar,
  );
}

/** The last `back` past hours and the next `ahead` hours (from the current one), ordered. */
export function stripBars(
  bars: readonly HourBar[],
  now: number,
  ahead = STRIP_HOURS,
  back = PAST_STRIP_HOURS,
): HourBar[] {
  const sorted = [...bars].sort((a, b) => ms(a.start) - ms(b.start));
  const past = sorted.filter((b) => ms(b.end) <= now).slice(-back);
  const coming = sorted.filter((b) => ms(b.end) > now).slice(0, ahead);
  return [...past, ...coming];
}

export interface RainWarning {
  /** `now`: it rains right now; `soon`: a wet hour starts within the job. */
  kind: 'now' | 'soon' | 'wet';
  /** Start of the wet hour (for `now`: the current hour; for `wet`: when the lawn is dry). */
  start: string;
  /** Probability in percent, or null. */
  percent: number | null;
}

function percent(bar: HourBar): number | null {
  return bar.probability === null ? null : Math.round(bar.probability * 100);
}

/** Rain falling now, or a wet hour overlapping `[now, now + minutes]`; null when dry. */
export function rainWarning(
  bars: readonly HourBar[],
  now: number,
  minutes: number | null,
  wetUntil: string | null = null,
): RainWarning | null {
  const current = currentBar(bars, now);
  if (current && (current.mm ?? 0) >= RAINING_MM) {
    return { kind: 'now', start: current.start, percent: percent(current) };
  }
  const end = now + (minutes ?? 60) * 60_000;
  const wet = [...bars]
    .sort((a, b) => ms(a.start) - ms(b.start))
    .find((b) => b.wet && ms(b.end) > now && ms(b.start) < end);
  if (!wet) {
    return wetUntil && ms(wetUntil) > now ? { kind: 'wet', start: wetUntil, percent: null } : null;
  }
  return { kind: wet === current ? 'now' : 'soon', start: wet.start, percent: percent(wet) };
}
