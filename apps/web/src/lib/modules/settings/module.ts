import { m } from '$lib/paraglide/messages';

import type { AppModule } from '../types';

/** The one settings page: account and profile, notifications, users, AI provider, linked
 * accounts and API tokens as sections (the modules with `placement: 'settings'`). Everybody has
 * at least the profile section, so the entry is core. */
export default {
  id: 'settings',
  route: '/settings',
  label: () => m.nav_settings(),
  icon: 'settings',
  requiredPermission: null,
  order: 90,
  core: true,
} satisfies AppModule;
