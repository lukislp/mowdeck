/** Mower activities as in the contracts (`MowerActivity`); the library keeps its own copy so it
 * has no dependency on generated API types. */
export const ACTIVITIES = [
  'idle',
  'mowing',
  'paused',
  'returning',
  'docked',
  'charging',
  'error',
  'offline',
  'unknown',
] as const;

export type Activity = (typeof ACTIVITIES)[number];

/** RTK fix quality as in the contracts (`RtkFixStatus`). */
export type RtkFix = 'fixed' | 'float' | 'none' | 'unknown';

/** The state colour family used for an activity (idle shares the neutral docked tone). */
export function activityTone(activity: Activity): Exclude<Activity, 'idle'> {
  return activity === 'idle' ? 'docked' : activity;
}

/** One command button of the command bar. Texts arrive translated. */
export interface CommandAction {
  id: string;
  label: string;
  icon?: import('./icons/Icon.svelte').IconName;
  enabled: boolean;
  /** Shown as the button's description when disabled (why it cannot be used now). */
  reason?: string;
  /** A command of this kind is on its way to the mower. */
  pending?: boolean;
  /** The main action of the current state (filled style). */
  primary?: boolean;
}

/** Feedback state of the emergency stop. */
export type StopState = 'idle' | 'sending' | 'sent' | 'executed' | 'failed';

export type ToastTone = 'ok' | 'info' | 'error';

export interface ToastItem {
  id: string;
  tone: ToastTone;
  title: string;
  detail?: string;
  /** A correlation id or similar reference, shown in monospace. */
  code?: string;
}
