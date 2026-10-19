/**
 * Licence guard of the web bundle (two Vite plugins, build only).
 *
 * * `mowdeck:agora-sdk`: the Agora Web SDK (`agora-rtc-sdk-ng`, `@agora-js/*`) is proprietary
 *   and is never part of a published image. Unless `MOWDECK_WEB_AGORA_SDK=include` (an operator's
 *   own build), the camera factory's dynamic `import('./agora-player')` resolves to
 *   `agora-player.excluded.ts`, and any other import of an Agora package fails the build. The
 *   relay player (WHEP, ADR 0045) is unaffected.
 * * `mowdeck:third-party-licences`: lists every npm package that ends up in the client bundle
 *   (the chunks' module ids), checks its declared licence against an allow list and writes
 *   `THIRD-PARTY-LICENSES.txt` (name, version, licence and licence text of each package) and
 *   `third-party-licences.json` next to the app, so the notices travel with the code. A
 *   package with a licence outside the list, without one, or an Agora package in a build without
 *   the SDK fails the build.
 */
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

import type { Plugin } from 'vite';

/** npm package names of the Agora Web SDK (and anything else published by Agora). */
const AGORA_NAME = /^(@agora-js\/.+|agora(-.+)?)$/i;

/** SPDX identifiers that may be redistributed in the AGPL-3.0 web bundle. */
export const ALLOWED_LICENCES: ReadonlySet<string> = new Set([
  '0BSD',
  'Apache-2.0',
  'BlueOak-1.0.0',
  'BSD-2-Clause',
  'BSD-3-Clause',
  'CC0-1.0',
  'ISC',
  'MIT',
  'MIT-0',
  'Unlicense',
]);

/** Whether `name` is an Agora package. */
export function isAgoraPackage(name: string): boolean {
  return AGORA_NAME.test(name);
}

/** The bare package name of an import specifier (`@scope/pkg/sub` -> `@scope/pkg`), or null. */
export function packageOfSpecifier(source: string): string | null {
  if (source.startsWith('.') || source.startsWith('/') || source.startsWith('\0')) return null;
  const parts = source.split('/');
  if (source.startsWith('@')) return parts.length > 1 ? `${parts[0]}/${parts[1]}` : null;
  return parts[0] ?? null;
}

/** Whether a declared SPDX expression is acceptable (every AND term, at least one OR term). */
export function licenceAllowed(expression: string | null | undefined): boolean {
  if (!expression) return false;
  const text = expression.replace(/[()]/g, ' ').trim();
  if (/\bOR\b/.test(text)) return text.split(/\s+OR\s+/).some((term) => licenceAllowed(term));
  return text.split(/\s+AND\s+/).every((term) => ALLOWED_LICENCES.has(term.trim()));
}

/** The package root and name of a module id inside node_modules, or null. */
export function packageOfModule(id: string): { root: string; name: string } | null {
  if (id.startsWith('\0')) return null;
  const path = id.split('?')[0]!.replace(/\\/g, '/');
  const marker = '/node_modules/';
  const at = path.lastIndexOf(marker);
  if (at < 0) return null;
  const rest = path.slice(at + marker.length).split('/');
  const name = rest[0]?.startsWith('@') ? `${rest[0]}/${rest[1]}` : rest[0];
  if (!name) return null;
  return { root: path.slice(0, at + marker.length + name.length), name };
}

/** One bundled third-party package. */
export interface BundledPackage {
  name: string;
  version: string;
  license: string | null;
  licenseText: string | null;
}

function licenceOf(manifest: Record<string, unknown>): string | null {
  const field = manifest.license;
  if (typeof field === 'string') return field;
  if (field && typeof field === 'object' && 'type' in field) return String(field.type);
  const list = manifest.licenses;
  if (Array.isArray(list) && list.length > 0) {
    return list.map((entry) => String((entry as { type?: unknown }).type)).join(' OR ');
  }
  return null;
}

