import { m } from '#lib/paraglide/messages.js';

import type { AppModule } from '../types';

/** Mowing schedules with rain pause. Whoever views a mower sees its schedules (the Scheduler
 * checks `can_view` on each target), so the module follows `mower.view`; editing controls are
 * shown per target after `POST /me/permissions` (the Scheduler announces the ui_area). */
export default {
  id: 'schedules',
  route: '/schedules',
  label: () => m.nav_schedules(),
  icon: 'calendar',
  requiredPermission: 'mower.view',
  order: 15,
} satisfies AppModule;
