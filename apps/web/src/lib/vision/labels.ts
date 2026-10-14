/**
 * Human readable texts of AI assist: drive steps ("Back 1.0 s", "Forward and left 1.5 s"),
 * situations, proposal states, risk flags and step outcomes. Pure (the locale is a parameter,
 * defaulting to the current one), so both languages are testable.
 */
import { m } from '$lib/paraglide/messages';
import { getLocale } from '$lib/paraglide/runtime';
import type { Locale } from '$lib/schedules/types';

import type { ProposalState, ProposalStep, RiskFlag, StepOutcome, VisionTask } from './api';

/** Below this share of full speed an axis counts as still (the model rounds to 0.05). */
export const AXIS_DEADBAND = 0.05;

export type StepMotion =
  | 'wait'
  | 'forward'
  | 'back'
  | 'left'
  | 'right'
  | 'forward_left'
  | 'forward_right'
  | 'back_left'
  | 'back_right';

const current = (): Locale => getLocale() as Locale;

/** The kind of movement of a step: linear -1..1 is backward..forward, angular -1..1 right..left. */
export function stepMotion(step: Pick<ProposalStep, 'linear' | 'angular'>): StepMotion {
  const ahead = step.linear > AXIS_DEADBAND ? 1 : step.linear < -AXIS_DEADBAND ? -1 : 0;
  const turn = step.angular > AXIS_DEADBAND ? 1 : step.angular < -AXIS_DEADBAND ? -1 : 0;
  if (ahead === 0 && turn === 0) return 'wait';
  if (ahead === 0) return turn > 0 ? 'left' : 'right';
  if (turn === 0) return ahead > 0 ? 'forward' : 'back';
  if (ahead > 0) return turn > 0 ? 'forward_left' : 'forward_right';
  return turn > 0 ? 'back_left' : 'back_right';
}

/** Seconds with one decimal in the locale ("1.0" / "1,0"). */
export function formatSeconds(seconds: number, locale: Locale = current()): string {
  return new Intl.NumberFormat(locale, {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  }).format(seconds);
}

/** "Back 1.0 s", "Turn left 1.0 s", "Forward and right 2.0 s", "Wait 0.5 s". */
export function stepDescription(
  step: Pick<ProposalStep, 'linear' | 'angular' | 'seconds'>,
  locale: Locale = current(),
): string {
  const options = { locale };
  const seconds = formatSeconds(step.seconds, locale);
  switch (stepMotion(step)) {
    case 'wait':
      return m.vision_step_wait({ seconds }, options);
    case 'forward':
      return m.vision_step_forward({ seconds }, options);
    case 'back':
      return m.vision_step_back({ seconds }, options);
    case 'left':
      return m.vision_step_left({ seconds }, options);
    case 'right':
      return m.vision_step_right({ seconds }, options);
    case 'forward_left':
      return m.vision_step_forward_left({ seconds }, options);
    case 'forward_right':
      return m.vision_step_forward_right({ seconds }, options);
    case 'back_left':
      return m.vision_step_back_left({ seconds }, options);
    case 'back_right':
      return m.vision_step_back_right({ seconds }, options);
  }
}

/** Speed of a step as a share of the slow assist speed in percent (the larger axis). */
export function stepEffort(step: Pick<ProposalStep, 'linear' | 'angular'>): number {
  return Math.round(Math.min(1, Math.max(Math.abs(step.linear), Math.abs(step.angular))) * 100);
}

/** The situation the model saw; unknown values (a newer service) fall back to "unknown". */
export function situationLabel(situation: string, locale: Locale = current()): string {
  const options = { locale };
  switch (situation) {
    case 'stuck_on_obstacle':
      return m.vision_situation_stuck_on_obstacle({}, options);
    case 'wheel_slip':
      return m.vision_situation_wheel_slip({}, options);
    case 'edge':
      return m.vision_situation_edge({}, options);
    case 'trapped':
      return m.vision_situation_trapped({}, options);
    case 'away_from_dock':
      return m.vision_situation_away_from_dock({}, options);
    case 'near_dock':
      return m.vision_situation_near_dock({}, options);
    case 'clear':
      return m.vision_situation_clear({}, options);
    default:
      return m.vision_situation_unknown({}, options);
  }
}

