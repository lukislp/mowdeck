// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import ts from 'typescript';
import { describe, expect, it } from 'vitest';

import { PROJECT_URL } from './project-links';
import {
  cleanVersion,
  displayUrl,
  LICENSE_NAME,
  LICENSE_SHORT,
  LICENSE_URL,
  releaseRef,
  sourceCodeUrl,
  versionLabel,
} from './source-link';

/**
 * The web app's module (`apps/web/src/lib/legal/source.ts`), compiled on the spot: importing it
 * would pull in the web workspace's generated tsconfig, and the two pieces of code are
 * deliberately separate (see source-link.ts).
 */
interface WebSource {
  OFFICIAL_REPOSITORY: string;
  LICENSE_SHORT: string;
  LICENSE_NAME: string;
  LICENSE_URL: string;
  releaseRef: (version: string | null) => string | null;
  sourceCodeUrl: (source: { repository: string; version: string | null; ref: null }) => string;
  versionLabel: (source: {
    repository: string;
    version: string | null;
    ref: null;
  }) => string | null;
  displayUrl: (url: string) => string;
}

async function loadWebSource(): Promise<WebSource> {
  const path = resolve(import.meta.dirname, '../../web/src/lib/legal/source.ts');
  const { outputText } = ts.transpileModule(readFileSync(path, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  const url = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`;
  return (await import(/* @vite-ignore */ url)) as WebSource;
}

const web = await loadWebSource();

const VERSIONS = [
  '0.3.12',
  'v0.3.12',
  '1.0.0',
  '10.20.30',
  '1.2.3-rc.1',
  '1.2.3+build.5',
  '1.2.3-rc.1+build.5',
  ' 0.1.0 ',
  '1.2',
  '1',
  'dev',
  'main',
  '2026.11.1',
  '',
  null,
];

function webInput(version: string | null) {
  return { repository: web.OFFICIAL_REPOSITORY, version, ref: null };
}

describe('the source link of the app equals the web app', () => {
  it('points at the official repository under the same licence', () => {
    expect(PROJECT_URL).toBe(web.OFFICIAL_REPOSITORY);
    expect(LICENSE_NAME).toBe(web.LICENSE_NAME);
    expect(LICENSE_SHORT).toBe(web.LICENSE_SHORT);
    expect(LICENSE_URL).toBe(web.LICENSE_URL);
  });

  it.each(VERSIONS)('for the version %j', (version) => {
    expect(releaseRef(version)).toBe(web.releaseRef(version));
    expect(versionLabel(version)).toBe(web.versionLabel(webInput(version)));
    expect(sourceCodeUrl(version)).toBe(web.sourceCodeUrl(webInput(version)));
  });

  it('displays an address the same way', () => {
    const url = sourceCodeUrl('1.2.3');
    expect(displayUrl(url)).toBe(web.displayUrl(url));
    expect(displayUrl(url)).toBe('github.com/lukislp/mowdeck/tree/v1.2.3');
  });
});

describe('sourceCodeUrl', () => {
  it('names the tag of a release', () => {
    expect(sourceCodeUrl('0.3.12')).toBe('https://github.com/lukislp/mowdeck/tree/v0.3.12');
    expect(sourceCodeUrl('v0.3.12')).toBe('https://github.com/lukislp/mowdeck/tree/v0.3.12');
  });

  it('falls back to the repository without a release version', () => {
    expect(sourceCodeUrl(null)).toBe('https://github.com/lukislp/mowdeck');
    expect(sourceCodeUrl('dev')).toBe('https://github.com/lukislp/mowdeck');
  });

  it('accepts another repository and trims it like the web app', () => {
    expect(sourceCodeUrl('1.0.0', 'https://example.org/fork.git/')).toBe(
      'https://example.org/fork/tree/v1.0.0',
    );
  });
});

describe('cleanVersion', () => {
  it('keeps plain versions and drops junk', () => {
    expect(cleanVersion(' 0.3.12 ')).toBe('0.3.12');
    expect(cleanVersion('')).toBeNull();
    expect(cleanVersion(null)).toBeNull();
    expect(cleanVersion(312)).toBeNull();
    expect(cleanVersion('1.0 <script>')).toBeNull();
    expect(cleanVersion('x'.repeat(65))).toBeNull();
  });
});
