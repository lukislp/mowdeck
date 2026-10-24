import { describe, expect, it, vi } from 'vitest';

import type { PlanDetail } from '#lib/plan/types.js';

import { OperatorAccess, PLATFORM_PROBE, probeOperator } from './access.svelte';
import {
  filterAccounts,
  joinAccounts,
  type ConsoleAccount,
  type PlatformAccount,
} from './accounts';
import { assignForm, assignmentBody, endOfDay, takesUntil, validateAssignment } from './assign';
import { accountKindLabel, accountStatusLabel, verdictLabel } from './labels';
import { planBody, planForm, toggleFeature, validatePlan } from './plans';
import {
  parseVisionDefault,
  probeService,
  probeVisionDefault,
  SERVICE_PROBES,
  VISION_STATUS_URL,
  verdictOf,
} from './system';

const account = (id: string, name: string, owner: string | null): PlatformAccount => ({
  id,
  name,
  kind: 'household',
  status: 'active',
  created_at: '2026-10-01T00:00:00Z',
  owner_username: owner,
});

const effective = {
  account_id: 'a1',
  plan: 'unlimited',
  plan_name: 'Unlimited',
  status: 'active' as const,
  source: 'default' as const,
  billing_enabled: false,
  limits: {},
  features: ['*'],
};

describe('operator access probe', () => {
  it('probes once until reset', async () => {
    let calls = 0;
    const access = new OperatorAccess(async () => {
      calls += 1;
      return calls === 1;
    });
    expect(access.checked).toBe(false);
    await Promise.all([access.load(), access.load()]);
    expect(calls).toBe(1);
    expect(access.allowed).toBe(true);
    expect(access.checked).toBe(true);
    access.reset();
    expect(access.allowed).toBe(false);
    await access.load();
    expect(access.allowed).toBe(false);
    expect(access.checked).toBe(true);
  });

  it('asks /me/platform and treats every failure as no operator, quietly', async () => {
    const error = vi.spyOn(console, 'error');
    const answer = (status: number, body: unknown) =>
      vi.fn(async () => new Response(JSON.stringify(body), { status }));
    const yes = answer(200, { operator_admin: true });
    expect(await probeOperator(yes as never)).toBe(true);
    expect(yes).toHaveBeenCalledWith(PLATFORM_PROBE, expect.anything());
    expect(await probeOperator(answer(200, { operator_admin: false }) as never)).toBe(false);
    expect(await probeOperator(answer(200, null) as never)).toBe(false);
    expect(await probeOperator(answer(404, { title: 'Not Found' }) as never)).toBe(false);
    expect(await probeOperator(answer(403, {}) as never)).toBe(false);
    const offline = vi.fn(async () => Promise.reject(new TypeError('offline')));
    expect(await probeOperator(offline as never)).toBe(false);
    const garbage = vi.fn(async () => new Response('<html>', { status: 200 }));
    expect(await probeOperator(garbage as never)).toBe(false);
    expect(error).not.toHaveBeenCalled();
    error.mockRestore();
  });
});

describe('account list', () => {
  const plans = new Map([
    [
      'a1',
      {
        assigned: {
          plan: 'fleet',
          status: 'active' as const,
          source: 'manual' as const,
          since: 's',
          trial_until: null,
          grace_until: null,
          updated_at: 'u',
        },
        effective,
      },
    ],
  ]);
  const rows: ConsoleAccount[] = joinAccounts(
    [account('a1', 'Miller home', 'anna'), account('a2', 'Garden Co', null)],
    plans,
  );

  it('joins plans by id and leaves the rest without', () => {
    expect(rows[0]?.plan?.assigned?.plan).toBe('fleet');
    expect(rows[1]?.plan).toBeNull();
  });

  it('filters by every word over name, owner, id and plan', () => {
    expect(filterAccounts(rows, '').length).toBe(2);
    expect(filterAccounts(rows, 'ANNA').map((r) => r.id)).toEqual(['a1']);
    expect(filterAccounts(rows, 'fleet miller').map((r) => r.id)).toEqual(['a1']);
    expect(filterAccounts(rows, 'garden').map((r) => r.id)).toEqual(['a2']);
    expect(filterAccounts(rows, 'garden anna')).toEqual([]);
  });

  it('labels kinds, statuses and verdicts', () => {
    expect(accountKindLabel('household')).toBe('Household');
    expect(accountKindLabel('business')).toBe('Business');
    expect(accountKindLabel('club')).toBe('club');
    expect(accountStatusLabel('active')).toBe('Active');
    expect(accountStatusLabel('deleting')).toBe('Being deleted');
    expect(accountStatusLabel('odd')).toBe('odd');
    expect(verdictLabel('up')).not.toBe(verdictLabel('down'));
    expect(verdictLabel('degraded')).not.toBe('');
  });
});

