// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * Where the first-launch screen's help and privacy links point. They are repeated as plain
 * `href`s in `index.html` (the page has no script-built links); `project-links.test.ts` keeps
 * the two in step. Off-app URLs open in the system browser (Capacitor's default navigation
 * policy; the shell allows navigation only to the user's own server).
 */
export const PROJECT_URL = 'https://github.com/lukislp/mowdeck';

/**
 * The app's privacy policy, published by the developer (App Store Connect's "Privacy Policy
 * URL" is the English one). The policy says operators are responsible for their own servers.
 * One place for the URLs: both pages build their privacy link from here.
 */
export const PRIVACY_POLICY_URLS = {
  de: 'https://legal.lktec.org/mowdeck/',
  en: 'https://legal.lktec.org/en/mowdeck/',
} as const;

/** German for a German UI, English for everything else (as the page texts do). */
export function privacyPolicyUrl(language: string | undefined): string {
  return language?.toLowerCase().startsWith('de') ? PRIVACY_POLICY_URLS.de : PRIVACY_POLICY_URLS.en;
}

/** Point a privacy link at the policy in the language in use. */
export function applyPrivacyLink(anchor: HTMLAnchorElement | null, language: string): void {
  if (anchor) anchor.href = privacyPolicyUrl(language);
}
