import { m } from '$lib/paraglide/messages';

import type { AppModule } from '../types';

/** Inbox and notification settings: every viewer of a mower gets notifications about it, so
 * the module follows `mower.view` (the Notifications service announces the ui_area). */
export default {
  id: 'notifications',
  route: '/notifications',
  label: () => m.nav_notifications(),
  icon: 'bell',
  requiredPermission: 'mower.view',
  order: 20,
} satisfies AppModule;
