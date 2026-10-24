import { m } from '#lib/paraglide/messages.js';

import type { AppModule } from '../types';

/** Fleet map, sites and groups. Everybody who views mowers sees the mowers and places they may
 * view; editing and bulk commands are checked per site, group and mower (Fleet announces the
 * ui_area). */
export default {
  id: 'fleet',
  route: '/fleet',
  label: () => m.nav_fleet(),
  icon: 'fleet',
  requiredPermission: 'mower.view',
  order: 12,
} satisfies AppModule;
