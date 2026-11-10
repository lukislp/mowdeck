// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * Where the first-launch screen's help and privacy links point. They are repeated as plain
 * `href`s in `index.html` (the page has no script-built links); `project-links.test.ts` keeps
 * the two in step. Off-app URLs open in the system browser (Capacitor's default navigation
 * policy; the shell allows navigation only to the user's own server).
 */
export const PROJECT_URL = 'https://github.com/lukislp/mowdeck';

/** The privacy section of the app guide in the repository (no separate policy page exists yet). */
export const PRIVACY_URL = `${PROJECT_URL}/blob/main/docs/ops/mobile-apps.md#privacy`;