export function stateLabel(state: ProposalState, locale: Locale = current()): string {
  const options = { locale };
  switch (state) {
    case 'analysing':
      return m.vision_state_analysing({}, options);
    case 'proposed':
      return m.vision_state_proposed({}, options);
    case 'rejected_by_ai':
      return m.vision_state_rejected_by_ai({}, options);
    case 'failed':
      return m.vision_state_failed({}, options);
    case 'rejected':
      return m.vision_state_rejected({}, options);
    case 'expired':
      return m.vision_state_expired({}, options);
    case 'executing':
      return m.vision_state_executing({}, options);
    case 'verifying':
      return m.vision_state_verifying({}, options);
    case 'recovered':
      return m.vision_state_recovered({}, options);
    case 'not_recovered':
      return m.vision_state_not_recovered({}, options);
    case 'aborted':
      return m.vision_state_aborted({}, options);
  }
}

export function riskLabel(flag: RiskFlag, locale: Locale = current()): string {
  const options = { locale };
  switch (flag) {
    case 'people_visible':
      return m.vision_risk_people_visible({}, options);
    case 'animals_visible':
      return m.vision_risk_animals_visible({}, options);
    case 'drop_off':
      return m.vision_risk_drop_off({}, options);
    case 'water':
      return m.vision_risk_water({}, options);
    case 'road':
      return m.vision_risk_road({}, options);
  }
}

export function stepOutcomeLabel(outcome: StepOutcome, locale: Locale = current()): string {
  const options = { locale };
  switch (outcome) {
    case 'pending':
      return m.vision_outcome_pending({}, options);
    case 'done':
      return m.vision_outcome_done({}, options);
    case 'aborted':
      return m.vision_outcome_aborted({}, options);
    case 'skipped':
      return m.vision_outcome_skipped({}, options);
  }
}

export function taskLabel(task: VisionTask, locale: Locale = current()): string {
  return task === 'find_dock'
    ? m.vision_task_find_dock({}, { locale })
    : m.vision_task_recover({}, { locale });
}

/** The state's tone for badges: success, still running, ended by a person, or a stop. */
export function stateTone(state: ProposalState): 'mowing' | 'paused' | 'error' | 'docked' {
  switch (state) {
    case 'recovered':
      return 'mowing';
    case 'analysing':
    case 'proposed':
    case 'executing':
    case 'verifying':
      return 'paused';
    case 'rejected':
    case 'expired':
      return 'docked';
    case 'rejected_by_ai':
    case 'failed':
    case 'not_recovered':
    case 'aborted':
      return 'error';
  }
}

/** Confidence 0..1 as a whole percentage, clamped. */
export function confidencePercent(confidence: number): number {
  return Math.round(Math.min(1, Math.max(0, confidence)) * 100);
}

/** "4:05" until `expiresAt` from `now` (ms); "0:00" once over; null without an expiry. */
export function expiryCountdown(expiresAt: string | null, now: number): string | null {
  if (!expiresAt) return null;
  const at = Date.parse(expiresAt);
  if (Number.isNaN(at)) return null;
  const left = Math.max(0, Math.ceil((at - now) / 1000));
  const minutes = Math.floor(left / 60);
  const seconds = left % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

/** A USD cost estimate; four decimals below one cent (one analysis costs fractions of a cent). */
export function formatCost(
  usd: number | null | undefined,
  locale: Locale = current(),
): string | null {
  if (usd === null || usd === undefined || !Number.isFinite(usd)) return null;
  return new Intl.NumberFormat(locale, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: usd < 0.01 ? 4 : 2,
  }).format(usd);
}
