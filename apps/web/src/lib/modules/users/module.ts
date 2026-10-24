import { m } from '#lib/paraglide/messages.js';

import type { AppModule } from '../types';

export default {
  id: 'users',
  route: '/settings/users',
  label: () => m.nav_users(),
  icon: 'users',
  requiredPermission: 'users.manage',
  order: 50,
  placement: 'settings',
  section: 'users',
} satisfies AppModule;
