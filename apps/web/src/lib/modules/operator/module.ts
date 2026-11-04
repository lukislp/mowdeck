// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { m } from '#lib/paraglide/messages.js';

import type { AppModule } from '../types';

/** The operator console (accounts, plans, system): only for operator admins of the installation,
 * shown when `GET /identity/me/platform` says `operator_admin` (no permission key exists for it). It
 * shows no mower data anywhere. */
export default {
  id: 'operator',
  route: '/operator',
  label: () => m.nav_operator(),
  icon: 'shield',
  requiredPermission: null,
  order: 95,
  core: true,
  platformAdmin: true,
} satisfies AppModule;
