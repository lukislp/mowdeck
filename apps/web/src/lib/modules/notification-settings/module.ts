import { m } from '#lib/paraglide/messages.js';

import type { AppModule } from '../types';

/** Notification preferences and channels: a section of the settings page, enabled with the
 * inbox (the Notifications service announces `/notifications`). */
export default {
  id: 'notification-settings',
  route: '/notifications/settings',
  area: '/notifications',
  label: () => m.notif_settings_title(),
  icon: 'bell',
  requiredPermission: 'mower.view',
  order: 21,
  placement: 'settings',
  section: 'notifications',
} satisfies AppModule;