describe('assign plan form', () => {
  it('needs plan and reason, and a future end date when given', () => {
    const now = Date.parse('2026-10-10T12:00:00Z');
    expect(validateAssignment(assignForm(''), now)).toEqual(['plan', 'reason']);
    const form = { ...assignForm('fleet', 'trial'), reason: ' upgrade ', until: '2026-01-01' };
    expect(validateAssignment(form, now)).toEqual(['until']);
    expect(validateAssignment({ ...form, until: '2026-12-01' }, now)).toEqual([]);
    expect(validateAssignment({ ...form, until: 'soon' }, now)).toEqual(['until']);
    expect(validateAssignment({ ...form, status: 'active', until: '2020-01-01' }, now)).toEqual([]);
  });

  it('sends the end date only with its status', () => {
    expect(takesUntil('trial')).toBe(true);
    expect(takesUntil('grace')).toBe(true);
    expect(takesUntil('expired')).toBe(false);
    expect(endOfDay('2026-13-45')).toBeNull();
    expect(endOfDay('x')).toBeNull();
    const trial = assignmentBody(
      { plan: 'fleet', status: 'trial', until: '2026-12-01', reason: ' why ' },
      'Miller',
    );
    expect(trial.reason).toBe('why');
    expect(trial.trial_until).toBe(endOfDay('2026-12-01'));
    expect(trial.grace_until).toBeNull();
    expect(trial.account_name).toBe('Miller');
    const grace = assignmentBody({ plan: 'home', status: 'grace', until: '', reason: 'r' }, null);
    expect(grace.grace_until).toBeNull();
    const active = assignmentBody(
      { plan: 'home', status: 'active', until: '2026-12-01', reason: 'r' },
      null,
    );
    expect(active.trial_until).toBeNull();
  });
});

describe('plan editor form', () => {
  const home: PlanDetail = {
    key: 'home',
    name: 'Home',
    description: 'One garden',
    limits: { max_mowers: 4, max_users: null },
    features: ['view', 'control'],
    trial_days: 30,
    is_default: true,
    active: true,
    unknown_features: [],
    from_plan_file: true,
    created_at: null,
    updated_at: null,
  };

  it('round-trips a plan', () => {
    const form = planForm(home);
    expect(form.limits.max_mowers).toBe('4');
    expect(form.limits.max_users).toBe('');
    expect(validatePlan(form)).toEqual({});
    const body = planBody(form);
    expect(body.limits).toMatchObject({ max_mowers: 4, max_users: null, history_days: null });
    expect(body.features).toEqual(['control', 'view']);
    expect(body.trial_days).toBe(30);
    expect(body.description).toBe('One garden');
  });

  it('starts a new plan with view and the emergency stop', () => {
    const form = planForm(null);
    expect(form.features).toEqual(['view', 'emergency_stop']);
    expect(validatePlan(form)).toMatchObject({ key: 'required', name: 'required' });
    expect(planBody({ ...form, key: 'x', name: 'X' }).description).toBeNull();
  });

  it('checks key, trial days and limits', () => {
    const form = { ...planForm(home), key: '1bad', trialDays: '400' };
    form.limits = { ...form.limits, max_sites: '-1', max_groups: '2.5' };
    expect(validatePlan(form)).toEqual({
      key: 'key',
      trialDays: 'trial',
      max_sites: 'number',
      max_groups: 'number',
    });
  });

  it('toggles features without duplicates', () => {
    expect(toggleFeature(['view'], 'control', true)).toEqual(['view', 'control']);
    expect(toggleFeature(['view', 'control'], 'control', true)).toEqual(['view', 'control']);
    expect(toggleFeature(['view', 'control'], 'view', false)).toEqual(['control']);
  });
});

describe('system probes', () => {
  it('judges status codes', () => {
    expect(verdictOf(200)).toBe('up');
    expect(verdictOf(304)).toBe('up');
    expect(verdictOf(403)).toBe('degraded');
    expect(verdictOf(404)).toBe('down');
    expect(verdictOf(503)).toBe('down');
    expect(verdictOf(null)).toBe('down');
    expect(verdictOf(0)).toBe('down');
  });

  it('measures latency and survives failures', async () => {
    let clock = 100;
    const now = () => clock;
    const ok = await probeService(
      SERVICE_PROBES[0]!,
      async () => {
        clock += 42;
        return new Response('{}', { status: 200 });
      },
      now,
    );
    expect(ok).toMatchObject({ status: 200, latencyMs: 42, verdict: 'up' });
    const down = await probeService(
      SERVICE_PROBES[1]!,
      () => Promise.reject(new Error('offline')),
      now,
    );
    expect(down).toMatchObject({ status: null, latencyMs: null, verdict: 'down' });
    expect(SERVICE_PROBES.map((p) => p.service)).toContain('entitlements');
  });
});

describe('operator vision default', () => {
  const jsonResponse = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    });

  it('reads the enabled flag and the model, never more', () => {
    expect(
      parseVisionDefault({
        hosted_default_enabled: true,
        hosted_default_model: 'anthropic/claude-sonnet-5-5',
        secrets_available: true,
      }),
    ).toEqual({ enabled: true, model: 'anthropic/claude-sonnet-5-5' });
    expect(
      parseVisionDefault({ hosted_default_enabled: false, hosted_default_model: 'x/y' }),
    ).toEqual({ enabled: false, model: null });
    expect(parseVisionDefault({ enabled: true })).toBeNull();
    expect(parseVisionDefault('nope')).toBeNull();
    expect(SERVICE_PROBES.map((p) => p.path)).toContain(VISION_STATUS_URL);
  });

  it('treats an absent or unreachable Vision service as no answer', async () => {
    expect(
      await probeVisionDefault(async () =>
        jsonResponse({ hosted_default_enabled: true, hosted_default_model: 'fake/recover' }),
      ),
    ).toEqual({ enabled: true, model: 'fake/recover' });
    expect(await probeVisionDefault(async () => jsonResponse({}, 404))).toBeNull();
    expect(
      await probeVisionDefault(
        async () => new Response('<html>', { headers: { 'content-type': 'text/html' } }),
      ),
    ).toBeNull();
    expect(await probeVisionDefault(() => Promise.reject(new Error('offline')))).toBeNull();
  });
});
