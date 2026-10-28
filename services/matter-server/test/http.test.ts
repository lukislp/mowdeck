import assert from 'node:assert/strict';
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { after, before, describe, it } from 'node:test';

import { createHttpHandler, descriptor, type HttpOptions } from '../src/http.ts';
import { MowDeckError, type PermissionAnswer } from '../src/mowdeck.ts';
import type { PairingInfo } from '../src/nodes.ts';
import { pairingCodes } from '../src/pairing.ts';

const A = '11111111-2222-4333-8444-555555555555';
const B = '21111111-2222-4333-8444-555555555555';
const C = '31111111-2222-4333-8444-555555555555';

const info = (mowerId: string, name: string): PairingInfo => ({
  mowerId,
  name,
  commissioned: false,
  fabricCount: 0,
  codes: pairingCodes({ passcode: 20_202_021, discriminator: 3840 }),
});

// The user may control A, only see B, and knows nothing of C.
const rights: Record<string, PermissionAnswer> = {
  [A]: { view: true, control: true },
  [B]: { view: true, control: false },
  [C]: { view: false, control: false },
};

let resets = 0;
const options: HttpOptions = {
  manager: {
    ready: true,
    size: 3,
    summaries: () => [
      { mowerId: B, name: 'Bravo', commissioned: false, fabricCount: 0 },
      { mowerId: A, name: 'Alpha', commissioned: true, fabricCount: 2 },
      { mowerId: C, name: 'Charlie', commissioned: false, fabricCount: 0 },
    ],
    pairing: (id: string) => (id === A || id === B || id === C ? info(id, 'Alpha') : null),
    reset: async (id: string) => {
      resets++;
      return info(id, 'Alpha');
    },
  },
  permissions: async (authorization, ids) => {
    if (authorization !== 'Bearer ok') throw new MowDeckError(401, 'nope');
    return new Map(ids.map((id) => [id, rights[id] ?? { view: false, control: false }]));
  },
  version: '1.2.3',
  sourceUrl: 'https://example.org/src',
};

let server: Server;
let base = '';
before(async () => {
  const handler = createHttpHandler(options);
  server = createServer((req, res) => void handler(req, res));
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
after(() => {
  server.close();
});

const get = (path: string, token = 'ok', method = 'GET') =>
  fetch(`${base}${path}`, { method, headers: token ? { authorization: `Bearer ${token}` } : {} });

describe('admin API', () => {
  it('lists only the mowers the caller may control, sorted, without codes', async () => {
    const response = await get('/api/v1/matter/mowers');
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('x-source-code'), 'https://example.org/src');
    const body = await response.json();
    assert.deepEqual(body, {
      items: [{ mower_id: A, name: 'Alpha', commissioned: true, fabric_count: 2 }],
    });
  });

  it('answers 401 without a valid token', async () => {
    assert.equal((await get('/api/v1/matter/mowers', '')).status, 401);
    assert.equal((await get(`/api/v1/matter/mowers/${A}/pairing`, 'bad')).status, 401);
  });

  it('shows the codes to a controller only, never cached', async () => {
    const response = await get(`/api/v1/matter/mowers/${A}/pairing`);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    const body = (await response.json()) as {
      manual_code: string;
      qr_payload: string;
      qr_matrix: string[];
    };
    assert.equal(body.manual_code, '3497-011-2332');
    assert.match(body.qr_payload, /^MT:/);
    assert.ok(Array.isArray(body.qr_matrix) && body.qr_matrix.length >= 21);
    assert.equal((await get(`/api/v1/matter/mowers/${B}/pairing`)).status, 403);
    assert.equal((await get(`/api/v1/matter/mowers/${C}/pairing`)).status, 404);
    assert.equal((await get('/api/v1/matter/mowers/not-a-uuid/pairing')).status, 404);
  });

  it('resets only with POST and control rights', async () => {
    assert.equal((await get(`/api/v1/matter/mowers/${A}/pairing/reset`)).status, 405);
    assert.equal((await get(`/api/v1/matter/mowers/${B}/pairing/reset`, 'ok', 'POST')).status, 403);
    assert.equal(resets, 0);
    const response = await get(`/api/v1/matter/mowers/${A}/pairing/reset`, 'ok', 'POST');
    assert.equal(response.status, 200);
    assert.equal(resets, 1);
  });

  it('serves health, readiness, metrics and the module descriptor', async () => {
    assert.equal((await get('/healthz', '')).status, 200);
    assert.equal((await get('/readyz', '')).status, 200);
    assert.match(await (await get('/metrics', '')).text(), /mowdeck_matter_nodes 3/);
    assert.deepEqual(await (await get('/.well-known/mowdeck-module', '')).json(), descriptor('1.2.3'));
  });

  it('describes the settings section for users with mower.control', () => {
    const value = descriptor('dev') as { version: string; ui_areas: { route: string }[] };
    assert.equal(value.version, '0.0.0');
    assert.deepEqual(
      value.ui_areas.map((area) => area.route),
      ['/settings/matter'],
    );
  });
});
