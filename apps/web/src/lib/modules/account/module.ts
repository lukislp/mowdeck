// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { m } from '#lib/paraglide/messages.js';

import type { AppModule } from '../types';

export default {
  id: 'account',
  route: '/account',
  label: () => m.nav_account(),
  icon: 'user',
  requiredPermission: null,
  order: 5,
  core: true,
  placement: 'settings',
  section: 'profile',
} satisfies AppModule;
