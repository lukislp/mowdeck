/** Scheduler API types as the schedules module uses them (from the generated OpenAPI types). */
import type { components } from '#lib/generated/scheduler.js';

type S = components['schemas'];

export type Schedule = S['ScheduleOut'];
export type ScheduleIn = S['ScheduleIn'];
export type SchedulePatch = S['SchedulePatch'];
export type Preview = S['PreviewOut'];
export type PreviewItem = S['PreviewItemOut'];
export type RainDecision = S['RainDecisionOut'];
export type RainPause = S['RainPause'];
/** An hour of the forecast; `past` is optional for fixtures (the API always sends it). */
export type ForecastHour = Omit<S['HourOut'], 'past'> & { past?: boolean };
export type Run = S['RunOut'];
export type RunOutcome = S['ScheduleRunOutcome'];
export type Verdict = RainDecision['verdict'];

export type Weekday = 'mon' | 'tue' | 'wed' | 'thu' | 'fri' | 'sat' | 'sun';
/** Days in week order (Monday first), as the API names them. */
export const WEEKDAYS: readonly Weekday[] = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

export type Locale = 'de' | 'en';

export function isWeekday(value: string): value is Weekday {
  return (WEEKDAYS as readonly string[]).includes(value);
}
