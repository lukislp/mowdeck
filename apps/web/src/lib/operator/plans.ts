/**
 * The plan editor of the operator console: a plan as form values (limits as text, empty means
 * unlimited), the checks Entitlements makes (`PlanIn`: key pattern, name, limits as whole numbers
 * from 0, trial days 0 to 365) and the body of `PUT /entitlements/plans/{key}` (the whole plan).
 */
import { LIMIT_KEYS, type LimitKey, type PlanDetail, type PlanIn } from '$lib/plan/types';

export const PLAN_KEY_PATTERN = /^[a-z][a-z0-9_-]{0,63}$/;

export interface PlanForm {
  key: string;
  name: string;
  description: string;
  trialDays: string;
  isDefault: boolean;
  active: boolean;
  limits: Record<LimitKey, string>;
  features: string[];
}

export type PlanIssue = 'required' | 'key' | 'number' | 'trial';

export function planForm(plan: PlanDetail | null): PlanForm {
  const limits = Object.fromEntries(
    LIMIT_KEYS.map((key) => {
      const value = plan?.limits[key];
      return [key, value === null || value === undefined ? '' : String(value)];
    }),
  ) as Record<LimitKey, string>;
  return {
    key: plan?.key ?? '',
    name: plan?.name ?? '',
    description: plan?.description ?? '',
    trialDays: String(plan?.trial_days ?? 0),
    isDefault: plan?.is_default ?? false,
    active: plan?.active ?? true,
    limits,
    features: [...(plan?.features ?? ['view', 'emergency_stop'])],
  };
}

function wholeNumber(value: string): number | null {
  const text = value.trim();
  if (!/^\d+$/.test(text)) return null;
  return Number(text);
}

/** Problems per field (`key`, `name`, `trialDays`, or a limit key); empty when valid. */
export function validatePlan(form: PlanForm): Record<string, PlanIssue> {
  const issues: Record<string, PlanIssue> = {};
  if (!form.key.trim()) issues.key = 'required';
  else if (!PLAN_KEY_PATTERN.test(form.key.trim())) issues.key = 'key';
  if (!form.name.trim()) issues.name = 'required';
  const trial = wholeNumber(form.trialDays);
  if (trial === null || trial > 365) issues.trialDays = 'trial';
  for (const key of LIMIT_KEYS) {
    const value = form.limits[key];
    if (value.trim() !== '' && wholeNumber(value) === null) issues[key] = 'number';
  }
  return issues;
}

/** The body of `PUT /plans/{key}` from a valid form. */
export function planBody(form: PlanForm): PlanIn {
  const limits = Object.fromEntries(
    LIMIT_KEYS.map((key) => {
      const value = form.limits[key].trim();
      return [key, value === '' ? null : Number(value)];
    }),
  ) as PlanIn['limits'];
  return {
    name: form.name.trim(),
    description: form.description.trim() || null,
    limits,
    features: [...new Set(form.features)].sort(),
    trial_days: Number(form.trialDays.trim()),
    is_default: form.isDefault,
    active: form.active,
  };
}

/** Toggle a feature key in the form's list. */
export function toggleFeature(features: string[], key: string, on: boolean): string[] {
  const rest = features.filter((f) => f !== key);
  return on ? [...rest, key] : rest;
}
