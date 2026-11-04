// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

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
