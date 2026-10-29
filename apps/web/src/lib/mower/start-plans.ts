/**
 * Starts without map areas (ADR 0066 amendment): a mower whose connection knows no area geometry
 * declares the starts it offers in `capabilities.start_plans` (start now, a zone of its own, the
 * edge, its own schedule). The mower page then offers exactly those behind its Start button, and a
 * pick sends `start_mow {plan_ref}`. Decided from the capabilities alone, never from the vendor;
 * pure and unit tested (`start-plans.test.ts`).
 */
import type { components } from '#lib/generated/telemetry.js';

import { m } from '#lib/paraglide/messages.js';

export type StartPlan = components['schemas']['StartPlan'];
export type StartPlanKind = StartPlan['kind'];

const KINDS = new Set<string>(['now', 'zone', 'edge', 'schedule']);
const REF = /^[a-z][a-z0-9_]{0,31}(:[A-Za-z0-9_.-]{1,64})?$/;

/**
 * The declared start plans, checked: unknown kinds, bad references and repeats are dropped (the
 * page never offers a start it cannot label or send). Null or none give [].
 */
export function startPlans(capabilities: object | null | undefined): StartPlan[] {
  const raw = (capabilities as { start_plans?: unknown } | null | undefined)?.start_plans;
  if (!Array.isArray(raw)) return [];
  const seen = new Set<string>();
  const plans: StartPlan[] = [];
  for (const item of raw as Partial<StartPlan>[]) {
    if (!item || typeof item.ref !== 'string' || !REF.test(item.ref) || seen.has(item.ref))
      continue;
    if (typeof item.kind !== 'string' || !KINDS.has(item.kind)) continue;
    seen.add(item.ref);
    plans.push({
      ref: item.ref,
      kind: item.kind,
      name: typeof item.name === 'string' && item.name.trim() ? item.name.trim() : null,
    });
  }
  return plans;
}

/**
 * How the Start button starts this mower: on its map areas (the start sheet), through its declared
 * plans (one plan: sent at once; several: a choice), or not at all (no areas and no plans yet).
 */
export type StartRoute = 'areas' | 'plan' | 'choice' | 'none';

export function startRoute(hasAreas: boolean, plans: readonly StartPlan[]): StartRoute {
  if (hasAreas) return 'areas';
  if (plans.length === 1) return 'plan';
  return plans.length > 1 ? 'choice' : 'none';
}

/** The label of a plan in the choice; a zone shows the mower's own name of it. */
export function planLabel(plan: StartPlan): string {
  switch (plan.kind) {
    case 'now':
      return m.start_plan_now();
    case 'zone':
      return plan.name ? m.start_plan_zone({ name: plan.name }) : m.start_plan_zone_unnamed();
    case 'edge':
      return m.start_plan_edge();
    case 'schedule':
      return m.start_plan_schedule();
  }
}

/** The command a pick sends. */
export function planCommand(plan: StartPlan): { kind: 'start_mow'; plan_ref: string } {
  return { kind: 'start_mow', plan_ref: plan.ref };
}
