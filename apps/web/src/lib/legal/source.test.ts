import { describe, expect, it } from 'vitest';

import {
  DEFAULT_SOURCE,
  OFFICIAL_REPOSITORY,
  displayUrl,
  parseSource,
  releaseRef,
  sourceCodeUrl,
  versionLabel,
  type SourceInfo,
} from './source';

const build = (over: Partial<SourceInfo>): SourceInfo => ({ ...DEFAULT_SOURCE, ...over });

describe('releaseRef', () => {
  it.each([
    ['1.2.3', 'v1.2.3'],
    ['v0.4.0', 'v0.4.0'],
    ['0.1.0-rc.1', 'v0.1.0-rc.1'],
    ['dev', null],
    ['1.2', null],
    [null, null],
  ])('%s -> %s', (version, ref) => {
    expect(releaseRef(version)).toBe(ref);
  });
});

describe('sourceCodeUrl', () => {
  it('links a release to its tag', () => {
    expect(sourceCodeUrl(build({ version: '0.3.0' }))).toBe(`${OFFICIAL_REPOSITORY}/tree/v0.3.0`);
  });

  it('links a development build to the repository', () => {
    expect(sourceCodeUrl(DEFAULT_SOURCE)).toBe(OFFICIAL_REPOSITORY);
    expect(sourceCodeUrl(build({ version: 'dev' }))).toBe(OFFICIAL_REPOSITORY);
  });

  it('prefers the ref of the build (a test build links to its commit)', () => {
    const sha = '389d704b2a5c0d6c0f1e1a2b3c4d5e6f70809010';
    expect(sourceCodeUrl(build({ version: '0.1.0-rc.1', ref: sha }))).toBe(
      `${OFFICIAL_REPOSITORY}/tree/${sha}`,
    );
  });

  it("follows a fork's repository and a template for other hosts", () => {
    expect(
      sourceCodeUrl({
        repository: 'https://git.example.org/me/mowdeck.git/',
        version: '1.0.0',
        ref: null,
      }),
    ).toBe('https://git.example.org/me/mowdeck/tree/v1.0.0');
    const template = 'https://codeberg.org/me/mowdeck/src/tag/{ref}';
    expect(sourceCodeUrl({ repository: template, version: '1.0.0', ref: null })).toBe(
      'https://codeberg.org/me/mowdeck/src/tag/v1.0.0',
    );
    expect(sourceCodeUrl({ repository: template, version: null, ref: null })).toBe(
      'https://codeberg.org/me/mowdeck/src/tag/HEAD',
    );
  });
});

describe('parseSource', () => {
  it('falls back to the official repository', () => {
    expect(parseSource(undefined)).toBe(DEFAULT_SOURCE);
    expect(parseSource({ url: '', version: ' ', ref: '' })).toBe(DEFAULT_SOURCE);
    expect(parseSource({ url: 'javascript:alert(1)', version: '1.0.0' })).toEqual(
      build({ version: '1.0.0' }),
    );
    expect(parseSource({ url: 'https://x.example/"a' }).repository).toBe(OFFICIAL_REPOSITORY);
  });

  it('drops refs and versions that cannot be part of a link', () => {
    const parsed = parseSource({ version: '<script>', ref: '../../evil' });
    expect(parsed.version).toBeNull();
    expect(parsed.ref).toBeNull();
  });
});

describe('labels', () => {
  it('shows a release with its v and nothing for a development build', () => {
    expect(versionLabel(build({ version: '0.3.0' }))).toBe('v0.3.0');
    expect(versionLabel(build({ version: 'nightly-7' }))).toBe('nightly-7');
    expect(versionLabel(DEFAULT_SOURCE)).toBeNull();
  });

  it('shows the address without the scheme', () => {
    expect(displayUrl('https://github.com/lukislp/mowdeck/tree/v1.0.0')).toBe(
      'github.com/lukislp/mowdeck/tree/v1.0.0',
    );
  });
});
