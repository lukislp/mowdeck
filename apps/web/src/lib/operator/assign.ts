/**
 * The "assign plan" sheet: plan, status, an optional end of a trial or grace period (a local date,
 * sent as the end of that day) and the required reason (kept in the history). Without an end date
 * Entitlements uses the plan's trial days or the installation's grace days.
 */
import type { AssignmentIn, PlanStatus } from '$lib/plan/types';

export interface AssignForm {
  plan: string;
  status: PlanStatus;
  /** `YYYY-MM-DD` or empty. */
  until: string;
  reason: string;
}

export type AssignIssue = 'plan' | 'reason' | 'until';

export function assignForm(plan: string, status: PlanStatus = 'active'): AssignForm {
  return { plan, status, until: '', reason: '' };
}

/** Does the status take an end date? */
export function takesUntil(status: PlanStatus): boolean {
  return status === 'trial' || status === 'grace';
}

/** The end of the local day `date` as an ISO time, or null for an invalid date. */
export function endOfDay(date: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return null;
  const at = new Date(`${date}T23:59:59`);
  return Number.isNaN(at.getTime()) ? null : at.toISOString();
}

export function validateAssignment(form: AssignForm, now: number = Date.now()): AssignIssue[] {
  const issues: AssignIssue[] = [];
  if (!form.plan) issues.push('plan');
  if (!form.reason.trim()) issues.push('reason');
  if (takesUntil(form.status) && form.until) {
    const until = endOfDay(form.until);
    if (until === null || Date.parse(until) <= now) issues.push('until');
  }
  return issues;
}

export function assignmentBody(form: AssignForm, accountName: string | null): AssignmentIn {
  const until = takesUntil(form.status) && form.until ? endOfDay(form.until) : null;
  return {
    plan: form.plan,
    status: form.status,
    reason: form.reason.trim(),
    trial_until: form.status === 'trial' ? until : null,
    grace_until: form.status === 'grace' ? until : null,
    account_name: accountName,
  };
}
