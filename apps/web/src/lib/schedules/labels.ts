/**
 * Schedule texts and tones in the active locale: rain verdicts and run outcomes as badges in
 * the state colour language (exhaustive switches, so a new contract value fails the type check).
 */
import { m } from '$lib/paraglide/messages';

import type { VerdictBadge } from './preview';
import type { RunOutcome } from './types';

/** State tones of the design system a badge can take. */
export type Tone = 'mowing' | 'returning' | 'docked' | 'paused' | 'error' | 'offline' | 'unknown';
/** The small glyph of a badge (never colour alone). */
export type Glyph = 'check' | 'rain' | 'pin' | 'question' | 'dash' | 'skip' | 'clock' | 'alert';

export interface BadgeLook {
  label: string;
  tone: Tone;
  glyph: Glyph;
}

export function verdictLook(badge: VerdictBadge): BadgeLook {
  switch (badge) {
    case 'run':
      return { label: m.sched_verdict_run(), tone: 'mowing', glyph: 'check' };
    case 'hold':
      return { label: m.sched_verdict_hold(), tone: 'paused', glyph: 'rain' };
    case 'no_location':
      return { label: m.sched_verdict_no_location(), tone: 'offline', glyph: 'pin' };
    case 'unknown':
      return { label: m.sched_verdict_unknown(), tone: 'unknown', glyph: 'question' };
    case 'disabled':
      return { label: m.sched_verdict_disabled(), tone: 'docked', glyph: 'dash' };
  }
}

export function outcomeLook(outcome: RunOutcome): BadgeLook {
  switch (outcome) {
    case 'started':
      return { label: m.sched_outcome_started(), tone: 'mowing', glyph: 'check' };
    case 'skipped_rain':
      return { label: m.sched_outcome_skipped_rain(), tone: 'paused', glyph: 'rain' };
    case 'skipped_manual':
      return { label: m.sched_outcome_skipped_manual(), tone: 'docked', glyph: 'skip' };
    case 'skipped_window':
      return { label: m.sched_outcome_skipped_window(), tone: 'offline', glyph: 'clock' };
    case 'skipped_mower_busy':
      return { label: m.sched_outcome_skipped_mower_busy(), tone: 'returning', glyph: 'dash' };
    case 'skipped_permission':
      return { label: m.sched_outcome_skipped_permission(), tone: 'error', glyph: 'alert' };
    case 'failed':
      return { label: m.sched_outcome_failed(), tone: 'error', glyph: 'alert' };
  }
}

/** The follow-up of a started run (`rain_dock`, `window_dock`, `finished`, `closed`). */
export function followupLabel(followup: string | null): string | null {
  switch (followup) {
    case 'rain_dock':
      return m.sched_followup_rain_dock();
    case 'window_dock':
      return m.sched_followup_window_dock();
    case 'finished':
      return m.sched_followup_finished();
    case 'closed':
      return m.sched_followup_closed();
    case null:
      return null;
    default:
      return followup;
  }
}
