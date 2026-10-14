import { m } from '$lib/paraglide/messages';

import type { AppModule } from '../types';

/** Linked Mammotion accounts belong to the fleet area: shown with `fleet.manage` (Fleet checks
 * `can_manage_mowers` on the account again), no ui_area of their own. */
export default {
  id: 'linked-accounts',
  route: '/settings/linked-accounts',
  label: () => m.nav_linked_accounts(),
  icon: 'link',
  requiredPermission: 'fleet.manage',
  order: 58,
  core: true,
  placement: 'settings',
  section: 'linked-accounts',
} satisfies AppModule;
