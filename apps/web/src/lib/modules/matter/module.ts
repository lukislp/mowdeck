import { m } from '#lib/paraglide/messages.js';

import type { AppModule } from '../types';

/** Matter pairing codes per mower (ADR 0063): shown with `mower.control` once the Matter server
 * announces its ui_area (Compose profile `matter`); the server checks `can_control` per mower. */
export default {
  id: 'matter',
  route: '/settings/matter',
  label: () => m.nav_matter(),
  icon: 'link',
  requiredPermission: 'mower.control',
  order: 57,
  placement: 'settings',
  section: 'matter',
} satisfies AppModule;
