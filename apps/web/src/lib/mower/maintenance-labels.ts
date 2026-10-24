/** Words of the maintenance actions (de and en through Paraglide), exhaustive per action. */
import { m } from '#lib/paraglide/messages.js';

import { neededRight, type MaintenanceAction, type MaintenanceBlocker } from './maintenance';

interface ActionText {
  label: () => string;
  short: () => string;
  body: () => string;
  rule: () => string;
}

const TEXTS: Record<MaintenanceAction, ActionText> = {
  leave_dock: {
    label: m.maint_leave_dock,
    short: m.maint_leave_dock_short,
    body: m.maint_leave_dock_body,
    rule: m.maint_rule_leave_dock,
  },
  sync_map: {
    label: m.maint_sync_map,
    short: m.maint_sync_map_short,
    body: m.maint_sync_map_body,
    rule: m.maint_rule_sync_map,
  },
  resync_rtk: {
    label: m.maint_resync_rtk,
    short: m.maint_resync_rtk_short,
    body: m.maint_resync_rtk_body,
    rule: m.maint_rule_resync_rtk,
  },
  relocate_dock: {
    label: m.maint_relocate_dock,
    short: m.maint_relocate_dock_short,
    body: m.maint_relocate_dock_body,
    rule: m.maint_rule_relocate_dock,
  },
  restart_mower: {
    label: m.maint_restart_mower,
    short: m.maint_restart_mower_short,
    body: m.maint_restart_mower_body,
    rule: m.maint_rule_restart_mower,
  },
};

export function actionText(action: MaintenanceAction): {
  label: string;
  short: string;
  body: string;
  rule: string;
} {
  const text = TEXTS[action];
  return { label: text.label(), short: text.short(), body: text.body(), rule: text.rule() };
}

export function maintenanceBlockerText(
  blocker: MaintenanceBlocker,
  action: MaintenanceAction,
): string {
  switch (blocker) {
    case 'no_permission':
      return neededRight(action) === 'can_control'
        ? m.maint_block_no_permission_control()
        : m.maint_block_no_permission_manage();
    case 'plan':
      return m.maint_block_plan();
    case 'offline':
      return m.maint_block_offline();
    case 'busy':
      return m.maint_block_busy();
    case 'pending':
      return m.maint_block_pending();
    case 'error':
      return m.maint_block_error();
    case 'not_at_rest':
      return m.maint_block_not_at_rest();
    case 'not_docked':
      return m.maint_block_not_docked();
    case 'job':
      return m.maint_block_job();
    case 'drive_session':
      return m.maint_block_drive_session();
  }
}
