import { m } from '#lib/paraglide/messages.js';

import type { AppModule } from '../types';

export default {
  id: 'mowers',
  route: '/mowers',
  label: () => m.nav_mowers(),
  icon: 'mower',
  requiredPermission: 'mower.view',
  order: 10,
} satisfies AppModule;
