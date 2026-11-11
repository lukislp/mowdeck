// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

// @vitest-environment node
/**
 * The access log of the web container in a public demo keeps no visitor data (docs/ops/demo.md,
 * "What the demo stores about a visitor"): the image's default log format prints the client
 * address, X-Forwarded-For (the visitor behind the gateway) and the browser string, so the
 * runtime config script renders access-log.conf with an anonymised format in a demo and leaves
 * the default log alone otherwise.
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const nginxDir = join(process.cwd(), 'nginx');
const script = join(nginxDir, '40-mowdeck-runtime-config.sh');
const hasSh = spawnSync('sh', ['-c', 'exit 0']).status === 0;

function accessLogConf(env: Record<string, string>): string {
  const dir = mkdtempSync(join(tmpdir(), 'mowdeck-accesslog-'));
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
  return readFileSync(join(dir, 'out', 'access-log.conf'), 'utf8');
}

const defaultConf = readFileSync(join(nginxDir, 'default.conf'), 'utf8');

describe('anonymised access log of the demo', () => {
  it('defines a format without address, browser, query or referrer', () => {
    const format = /log_format mowdeck_demo ([^;]+);/.exec(defaultConf);
    expect(format, 'log_format mowdeck_demo in default.conf').not.toBeNull();
    const text = format![1]!;
    for (const variable of [
      '$remote_addr',
      '$binary_remote_addr',
      '$realip_remote_addr',
      '$http_user_agent',
      '$http_x_forwarded_for',
      '$http_x_real_ip',
      '$http_referer',
      '$http_cookie',
      '$request ',
      '$request_uri',
      '$args',
      '$query_string',
      '$remote_user',
    ]) {
      expect(`${text} `).not.toContain(variable);
    }
    expect(text).toContain('$uri');
  });

  it('is included at server level, where it replaces the image default', () => {
    const server = defaultConf.slice(defaultConf.indexOf('server {'));
    expect(server).toContain('include /tmp/mowdeck/access-log.conf;');
    // No other access_log may log the default format: only /healthz switches its log off.
    const logs = [...defaultConf.matchAll(/access_log\s+([^;]+);/g)].map((m) => m[1]);
    expect(logs).toEqual(['off']);
  });
});

describe.skipIf(!hasSh)('access-log.conf', () => {
  it.each([{ MOWDECK_WEB_DEMO_MODE: 'true' }, { MOWDECK_WEB_DEMO_MODE: 'ON' }])(
    'switches the demo to the anonymised log (%o)',
    (env) => {
      const lines = accessLogConf(env)
        .split('\n')
        .filter((line) => line && !line.startsWith('#'));
      expect(lines).toEqual(['access_log /dev/stdout mowdeck_demo;']);
    },
  );

  it.each([{}, { MOWDECK_WEB_DEMO_MODE: 'false' }])(
    'leaves the default access log of other installations alone (%o)',
    (env) => {
      const lines = accessLogConf(env)
        .split('\n')
        .filter((line) => line && !line.startsWith('#'));
      expect(lines).toEqual([]);
    },
  );
});
