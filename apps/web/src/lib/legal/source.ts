/**
 * Where users get the source code of the running version (AGPL-3.0 section 13, LICENSING.md).
 *
 * The web image bakes `MOWDECK_VERSION`, `MOWDECK_SOURCE_URL` and `MOWDECK_SOURCE_REF` in at
 * build time (the operator may override them at runtime); nginx renders them into
 * `/config.json` as `source`. The link is built exactly like the services' `X-Source-Code`
 * header (`mowdeck_service_kit.source`): `<repository>/tree/<ref>` with the ref of the build or
 * `v<version>` of a release, a `{ref}` template for hosts other than GitHub, and the repository
 * itself for a development build.
 */

export const OFFICIAL_REPOSITORY = 'https://github.com/lukislp/mowdeck';
export const LICENSE_ID = 'AGPL-3.0-only';
/** The licence as shown (a name, not translated). */
export const LICENSE_SHORT = 'AGPL-3.0';
export const LICENSE_NAME = 'GNU AGPL-3.0';
export const LICENSE_URL = 'https://www.gnu.org/licenses/agpl-3.0.html';

export interface SourceInfo {
  /** Repository URL, or a template with `{ref}`. */
  repository: string;
  /** Version of the build; null for a development build. */
  version: string | null;
  /** Git ref of the build (tag or commit); null: derived from the version. */
  ref: string | null;
}

export const DEFAULT_SOURCE: SourceInfo = Object.freeze({
  repository: OFFICIAL_REPOSITORY,
  version: null,
  ref: null,
});

const URL_PATTERN = /^https?:\/\/[^\s"'<>\\]+$/;
const REF_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$/;
const SEMVER = /^v?(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;
const VERSION_PATTERN = /^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$/;

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

/** Validate the untrusted `source` object of `/config.json`. */
export function parseSource(raw: unknown): SourceInfo {
  if (!raw || typeof raw !== 'object') return DEFAULT_SOURCE;
  const { url, version, ref } = raw as Record<string, unknown>;
  const repository = text(url);
  const ver = text(version);
  const gitRef = text(ref);
  const parsed: SourceInfo = {
    repository: repository && URL_PATTERN.test(repository) ? repository : OFFICIAL_REPOSITORY,
    version: ver && VERSION_PATTERN.test(ver) ? ver : null,
    ref: gitRef && REF_PATTERN.test(gitRef) && !gitRef.includes('..') ? gitRef : null,
  };
  return parsed.repository === OFFICIAL_REPOSITORY && !parsed.version && !parsed.ref
    ? DEFAULT_SOURCE
    : parsed;
}

/** The tag of a semantic version (`1.2.3` -> `v1.2.3`), null for anything else. */
export function releaseRef(version: string | null): string | null {
  const value = version?.trim() ?? '';
  return SEMVER.test(value) ? `v${value.replace(/^v/, '')}` : null;
}

function encodeRef(ref: string): string {
  return ref.split('/').map(encodeURIComponent).join('/');
}

/** The address of the source code of the build. */
export function sourceCodeUrl(source: SourceInfo): string {
  const ref = source.ref ?? releaseRef(source.version);
  if (source.repository.includes('{ref}')) {
    return source.repository.replaceAll('{ref}', encodeRef(ref ?? 'HEAD'));
  }
  const base = source.repository.replace(/\/+$/, '').replace(/\.git$/, '');
  return ref ? `${base}/tree/${encodeRef(ref)}` : base;
}

/** The version as shown: `v1.2.3`, the raw version of another scheme, or null (dev build). */
export function versionLabel(source: SourceInfo): string | null {
  if (!source.version) return null;
  return releaseRef(source.version) ?? source.version;
}

/** The address without the scheme, for display (`github.com/lukislp/mowdeck/tree/v1.2.3`). */
export function displayUrl(url: string): string {
  return url.replace(/^https?:\/\//, '');
}
