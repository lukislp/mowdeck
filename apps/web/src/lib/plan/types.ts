/** The Entitlements types the web app uses (from the generated OpenAPI types). */
import type { components } from '#lib/generated/entitlements.js';

type Schemas = components['schemas'];

export type ResolvedPlan = Schemas['ResolvedOut'];
export type PlanStatus = Schemas['PlanStatus'];
export type PlanSource = Schemas['PlanSource'];
export type PlanLimits = Schemas['PlanLimits'];
export type Plan = Schemas['PlanOut'];
export type PlanDetail = Schemas['PlanDetailOut'];
export type PlanIn = Schemas['PlanIn'];
export type Feature = Schemas['FeatureOut'];
export type EntitlementsAccount = Schemas['AccountOut'];
export type AccountDetail = Schemas['AccountDetailOut'];
export type Assigned = Schemas['AssignedOut'];
export type HistoryEntry = Schemas['HistoryOut'];
export type AssignmentIn = Schemas['AssignmentIn'];

/** The limits of a plan in display order (the keys of `PlanLimits`). */
export const LIMIT_KEYS = [
  'max_mowers',
  'max_users',
  'max_sites',
  'max_groups',
  'max_schedules',
  'history_days',
] as const satisfies readonly (keyof PlanLimits)[];

export type LimitKey = (typeof LIMIT_KEYS)[number];

export const PLAN_STATUSES = [
  'active',
  'trial',
  'grace',
  'expired',
] as const satisfies readonly PlanStatus[];
