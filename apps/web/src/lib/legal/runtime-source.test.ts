// @vitest-environment node
/**
 * The `source` of /config.json as nginx/40-mowdeck-runtime-config.sh renders it from the image's
 * build values (AGPL-3.0 section 13): passed through when valid, left out otherwise.
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { parseConfig } from '#lib/config.js';

import { OFFICIAL_REPOSITORY, sourceCodeUrl } from './source';

const script = join(process.cwd(), 'nginx', '40-mowdeck-runtime-config.sh');
const hasSh = spawnSync('sh', ['-c', 'exit 0']).status === 0;

function render(env: Record<string, string>): { source: Record<string, string> } {
  const dir = mkdtempSync(join(tmpdir(), 'mowdeck-source-'));
  const template = join(dir, 'template.conf');
  writeFileSync(template, 'add_header Content-Security-Policy "default-src \'self\'" always;\n');
  const result = spawnSync('sh', [script], {
    env: {
      PATH: process.env.PATH ?? '',
      MOWDECK_CONFIG_DIR: join(dir, 'out'),
      MOWDECK_CSP_TEMPLATE: template,
      ...env,
    },
    encoding: 'utf8',
  });
  expect(result.status, result.stderr).toBe(0);
  return JSON.parse(readFileSync(join(dir, 'out', 'config.json'), 'utf8'));
}

describe.skipIf(!hasSh)('rendered source', () => {
  it('passes the build values of a release through', () => {
    const config = render({
      MOWDECK_VERSION: '0.3.0',
      MOWDECK_SOURCE_URL: 'https://github.com/someone/fork',
      MOWDECK_SOURCE_REF: 'v0.3.0',
    });
    expect(config.source).toEqual({
      url: 'https://github.com/someone/fork',
      version: '0.3.0',
      ref: 'v0.3.0',
    });
    expect(sourceCodeUrl(parseConfig(config).source)).toBe(
      'https://github.com/someone/fork/tree/v0.3.0',
    );
  });

  it('leaves out what is not a plain URL, version or ref', () => {
    const config = render({
      MOWDECK_VERSION: '<b>1</b>',
      MOWDECK_SOURCE_URL: 'https://example.org/a"b',
      MOWDECK_SOURCE_REF: '../main',
    });
    expect(config.source).toEqual({ url: '', version: '', ref: '' });
    expect(sourceCodeUrl(parseConfig(config).source)).toBe(OFFICIAL_REPOSITORY);
  });

  it('is a development build without build values', () => {
    expect(render({}).source).toEqual({ url: '', version: '', ref: '' });
  });
});
