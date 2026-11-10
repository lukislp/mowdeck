// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * The public MowDeck demo (ADR 0068): per-visitor sandboxes with simulated mowers. The first-launch
 * screen offers it as "Try the demo"; the address goes through the same check as a typed one
 * (`connect.ts`). This is the one place the app knows a developer-operated server.
 */
export const DEMO_SERVER_URL = 'https://mowdeck-demo.lktec.org';
