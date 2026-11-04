// @vitest-environment node
/**
 * HTTP Strict Transport Security as nginx/40-mowdeck-runtime-config.sh renders it: hsts.conf maps
 * the gateway's X-Forwarded-Proto to the header value, `https` to one year with subdomains (never
 * preload), everything else (plain HTTP) to an empty value, which nginx does not send.
 * MOWDECK_WEB_HSTS=false leaves only the empty value. The real nginx behaviour behind Traefik is
 * covered by tests/e2e-web/tests/security-headers-desktop.spec.ts.
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const nginxDir = join(process.cwd(), 'nginx');
const script = join(nginxDir, '40-mowdeck-runtime-config.sh');
const hasSh = spawnSync('sh', ['-c', 'exit 0']).status === 0;
const VALUE = 'max-age=31536000; includeSubDomains';

function render(env: Record<string, string>) {
  const dir = mkdtempSync(join(tmpdir(), 'mowdeck-hsts-'));
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
  return { conf: readFileSync(join(dir, 'out', 'hsts.conf'), 'utf8'), stderr: result.stderr };
}

/** The value the map gives a request with this X-Forwarded-Proto (nginx's map semantics). */
function valueFor(conf: string, proto: string): string {
  for (const line of conf.split('\n')) {
    const regex = /^\s*~\*(\S+)\s+"([^"]*)";$/.exec(line);
    if (regex && new RegExp(regex[1]!, 'i').test(proto)) return regex[2]!;
  }
  const fallback = /^\s*default\s+"([^"]*)";$/m.exec(conf);
  return fallback![1]!;
}

describe.skipIf(!hasSh)('MOWDECK_WEB_HSTS', () => {
  it('is on by default for HTTPS, one year with subdomains and without preload', () => {
    const envs: Record<string, string>[] = [
      {},
      { MOWDECK_WEB_HSTS: 'true' },
      { MOWDECK_WEB_HSTS: 'ON' },
    ];
    for (const env of envs) {
      const { conf } = render(env);
      expect(conf).toContain('map $http_x_forwarded_proto $mowdeck_hsts {');
      expect(valueFor(conf, 'https')).toBe(VALUE);
      expect(valueFor(conf, 'HTTPS')).toBe(VALUE);
      expect(conf).not.toContain('preload');
    }
  });

  it('never sends the header over plain HTTP', () => {
    const { conf } = render({});
    expect(valueFor(conf, 'http')).toBe('');
    expect(valueFor(conf, '')).toBe('');
    expect(valueFor(conf, 'https, http')).toBe('');
    expect(valueFor(conf, 'xhttps')).toBe('');
  });

  it('turns off with false', () => {
    for (const value of ['false', 'FALSE', '0', 'off', 'no']) {
      const { conf } = render({ MOWDECK_WEB_HSTS: value });
      expect(valueFor(conf, 'https')).toBe('');
      expect(valueFor(conf, 'http')).toBe('');
    }
  });

  it('stays on for an unclear value and says so', () => {
    const { conf, stderr } = render({ MOWDECK_WEB_HSTS: 'maybe' });
    expect(valueFor(conf, 'https')).toBe(VALUE);
    expect(stderr).toContain('MOWDECK_WEB_HSTS');
  });

  it('is sent by every security header snippet and defined at the http level', () => {
    for (const snippet of [
      'security-headers.conf',
      'security-headers-track.conf',
      'security-headers-explorer.conf',
    ]) {
      expect(readFileSync(join(nginxDir, snippet), 'utf8')).toContain(
        'add_header Strict-Transport-Security $mowdeck_hsts always;',
      );
    }
    const server = readFileSync(join(nginxDir, 'default.conf'), 'utf8');
    expect(server.indexOf('include /tmp/mowdeck/hsts.conf;')).toBeGreaterThan(-1);
    expect(server.indexOf('include /tmp/mowdeck/hsts.conf;')).toBeLessThan(
      server.indexOf('server {'),
    );
  });
});
