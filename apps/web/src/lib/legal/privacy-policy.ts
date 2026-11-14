// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * The MowDeck app's privacy policy, published by the developer on legal.lktec.org (App Store
 * guideline 5.1.1(i): easy to reach inside the app). It is not the template an operator
 * completes for their own server (`/legal/privacy`). The native shell keeps the same two URLs
 * in `apps/mobile/src/project-links.ts` for its own screens; this is the one place for the web
 * UI, which is what the app shows after sign-in.
 */
export const PRIVACY_POLICY_URLS = {
  de: 'https://legal.lktec.org/mowdeck/',
  en: 'https://legal.lktec.org/en/mowdeck/',
} as const;

/** German for a German UI, English for every other language. */
export function privacyPolicyUrl(language: string | undefined): string {
  return language?.toLowerCase().startsWith('de') ? PRIVACY_POLICY_URLS.de : PRIVACY_POLICY_URLS.en;
}
