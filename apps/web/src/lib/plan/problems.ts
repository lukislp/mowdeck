/**
 * Plan refusals of the services (ADR 0028): 409 `plan-limit` carries the limit key with the limit
 * and the current count in `errors` (`code` `limit` and `current`), 403 `feature-not-in-plan`
 * the feature key as the error's `parameter` and the refusal reason as its `code`. The texts
 * name the limit or feature in words; `notifyPlanProblem` puts them in a toast.
 */
import type { ApiProblem } from '#lib/api/problem.js';
import { m } from '#lib/paraglide/messages.js';
import { toasts } from '#lib/stores/ui.svelte.js';

import { featureLabel, isLimitKey, limitLabel } from './labels';

export interface PlanLimitInfo {
  key: string;
  limit: number | null;
  current: number | null;
}

function numberOrNull(value: string | undefined): number | null {
  if (value === undefined || value.trim() === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** The limit, its value and the current count of a `plan-limit` problem. */
export function planLimitOf(problem: ApiProblem): PlanLimitInfo | null {
  if (problem.slug !== 'plan-limit') return null;
  const limit = problem.errors.find((e) => e.code === 'limit');
  const current = problem.errors.find((e) => e.code === 'current');
  const key = limit?.parameter ?? current?.parameter ?? '';
  return {
    key,
    limit: numberOrNull(limit?.message),
    current: numberOrNull(current?.message),
  };
}

/** The feature key of a `feature-not-in-plan` problem and why it was refused. */
export function missingFeatureOf(problem: ApiProblem): { key: string; reason: string } | null {
  if (problem.slug !== 'feature-not-in-plan') return null;
  const first = problem.errors[0];
  return { key: first?.parameter ?? '', reason: first?.code ?? 'feature_not_in_plan' };
}

const INACTIVE = /not active/i;

/** Title and detail of a plan refusal in words, or null for any other problem. */
export function planProblemText(problem: ApiProblem): { title: string; detail: string } | null {
  const limit = planLimitOf(problem);
  if (limit) {
    const name = isLimitKey(limit.key) ? limitLabel(limit.key) : limit.key;
    if (limit.limit === null) {
      return INACTIVE.test(problem.detail ?? '')
        ? { title: m.problem_plan_inactive(), detail: m.problem_plan_inactive_detail() }
        : { title: m.problem_plan_unavailable(), detail: m.problem_plan_unavailable_detail() };
    }
    return {
      title: m.problem_plan_limit({ limit: name }),
      detail: m.problem_plan_limit_detail({
        limit: String(limit.limit),
        current: String(limit.current ?? limit.limit),
      }),
    };
  }
  const feature = missingFeatureOf(problem);
  if (feature) {
    if (feature.reason === 'plan_inactive') {
      return { title: m.problem_plan_inactive(), detail: m.problem_plan_inactive_detail() };
    }
    if (feature.reason === 'entitlements_unavailable') {
      return { title: m.problem_plan_unavailable(), detail: m.problem_plan_unavailable_detail() };
    }
    return {
      title: m.problem_feature_not_in_plan({ feature: featureLabel(feature.key) }),
      detail: m.problem_feature_not_in_plan_detail(),
    };
  }
  return null;
}

/** Show a plan refusal as a problem toast; true when `problem` was one. */
export function notifyPlanProblem(problem: ApiProblem): boolean {
  const text = planProblemText(problem);
  if (!text) return false;
  toasts.push('error', text.title, text.detail, problem.correlationId ?? undefined);
  return true;
}
