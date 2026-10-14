/** Words for the operator console: account kinds and statuses (Identity), probe verdicts. */
import { m } from '$lib/paraglide/messages';

import type { ProbeVerdict } from './system';

/** Identity's account kind; an unknown kind shows as it is. */
export function accountKindLabel(kind: string): string {
  switch (kind) {
    case 'household':
      return m.op_kind_household();
    case 'business':
      return m.op_kind_business();
    default:
      return kind;
  }
}

/** Identity's account status; an unknown status shows as it is. */
export function accountStatusLabel(status: string): string {
  switch (status) {
    case 'active':
      return m.op_account_active();
    case 'deleting':
      return m.op_account_deleting();
    default:
      return status;
  }
}

export function verdictLabel(verdict: ProbeVerdict): string {
  switch (verdict) {
    case 'up':
      return m.op_probe_up();
    case 'degraded':
      return m.op_probe_degraded();
    case 'down':
      return m.op_probe_down();
  }
}
