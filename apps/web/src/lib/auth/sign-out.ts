// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * Leaving the app: the one sequence for "Sign out" and for "Delete me" (ADR 0082), so both hand
 * over the same way.
 *
 * 1. The app shell first (`nativeShell.signingOut()`): it removes this device's push token and
 *    ends the widget credential while the session may still do so, and forgets them on the
 *    device. After "delete me" the server has already deleted both; the calls fail and the
 *    device still forgets them. Inert in a browser.
 * 2. A demo visitor who leaves on purpose gets no fresh sandbox by itself (ADR 0076).
 * 3. The session ends here and at Identity; `reason` is what the login page says.
 * 4. The login page.
 */
import { goto } from '$app/navigation';

import { session, type SessionEnd } from '#lib/auth/session.svelte.js';
import { forgetVisitor } from '#lib/demo/sandbox.js';
import { nativeShell } from '#lib/native/runtime.js';

export interface SignOutDeps {
  handOver: () => Promise<void>;
  forgetVisitor: () => void;
  logout: (reason: SessionEnd | null) => Promise<void>;
  toLogin: () => Promise<void>;
}

export const browserSignOut: SignOutDeps = {
  handOver: () => nativeShell.signingOut(),
  forgetVisitor,
  logout: (reason) => session.logout(reason),
  toLogin: () => goto('/login', { replace: true }),
};

/** Leave the app (see the module documentation). */
export async function signOut(
  reason: SessionEnd | null = null,
  deps: SignOutDeps = browserSignOut,
): Promise<void> {
  await deps.handOver().catch(() => undefined);
  deps.forgetVisitor();
  await deps.logout(reason);
  await deps.toLogin();
}
