import { m } from '$lib/paraglide/messages';

import type { AppModule } from '../types';

/** Reports per site and period with CSV export, built on Telemetry's day totals of the mowers
 * the user may view (Telemetry announces the ui_area). */
export default {
  id: 'reports',
  route: '/reports',
  label: () => m.nav_reports(),
  icon: 'chart',
  requiredPermission: 'mower.view',
  order: 40,
} satisfies AppModule;