function readPackage(root: string, name: string): BundledPackage {
  const manifest = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8')) as Record<
    string,
    unknown
  >;
  const files = existsSync(root)
    ? readdirSync(root).filter((file) => /^(licen[cs]e|copying|notice)/i.test(file))
    : [];
  const licenseText = files.length
    ? files.map((file) => readFileSync(join(root, file), 'utf8').trim()).join('\n\n')
    : null;
  return {
    name,
    version: String(manifest.version ?? ''),
    license: licenceOf(manifest),
    licenseText,
  };
}

/** Problems of a bundle's package list; empty when it may be published. */
export function bundleProblems(packages: readonly BundledPackage[], agora: boolean): string[] {
  const problems: string[] = [];
  for (const pkg of packages) {
    if (isAgoraPackage(pkg.name)) {
      if (!agora) problems.push(`${pkg.name}@${pkg.version}: Agora package in the bundle`);
      continue;
    }
    if (!licenceAllowed(pkg.license)) {
      problems.push(`${pkg.name}@${pkg.version}: licence ${pkg.license ?? 'missing'} not allowed`);
    }
  }
  return problems;
}

/** The notice file: one section per package, sorted by name. */
export function noticeText(packages: readonly BundledPackage[]): string {
  const header =
    'Third-party software in the MowDeck web app (generated at build time).\n' +
    'MowDeck itself: AGPL-3.0-only, source at https://github.com/lukislp/mowdeck\n';
  const sections = packages.map(
    (pkg) =>
      `${'-'.repeat(78)}\n${pkg.name}@${pkg.version} (${pkg.license ?? 'no licence declared'})\n\n` +
      `${pkg.licenseText ?? '(the package ships no licence file)'}\n`,
  );
  return [header, ...sections].join('\n');
}

/** Whether this build includes the Agora Web SDK (`MOWDECK_WEB_AGORA_SDK`). */
export function agoraIncluded(value: string | undefined): boolean {
  if (value === undefined || value === '' || value === 'exclude') return false;
  if (value === 'include') return true;
  throw new Error(`MOWDECK_WEB_AGORA_SDK must be "include" or "exclude", not "${value}"`);
}

/** The two plugins; `agora` from {@link agoraIncluded}. */
export function releaseLicences(agora: boolean): Plugin[] {
  return [
    {
      name: 'mowdeck:agora-sdk',
      apply: 'build',
      enforce: 'pre',
      async resolveId(source, importer) {
        if (agora) return null;
        if (source === './agora-player' && importer) {
          const from = importer.replace(/\\/g, '/');
          if (from.endsWith('/src/lib/camera/factory.ts')) {
            return this.resolve('./agora-player.excluded', importer, { skipSelf: true });
          }
        }
        const name = packageOfSpecifier(source);
        if (name && isAgoraPackage(name)) {
          this.error(
            `${name} is imported by ${importer ?? '?'}: the Agora Web SDK is never part of a ` +
              'published build (set MOWDECK_WEB_AGORA_SDK=include only for your own image)',
          );
        }
        return null;
      },
    },
    {
      name: 'mowdeck:third-party-licences',
      apply: 'build',
      applyToEnvironment: (environment) => environment.name === 'client',
      generateBundle(_options, bundle) {
        const roots = new Map<string, string>();
        for (const output of Object.values(bundle)) {
          if (output.type !== 'chunk') continue;
          for (const id of output.moduleIds) {
            const pkg = packageOfModule(id);
            if (pkg) roots.set(pkg.name, pkg.root);
          }
        }
        const packages = [...roots.entries()]
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([name, root]) => readPackage(root, name));
        const problems = bundleProblems(packages, agora);
        if (problems.length > 0) {
          this.error(`licence check of the web bundle failed:\n  ${problems.join('\n  ')}`);
        }
        this.emitFile({
          type: 'asset',
          fileName: 'THIRD-PARTY-LICENSES.txt',
          source: noticeText(packages),
        });
        this.emitFile({
          type: 'asset',
          fileName: 'third-party-licences.json',
          source: `${JSON.stringify(
            {
              agoraWebSdk: agora,
              packages: packages.map(({ name, version, license }) => ({ name, version, license })),
            },
            null,
            2,
          )}\n`,
        });
      },
    },
  ];
}
