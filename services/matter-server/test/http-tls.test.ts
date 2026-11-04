// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * Internal TLS of the admin API (docs/ops/internal-tls.md): with a certificate the server speaks
 * HTTPS only, and a client that trusts the certificate gets its answers. The certificate is made
 * by the openssl CLI for this test; without it the test is skipped.
 */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync } from 'node:fs';
import { get as httpGet } from 'node:http';
import { get as httpsGet } from 'node:https';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, it } from 'node:test';

import { loadConfig } from '../src/config.ts';
import { startHttp, type HttpOptions } from '../src/http.ts';

const options: HttpOptions = {
  manager: {
    ready: true,
    size: 0,
    summaries: () => [],
    pairing: () => null,
    reset: async () => null,
  },
  permissions: async () => new Map(),
  version: '1.2.3',
  sourceUrl: 'https://example.org/src',
};

function selfSigned(): { cert: Buffer; key: Buffer } | null {
  const dir = mkdtempSync(join(tmpdir(), 'matter-tls-'));
  const cert = join(dir, 'tls.crt');
  const key = join(dir, 'tls.key');
  const result = spawnSync(
    'openssl',
    [
      'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
      '-keyout', key, '-out', cert, '-days', '1', '-subj', '/CN=matter-server',
      '-addext', 'subjectAltName=DNS:matter-server,IP:127.0.0.1',
    ],
    { encoding: 'utf8' },
  );
  if (result.status !== 0) return null;
  return { cert: readFileSync(cert), key: readFileSync(key) };
}

const pem = selfSigned();

function fetchStatus(get: typeof httpsGet, url: string, ca?: Buffer): Promise<number> {
  return new Promise((resolve, reject) => {
    const request = get(url, ca ? { ca, servername: 'matter-server' } : {}, (response) => {
      response.resume();
      resolve(response.statusCode ?? 0);
    });
    request.on('error', reject);
    request.setTimeout(5000, () => request.destroy(new Error('timeout')));
  });
}

describe('internal TLS of the admin API', () => {
  it('reads certificate and key as a pair', () => {
    assert.equal(loadConfig({}).tls, null);
    assert.deepEqual(
      loadConfig({ MOWDECK_MATTER_TLS_CERT_FILE: '/c.crt', MOWDECK_MATTER_TLS_KEY_FILE: '/c.key' })
        .tls,
      { certFile: '/c.crt', keyFile: '/c.key' },
    );
    assert.throws(() => loadConfig({ MOWDECK_MATTER_TLS_CERT_FILE: '/c.crt' }), /TLS_KEY_FILE/);
  });

  it('serves HTTPS only with a certificate', { skip: pem === null && 'no openssl CLI' }, async () => {
    const server = startHttp(options, '127.0.0.1', 0, pem);
    await new Promise<void>((resolve) => server.once('listening', resolve));
    const { port } = server.address() as AddressInfo;
    try {
      assert.equal(await fetchStatus(httpsGet, `https://127.0.0.1:${port}/healthz`, pem!.cert), 200);
      await assert.rejects(fetchStatus(httpsGet, `https://127.0.0.1:${port}/healthz`));
      await assert.rejects(fetchStatus(httpGet as typeof httpsGet, `http://127.0.0.1:${port}/healthz`));
    } finally {
      server.close();
    }
  });
});
