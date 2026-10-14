import { m } from '$lib/paraglide/messages';

import type { AppModule } from '../types';

/** API tokens belong to the users area (same permission, no ui_area of their own). */
export default {
  id: 'api-tokens',
  route: '/settings/api-tokens',
  label: () => m.nav_api_tokens(),
  icon: 'key',
  requiredPermission: 'users.manage',
  order: 60,
  core: true,
  placement: 'settings',
  section: 'api-tokens',
} satisfies AppModule;
