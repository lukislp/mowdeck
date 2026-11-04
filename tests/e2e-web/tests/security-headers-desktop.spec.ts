// SPDX-FileCopyrightText: 2026 Lukas Körber
// SPDX-License-Identifier: AGPL-3.0-only

/**
 * Strict-Transport-Security of the web app (apps/web/nginx, MOWDECK_WEB_HSTS, on by default):
 * sent only for requests that reached the gateway over HTTPS. The test stack speaks plain HTTP, so
 * a page through Traefik has no header, and neither has a client that claims HTTPS itself (Traefik
 * trusts no proxy here and replaces its X-Forwarded-Proto). What the web container answers when
 * the gateway did forward an HTTPS request is checked inside the stack with `docker exec`.
 */
import { execFileSync } from 'node:child_process';

import { expect, test } from '@playwright/test';

import { PLATFORM_ADMIN_ENABLED, serviceContainer } from './support';

const HSTS = 'max-age=31536000; includeSubDomains';

test('plain HTTP pages carry the security headers but no Strict-Transport-Security', async ({
  request,
}) => {
  for (const path of ['/', '/login', '/track']) {
    const response = await request.get(path);
    expect(response.status(), path).toBe(200);
    const headers = response.headers();
    expect(headers['x-content-type-options'], path).toBe('nosniff');
    expect(headers['content-security-policy'], path).toBeTruthy();
    expect(headers['strict-transport-security'], path).toBeUndefined();
  }
});

test('a client cannot claim HTTPS through the gateway', async ({ request }) => {
  const response = await request.get('/', { headers: { 'X-Forwarded-Proto': 'https' } });
  expect(response.status()).toBe(200);
  expect(response.headers()['strict-transport-security']).toBeUndefined();
});

test('pages the gateway received over HTTPS carry Strict-Transport-Security', () => {
  test.skip(!PLATFORM_ADMIN_ENABLED, 'reads the web container in the stack (docker exec)');
  const web = serviceContainer('web');
  // busybox wget prints the response headers (-S) on stderr.
  const stsOf = (path: string, proto: string): string | undefined => {
    const result = execFileSync(
      'docker',
      [
        'exec',
        web,
        'sh',
        '-c',
        'wget -S -O /dev/null --header "X-Forwarded-Proto: $1" "http://127.0.0.1:8080$2" 2>&1',
        'sh',
        proto,
        path,
      ],
      { encoding: 'utf-8', timeout: 30_000, stdio: ['ignore', 'pipe', 'pipe'] },
    );
    const line = result.split('\n').find((l) => /^\s*strict-transport-security:/i.test(l));
    return line?.replace(/^\s*strict-transport-security:\s*/i, '').trim();
  };
  for (const path of ['/', '/login', '/track']) {
    expect(stsOf(path, 'https'), path).toBe(HSTS);
    expect(stsOf(path, 'http'), path).toBeUndefined();
  }
});
