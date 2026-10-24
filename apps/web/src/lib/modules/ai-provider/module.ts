import { m } from '#lib/paraglide/messages.js';

import type { AppModule } from '../types';

/** The user's own vision model for AI assist; the Vision service announces the ui_area. */
export default {
  id: 'ai-provider',
  route: '/settings/ai-provider',
  label: () => m.nav_ai_provider(),
  icon: 'link',
  requiredPermission: 'mower.drive',
  order: 55,
  placement: 'settings',
  section: 'ai-provider',
} satisfies AppModule;
