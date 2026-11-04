// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

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
