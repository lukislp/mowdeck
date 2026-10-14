import { describe, expect, it } from 'vitest';

import { ApiProblem, problemFromBody } from '$lib/api/problem';
import { problemMessage } from '$lib/i18n';
import { toasts } from '$lib/stores/ui.svelte';

import {
  featureLabel,
  isLimitKey,
  limitLabel,
  sourceLabel,
  statusLabel,
  statusTone,
} from './labels';
import { AccountPlan } from './plan.svelte';
import { missingFeatureOf, notifyPlanProblem, planLimitOf, planProblemText } from './problems';
import { PLAN_STATUSES, type ResolvedPlan } from './types';
import { isCounted, isRestricted, limitRows, statusUntil } from './usage';

const PROBLEM = 'urn:mowdeck:problem:';

function limitProblem(
  limit: string,
  current: string,
  detail = 'the plan home allows 4',
): ApiProblem {
  return problemFromBody(409, {
    type: `${PROBLEM}plan-limit`,
    title: 'Plan limit reached',
    status: 409,
    detail,
    errors: [
      { parameter: 'max_mowers', code: 'limit', detail: limit },
      { parameter: 'max_mowers', code: 'current', detail: current },
    ],
  });
}

function featureProblem(code = 'feature_not_in_plan'): ApiProblem {
  return problemFromBody(403, {
    type: `${PROBLEM}feature-not-in-plan`,
    status: 403,
    errors: [{ parameter: 'fleet.bulk_commands', code, detail: 'no' }],
  });
}

const resolved: ResolvedPlan = {
  account_id: 'a1',
  plan: 'home',
  plan_name: 'Home',
  status: 'trial',
  source: 'default',
  billing_enabled: true,
  limits: {
    max_mowers: 4,
    max_users: 5,
    max_sites: 1,
    max_groups: 2,
    max_schedules: null,
    history_days: 90,
  },
  features: ['view'],
  since: '2026-10-01T00:00:00Z',
  trial_until: '2026-10-31T00:00:00Z',
  grace_until: null,
};

describe('plan limits', () => {
  it('builds rows with use, share and reached', () => {
    const rows = limitRows(resolved.limits, { max_mowers: 4, max_users: 2, max_groups: 0 });
    const by = Object.fromEntries(rows.map((r) => [r.key, r]));
    expect(rows.map((r) => r.key)).toEqual([
      'max_mowers',
      'max_users',
      'max_sites',
      'max_groups',
      'max_schedules',
      'history_days',
    ]);
    expect(by.max_mowers).toMatchObject({ limit: 4, used: 4, share: 1, reached: true });
    expect(by.max_users).toMatchObject({ used: 2, share: 0.4, reached: false });
    expect(by.max_sites).toMatchObject({ used: null, share: null, reached: false });
    expect(by.max_schedules).toMatchObject({ limit: null, share: null, reached: false });
    expect(by.history_days).toMatchObject({ limit: 90, used: null });
    expect(isCounted('history_days')).toBe(false);
    expect(isCounted('max_mowers')).toBe(true);
  });

  it('treats a zero limit as full', () => {
    const [row] = limitRows({ max_mowers: 0 }, { max_mowers: 0 });
    expect(row).toMatchObject({ share: 1, reached: true });
  });

  it('knows restricted statuses and their end dates', () => {
    expect(isRestricted('grace')).toBe(true);
    expect(isRestricted('expired')).toBe(true);
    expect(isRestricted('trial')).toBe(false);
    expect(isRestricted(null)).toBe(false);
    expect(statusUntil(resolved)).toBe('2026-10-31T00:00:00Z');
    expect(statusUntil({ status: 'grace', grace_until: 'g' })).toBe('g');
    expect(statusUntil({ status: 'active' })).toBeNull();
  });
});

describe('plan labels', () => {
  it('translates every status, source and limit', () => {
    for (const status of PLAN_STATUSES) {
      expect(statusLabel(status)).not.toBe('');
      expect(statusTone(status).tone).toBeTruthy();
    }
    for (const source of ['default', 'manual', 'billing'] as const) {
      expect(sourceLabel(source)).not.toBe('');
    }
    expect(limitLabel('max_mowers')).toBe('Mowers');
    expect(isLimitKey('max_users')).toBe(true);
    expect(isLimitKey('max_planets')).toBe(false);
  });

  it('names known features and falls back for unknown ones', () => {
    expect(featureLabel('manual_drive')).toBe('Manual drive');
    expect(featureLabel('*')).toBe('All features');
    expect(featureLabel('x.unknown', 'From the catalogue')).toBe('From the catalogue');
    expect(featureLabel('x.unknown')).toBe('x.unknown');
  });
});

describe('plan refusals', () => {
  it('reads limit and current of plan-limit', () => {
    const problem = limitProblem('4', '4');
    expect(planLimitOf(problem)).toEqual({ key: 'max_mowers', limit: 4, current: 4 });
    const text = planProblemText(problem);
    expect(text?.title).toContain('Mowers');
    expect(text?.detail).toContain('4');
    expect(problemMessage(problem)).toBe(text?.title);
    expect(planLimitOf(featureProblem())).toBeNull();
  });

  it('explains an inactive or unavailable plan behind plan-limit', () => {
    expect(planProblemText(limitProblem('None', '3', 'the plan is not active: only'))?.title).toBe(
      'The plan is not active',
    );
    expect(planProblemText(limitProblem('', '', 'cannot be checked'))?.title).toBe(
      'The plan cannot be checked right now',
    );
  });

  it('names the missing feature', () => {
    const problem = featureProblem();
    expect(missingFeatureOf(problem)).toEqual({
      key: 'fleet.bulk_commands',
      reason: 'feature_not_in_plan',
    });
    expect(planProblemText(problem)?.title).toContain('Bulk commands');
    expect(problemMessage(problem)).toContain('Bulk commands');
    expect(planProblemText(featureProblem('plan_inactive'))?.title).toBe('The plan is not active');
    expect(planProblemText(featureProblem('entitlements_unavailable'))?.title).toBe(
      'The plan cannot be checked right now',
    );
    expect(missingFeatureOf(limitProblem('1', '1'))).toBeNull();
  });

  it('toasts plan refusals only', () => {
    const before = toasts.items.length;
    expect(notifyPlanProblem(new ApiProblem({ status: 404, type: `${PROBLEM}not-found` }))).toBe(
      false,
    );
    expect(toasts.items.length).toBe(before);
    expect(notifyPlanProblem(limitProblem('4', '4'))).toBe(true);
    const last = toasts.items.at(-1);
    expect(last?.tone).toBe('error');
    expect(last?.detail).toContain('4');
  });
});

describe('account plan store', () => {
  it('loads once, reloads on demand and resets', async () => {
    let calls = 0;
    const store = new AccountPlan(async () => {
      calls += 1;
      return { ...resolved, status: calls > 1 ? 'grace' : 'trial' };
    });
    await store.load();
    await store.load();
    expect(calls).toBe(1);
    expect(store.restricted).toBe(false);
    await store.load(true);
    expect(store.current?.status).toBe('grace');
    expect(store.restricted).toBe(true);
    store.reset();
    expect(store.current).toBeNull();
  });

  it('marks a failed load and keeps nothing', async () => {
    const store = new AccountPlan(() => Promise.reject(new Error('down')));
    expect(await store.load()).toBeNull();
    expect(store.failed).toBe(true);
  });
});
