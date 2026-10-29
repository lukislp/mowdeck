import { m } from '#lib/paraglide/messages.js';

import type { AppModule } from '../types';

/** The security center (ADR 0065): every user's own sessions, devices, passkeys, login
 * providers, personal access tokens and security events; right after the profile. */
export default {
  id: 'security',
  route: '/settings/security',
  label: () => m.nav_security(),
  icon: 'shield',
  requiredPermission: null,
  order: 6,
  core: true,
  placement: 'settings',
  section: 'security',
} satisfies AppModule;
