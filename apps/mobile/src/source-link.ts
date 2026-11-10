// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * The version and source-code conventions of the app, for the About page.
 *
 * They are the web app's (`apps/web/src/lib/legal/source.ts`, which builds the link of the
 * server's About section like the services' `X-Source-Code` header): the tag of a release is
 * `v<version>` and the source of a build is `<repository>/tree/<tag>`. The bundled pages cannot
 * import from `apps/web` and `libs/mowdeck-ui` is a Svelte component library, so the functions
 * are mirrored here for the one case the app has (the official repository, a version from the
 * native shell, no overrides), and `source-link.test.ts` runs both implementations on the same
 * inputs and fails when they differ.
 */
import { PROJECT_URL } from './project-links';

export const LICENSE_SHORT = 'AGPL-3.0';
export const LICENSE_NAME = 'GNU AGPL-3.0';
export const LICENSE_URL = 'https://www.gnu.org/licenses/agpl-3.0.html';

const SEMVER = /^v?(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;
const VERSION_PATTERN = /^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$/;

/** A version string the shell reported, or null when it is empty or has an odd shape. */
export function cleanVersion(value: unknown): string | null {
  const text = typeof value === 'string' ? value.trim() : '';
  return VERSION_PATTERN.test(text) ? text : null;
}

/** The tag of a semantic version (`1.2.3` -> `v1.2.3`), null for anything else. */
export function releaseRef(version: string | null): string | null {
  const value = version?.trim() ?? '';
  return SEMVER.test(value) ? `v${value.replace(/^v/, '')}` : null;
}

/** The address of the source code of this version (the repository itself without one). */
export function sourceCodeUrl(version: string | null, repository: string = PROJECT_URL): string {
  const ref = releaseRef(version);
  const base = repository.replace(/\/+$/, '').replace(/\.git$/, '');
  return ref ? `${base}/tree/${ref.split('/').map(encodeURIComponent).join('/')}` : base;
}

/** The version as shown: `v1.2.3`, the raw version of another scheme, or null. */
export function versionLabel(version: string | null): string | null {
  if (!version) return null;
  return releaseRef(version) ?? version;
}

/** The address without the scheme, for display (`github.com/lukislp/mowdeck/tree/v1.2.3`). */
export function displayUrl(url: string): string {
  return url.replace(/^https?:\/\//, '');
}
