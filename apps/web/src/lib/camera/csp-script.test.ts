// @vitest-environment node
/**
 * The rendered CSP of nginx/40-mowdeck-runtime-config.sh: the hash of the Agora SDK's one inline
 * style (a 1 px probe <video>, agora-rtc-sdk-ng 4.24.8) is in style-src only with the camera on.
 */
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const script = join(process.cwd(), 'nginx', '40-mowdeck-runtime-config.sh');
const TEMPLATE =
  "add_header Content-Security-Policy \"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-hashes' 'sha256-abc='; img-src 'self' data: blob:; media-src 'self'; connect-src 'self'; worker-src 'self'\" always;\n";
const PROBE_STYLE = 'position: fixed; top: 0; left: 0; width: 1px; height: 1px';
const PROBE_HASH = `'sha256-${createHash('sha256').update(PROBE_STYLE).digest('base64')}'`;

const hasSh = spawnSync('sh', ['-c', 'exit 0']).status === 0;

function render(camera: string): string {
  const dir = mkdtempSync(join(tmpdir(), 'mowdeck-csp-'));
  const template = join(dir, 'template.conf');
  writeFileSync(template, TEMPLATE);
  const result = spawnSync('sh', [script], {
    env: {
      PATH: process.env.PATH ?? '',
      MOWDECK_CONFIG_DIR: join(dir, 'out'),
      MOWDECK_CSP_TEMPLATE: template,
      MOWDECK_WEB_CAMERA_ENABLED: camera,
    },
    encoding: 'utf8',
  });
  expect(result.status, result.stderr).toBe(0);
  return readFileSync(join(dir, 'out', 'csp.conf'), 'utf8');
}

describe.skipIf(!hasSh)('rendered CSP', () => {
  it('allows the SDK probe style only with the camera enabled', () => {
    expect(PROBE_HASH).toBe("'sha256-cPxxYHvCiQ/kT6TlojjBofFPdVQmCg8k/CdPKQDf9zI='");
    const on = render('true');
    expect(on).toContain(`style-src 'self' ${PROBE_HASH} 'unsafe-hashes' 'sha256-abc='`);
    expect(on).toContain('wss://*.sd-rtn.com:*');
    const off = render('false');
    expect(off).not.toContain(PROBE_HASH);
    expect(off).toContain("style-src 'self' 'unsafe-hashes' 'sha256-abc='");
    expect(off).not.toContain('sd-rtn.com');
  });
});
