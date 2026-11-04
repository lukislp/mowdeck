// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { m } from '#lib/paraglide/messages.js';

import type { AppModule } from '../types';

/** Personal access tokens (ADR 0064): every user manages their own, so no permission is needed;
 * the section sits right after the account's API tokens. */
export default {
  id: 'api-access',
  route: '/settings/api-access',
  label: () => m.nav_api_access(),
  icon: 'key',
  requiredPermission: null,
  order: 62,
  core: true,
  placement: 'settings',
  section: 'api-access',
} satisfies AppModule;
