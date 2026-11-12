// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * The tabs of the operator console. Payments (ADR 0083) exists only where Billing runs: without
 * it there is nothing to show, so the tab is not there at all (its page says so when opened by
 * its address).
 */
import type { IconName } from '@mowdeck/ui';

import { m } from '#lib/paraglide/messages.js';

export type ConsoleTabId = 'accounts' | 'plans' | 'payments' | 'system';

export interface ConsoleTab {
  id: ConsoleTabId;
  href: string;
  label: () => string;
  icon: IconName;
}

const ALL: readonly ConsoleTab[] = [
  { id: 'accounts', href: '/operator', label: () => m.op_tab_accounts(), icon: 'users' },
  { id: 'plans', href: '/operator/plans', label: () => m.op_tab_plans(), icon: 'list' },
  { id: 'payments', href: '/operator/payments', label: () => m.op_tab_payments(), icon: 'chart' },
  { id: 'system', href: '/operator/system', label: () => m.op_tab_system(), icon: 'pulse' },
];

/** The tabs in order; Payments only with `billing`. */
export function consoleTabs(billing: boolean): ConsoleTab[] {
  return ALL.filter((tab) => tab.id !== 'payments' || billing);
}

/** The tab a path belongs to. */
export function currentTab(path: string): ConsoleTabId {
  if (path.startsWith('/operator/plans')) return 'plans';
  if (path.startsWith('/operator/payments')) return 'payments';
  if (path.startsWith('/operator/system')) return 'system';
  return 'accounts';
}
