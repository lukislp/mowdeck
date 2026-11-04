// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

// @vitest-environment node
/**
 * Internal TLS (docs/ops/internal-tls.md) as nginx/40-mowdeck-runtime-config.sh renders it:
 * listen.conf serves HTTPS on 8443 for the gateway and plain HTTP on the loopback only when a
 * certificate and its key are given, and plain HTTP on 8080 otherwise. Paths that are not plain
 * absolute paths never reach the nginx configuration. The real handshake behind Traefik is
 * covered by the Compose end-to-end suites.
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const nginxDir = join(process.cwd(), 'nginx');
const script = join(nginxDir, '40-mowdeck-runtime-config.sh');
const hasSh = spawnSync('sh', ['-c', 'exit 0']).status === 0;

function render(env: Record<string, string>) {
  const dir = mkdtempSync(join(tmpdir(), 'mowdeck-tls-'));
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
  return { conf: readFileSync(join(dir, 'out', 'listen.conf'), 'utf8'), stderr: result.stderr };
}

const CERT = '/etc/mowdeck/tls/web/tls.crt';
const KEY = '/etc/mowdeck/tls/web/tls.key';

describe.skipIf(!hasSh)('internal TLS of the web app', () => {
  it('serves plain HTTP on 8080 without a certificate', () => {
    const { conf, stderr } = render({});
    expect(conf.trim()).toBe('listen 8080;');
    expect(stderr).toBe('');
  });

  it('serves HTTPS on 8443 and HTTP on the loopback only with a certificate', () => {
    const { conf } = render({ MOWDECK_WEB_TLS_CERT_FILE: CERT, MOWDECK_WEB_TLS_KEY_FILE: KEY });
    expect(conf).toContain('listen 8443 ssl;');
    expect(conf).toContain('listen 127.0.0.1:8080;');
    expect(conf).not.toMatch(/^listen 8080;$/m);
    expect(conf).toContain(`ssl_certificate ${CERT};`);
    expect(conf).toContain(`ssl_certificate_key ${KEY};`);
    expect(conf).toContain('ssl_protocols TLSv1.2 TLSv1.3;');
  });

  it('keeps plain HTTP and says why for a missing key or an unsafe path', () => {
    const envs: Record<string, string>[] = [
      { MOWDECK_WEB_TLS_CERT_FILE: CERT },
      { MOWDECK_WEB_TLS_CERT_FILE: CERT, MOWDECK_WEB_TLS_KEY_FILE: 'relative/tls.key' },
      { MOWDECK_WEB_TLS_CERT_FILE: `${CERT}; include /etc/passwd`, MOWDECK_WEB_TLS_KEY_FILE: KEY },
    ];
    for (const env of envs) {
      const { conf, stderr } = render(env);
      expect(conf.trim()).toBe('listen 8080;');
      expect(stderr).toContain('MOWDECK_WEB_TLS_CERT_FILE');
    }
  });

  it('is included by the server block', () => {
    const server = readFileSync(join(nginxDir, 'default.conf'), 'utf8');
    expect(server).toContain('include /tmp/mowdeck/listen.conf;');
    expect(server).not.toMatch(/^\s*listen /m);
  });
});
