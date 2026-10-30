import { describe, expect, it } from 'vitest';

import { m } from '#lib/paraglide/messages.js';

import { planCommand, planLabel, startPlans, startRoute } from './start-plans';

const DECLARED = [
  { ref: 'now', kind: 'now' },
  { ref: 'zone:1', kind: 'zone', name: ' Back lawn ' },
  { ref: 'edge', kind: 'edge' },
  { ref: 'schedule', kind: 'schedule' },
];

describe('start plans', () => {
  it('takes the declared plans and drops what it cannot label or send', () => {
    const plans = startPlans({
      start_plans: [
        ...DECLARED,
        { ref: 'now', kind: 'now' },
        { ref: 'Bad Ref', kind: 'now' },
        { ref: 'later', kind: 'warp' },
        null,
      ],
    });
    expect(plans.map((p) => p.ref)).toEqual(['now', 'zone:1', 'edge', 'schedule']);
    expect(plans[1]?.name).toBe('Back lawn');
    expect(startPlans(null)).toEqual([]);
    expect(startPlans({ start_plans: 'x' })).toEqual([]);
    expect(startPlans({})).toEqual([]);
  });

  it('routes the Start button by map areas first, then by plans', () => {
    const plans = startPlans({ start_plans: DECLARED });
    expect(startRoute(true, plans)).toBe('areas');
    expect(startRoute(false, plans)).toBe('choice');
    expect(startRoute(false, plans.slice(0, 1))).toBe('plan');
    expect(startRoute(false, [])).toBe('none');
  });

  it('labels every kind neutrally and sends the reference', () => {
    const [now, zone, edge, schedule] = startPlans({ start_plans: DECLARED });
    expect(planLabel(now!)).toBe(m.start_plan_now());
    expect(planLabel(zone!)).toBe(m.start_plan_zone({ name: 'Back lawn' }));
    expect(planLabel({ ref: 'zone:2', kind: 'zone', name: null })).toBe(
      m.start_plan_zone_unnamed(),
    );
    expect(planLabel(edge!)).toBe(m.start_plan_edge());
    expect(planLabel(schedule!)).toBe(m.start_plan_schedule());
    expect(planCommand(zone!)).toEqual({ kind: 'start_mow', plan_ref: 'zone:1' });
  });
});
