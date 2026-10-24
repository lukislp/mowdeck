/**
 * Words for plan values: status (with the tone and glyph of `ToneBadge`), source, limits and
 * features. Statuses, sources and limits map through exhaustive switches, so a new contract value
 * fails the type check until it is translated. Feature keys are open (every module declares its
 * own): the known ones have a translation, the rest show the catalogue's English description or
 * the key.
 */
import { m } from '#lib/paraglide/messages.js';
import type { Glyph, Tone } from '#lib/schedules/labels.js';

import type { LimitKey, PlanSource, PlanStatus } from './types';

export function statusLabel(status: PlanStatus): string {
  switch (status) {
    case 'active':
      return m.plan_status_active();
    case 'trial':
      return m.plan_status_trial();
    case 'grace':
      return m.plan_status_grace();
    case 'expired':
      return m.plan_status_expired();
  }
}

export function statusTone(status: PlanStatus): { tone: Tone; glyph: Glyph } {
  switch (status) {
    case 'active':
      return { tone: 'mowing', glyph: 'check' };
    case 'trial':
      return { tone: 'returning', glyph: 'clock' };
    case 'grace':
      return { tone: 'paused', glyph: 'alert' };
    case 'expired':
      return { tone: 'error', glyph: 'alert' };
  }
}

export function sourceLabel(source: PlanSource): string {
  switch (source) {
    case 'default':
      return m.plan_source_default();
    case 'manual':
      return m.plan_source_manual();
    case 'billing':
      return m.plan_source_billing();
  }
}

export function limitLabel(key: LimitKey): string {
  switch (key) {
    case 'max_mowers':
      return m.plan_limit_mowers();
    case 'max_users':
      return m.plan_limit_users();
    case 'max_sites':
      return m.plan_limit_sites();
    case 'max_groups':
      return m.plan_limit_groups();
    case 'max_schedules':
      return m.plan_limit_schedules();
    case 'history_days':
      return m.plan_limit_history_days();
  }
}

/** Is `key` one of the plan limits? */
export function isLimitKey(key: string): key is LimitKey {
  return [
    'max_mowers',
    'max_users',
    'max_sites',
    'max_groups',
    'max_schedules',
    'history_days',
  ].includes(key);
}

/** The word of a feature key; unknown keys fall back to `fallback` (the catalogue) or the key. */
export function featureLabel(key: string, fallback?: string | null): string {
  switch (key) {
    case '*':
      return m.plan_feature_all();
    case 'view':
      return m.plan_feature_view();
    case 'control':
      return m.plan_feature_control();
    case 'emergency_stop':
      return m.plan_feature_emergency_stop();
    case 'schedules':
      return m.plan_feature_schedules();
    case 'notifications':
      return m.plan_feature_notifications();
    case 'notifications.push':
      return m.plan_feature_notifications_push();
    case 'manual_drive':
      return m.plan_feature_manual_drive();
    case 'home_assistant':
      return m.plan_feature_home_assistant();
    case 'map':
      return m.plan_feature_map();
    case 'health':
      return m.plan_feature_health();
    case 'camera':
      return m.plan_feature_camera();
    case 'reports':
      return m.plan_feature_reports();
    case 'fleet.sites':
      return m.plan_feature_fleet_sites();
    case 'fleet.groups':
      return m.plan_feature_fleet_groups();
    case 'fleet.bulk_commands':
      return m.plan_feature_fleet_bulk_commands();
    case 'api_tokens.scoped':
      return m.plan_feature_api_tokens_scoped();
    default:
      return fallback || key;
  }
}
