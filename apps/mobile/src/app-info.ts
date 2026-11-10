// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * What the About page shows about this build: the version and build number the native shell
 * reports (`@capacitor/app` `App.getInfo()`: `CFBundleShortVersionString` and `CFBundleVersion`
 * on iOS, `versionName` and `versionCode` on Android), and the source code of that version.
 * Pure: the page passes the plugin call in, so a plain browser (which has no native shell) and
 * the tests get the same fallback.
 */
import { cleanVersion, displayUrl, sourceCodeUrl, versionLabel } from './source-link';

export interface AppInfo {
  /** Marketing version (`0.3.12`), null when the shell reported none. */
  version: string | null;
  /** Build number (`312`), null when the shell reported none. */
  build: string | null;
}

export const UNKNOWN_APP: AppInfo = Object.freeze({ version: null, build: null });

const BUILD_PATTERN = /^[0-9A-Za-z][0-9A-Za-z.+-]{0,31}$/;

function cleanBuild(value: unknown): string | null {
  const text = typeof value === 'number' ? String(value) : typeof value === 'string' ? value : '';
  return BUILD_PATTERN.test(text.trim()) ? text.trim() : null;
}

/** Read the shell's app info; never throws (the web build of the plugin has no `getInfo`). */
export async function readAppInfo(
  getInfo: () => Promise<{ version?: unknown; build?: unknown }>,
): Promise<AppInfo> {
  try {
    const info = await getInfo();
    return { version: cleanVersion(info.version), build: cleanBuild(info.build) };
  } catch {
    return UNKNOWN_APP;
  }
}

export interface AppDescription {
  /** `v0.3.12`, or null when this is not the installed app (no native shell). */
  version: string | null;
  build: string | null;
  /** The source code of exactly this version; the repository itself without a version. */
  sourceUrl: string;
  sourceLabel: string;
  /** Whether `sourceUrl` points at this version (else at the project). */
  sourceIsVersion: boolean;
}

export function describeApp(info: AppInfo): AppDescription {
  const sourceUrl = sourceCodeUrl(info.version);
  return {
    version: versionLabel(info.version),
    build: info.build,
    sourceUrl,
    sourceLabel: displayUrl(sourceUrl),
    sourceIsVersion: sourceUrl !== sourceCodeUrl(null),
  };
}
