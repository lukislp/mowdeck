// @vitest-environment node
/**
 * The API explorer's switch (ADR 0072) as nginx/40-mowdeck-runtime-config.sh renders it: the
 * `apiExplorer` flag of /config.json and explorer.conf, which the /_explorer/ location includes
 * (off: `return 404;`, so none of the frame's files is served).
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { parseConfig } from '#lib/config.js';

const script = join(process.cwd(), 'nginx', '40-mowdeck-runtime-config.sh');
const hasSh = spawnSync('sh', ['-c', 'exit 0']).status === 0;

function render(env: Record<string, string>) {
  const dir = mkdtempSync(join(tmpdir(), 'mowdeck-explorer-'));
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
  return {
    config: JSON.parse(readFileSync(join(dir, 'out', 'config.json'), 'utf8')) as Record<
      string,
      unknown
    >,
    nginx: readFileSync(join(dir, 'out', 'explorer.conf'), 'utf8'),
    csp: readFileSync(join(dir, 'out', 'csp.conf'), 'utf8'),
    stderr: result.stderr,
  };
}

describe.skipIf(!hasSh)('MOWDECK_WEB_API_EXPLORER', () => {
  it('is off by default: no page, and the frame answers 404', () => {
    const envs: Record<string, string>[] = [{}, { MOWDECK_WEB_API_EXPLORER: 'false' }];
    for (const env of envs) {
      const out = render(env);
      expect(out.config.apiExplorer).toBe(false);
      expect(parseConfig(out.config).apiExplorer).toBe(false);
      expect(out.nginx.trim()).toBe('return 404;');
    }
  });

  it('turns the page and the frame on', () => {
    for (const value of ['true', 'TRUE', 'on', '1']) {
      const out = render({ MOWDECK_WEB_API_EXPLORER: value });
      expect(out.config.apiExplorer).toBe(true);
      expect(parseConfig(out.config).apiExplorer).toBe(true);
      expect(out.nginx).not.toContain('return');
    }
  });

  it('stays off for an unclear value and says so', () => {
    const out = render({ MOWDECK_WEB_API_EXPLORER: 'maybe' });
    expect(out.config.apiExplorer).toBe(false);
    expect(out.nginx.trim()).toBe('return 404;');
    expect(out.stderr).toContain('MOWDECK_WEB_API_EXPLORER');
  });

  it('says whether the image ships the notes of "Was ist neu"', () => {
    const root = mkdtempSync(join(tmpdir(), 'mowdeck-root-'));
    const without = render({ MOWDECK_WEB_ROOT: root });
    expect(without.config.whatsNew).toBe(false);
    expect(parseConfig(without.config).whatsNew).toBe(false);
    writeFileSync(join(root, 'whats-new.json'), '{"releases":[]}');
    const shipped = render({ MOWDECK_WEB_ROOT: root });
    expect(shipped.config.whatsNew).toBe(true);
    expect(parseConfig(shipped.config).whatsNew).toBe(true);
  });

  it('leaves the app policy alone', () => {
    const off = render({});
    const on = render({ MOWDECK_WEB_API_EXPLORER: 'true' });
    expect(on.csp).toBe(off.csp);
  });
});
