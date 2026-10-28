import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, describe, it } from 'node:test';

import { loadConfig, parseTokens } from '../src/config.ts';
import { MowDeckClient, checkUserPermissions } from '../src/mowdeck.ts';
import { credentialsValid } from '../src/pairing.ts';
import { NodeStore, freePort, isMowerId } from '../src/store.ts';

const A = '11111111-2222-4333-8444-555555555555';
const B = '21111111-2222-4333-8444-555555555555';
const C = '31111111-2222-4333-8444-555555555555';

describe('NodeStore', () => {
  let dir = '';
  before(async () => {
    dir = await mkdtemp(join(tmpdir(), 'matter-store-'));
  });
  after(async () => {
    await rm(dir, { recursive: true, force: true });
  });

  it('gives every mower stable credentials and the lowest free port', async () => {
    const store = new NodeStore(dir);
    await store.load();
    const a = await store.ensure(A, 5540, 2);
    const b = await store.ensure(B, 5540, 2);
    assert.equal(a?.port, 5540);
    assert.equal(b?.port, 5541);
    assert.ok(a && credentialsValid(a));
    assert.equal(await store.ensure(C, 5540, 2), null, 'only two nodes allowed');
    assert.deepEqual(await store.ensure(A, 5540, 2), a, 'known mower keeps its record');

    const reloaded = new NodeStore(dir);
    await reloaded.load();
    assert.deepEqual(reloaded.get(A), a);
    if (process.platform !== 'win32') {
      assert.equal((await stat(join(dir, 'nodes.json'))).mode & 0o777, 0o600);
    }
  });

  it('regenerates credentials on the same port and frees ports on removal', async () => {
    const store = new NodeStore(dir);
    await store.load();
    const before = store.get(A);
    const after = await store.regenerate(A);
    assert.equal(after?.port, before?.port);
    assert.notEqual(after?.passcode, before?.passcode);
    await store.remove(B);
    assert.equal((await store.ensure(C, 5540, 2))?.port, 5541);
  });

  it('drops records with invalid credentials or ids', async () => {
    const file = join(dir, 'nodes.json');
    const data = JSON.parse(await readFile(file, 'utf8'));
    data.nodes.push({ mowerId: 'nope', port: 1, passcode: 20202021, discriminator: 1 });
    data.nodes.push({ mowerId: B, port: 5545, passcode: 12345678, discriminator: 1 });
    await writeFile(file, JSON.stringify(data));
    const store = new NodeStore(dir);
    await store.load();
    assert.deepEqual(
      store
        .all()
        .map((r) => r.mowerId)
        .sort(),
      [A, C],
    );
  });

  it('finds free ports and recognizes mower ids', () => {
    const rec = (port: number) => ({ mowerId: A, port, passcode: 1, discriminator: 1, createdAt: '' });
    assert.equal(freePort([rec(5540), rec(5542)], 5540, 4), 5541);
    assert.equal(freePort([rec(5540)], 5540, 1), null);
    assert.ok(isMowerId(A));
    assert.ok(!isMowerId('../etc'));
  });
});

describe('config', () => {
  it('reads tokens from a file, skipping comments and blank lines', () => {
    assert.deepEqual(parseTokens('# account one\nmdk_a\n\n  mdk_b  \r\n#x\n'), ['mdk_a', 'mdk_b']);
    const config = loadConfig(
      { MOWDECK_MATTER_API_TOKENS_FILE: '/run/secrets/x', MOWDECK_MATTER_LANGUAGE: 'EN' },
      ((path: string) => {
        assert.equal(path, '/run/secrets/x');
        return 'mdk_one\n';
      }) as never,
    );
    assert.deepEqual(config.apiTokens, ['mdk_one']);
    assert.equal(config.language, 'en');
    assert.equal(config.portBase, 5540);
    assert.equal(config.httpPort, 8790);
    assert.equal(config.profile, 'rvc');
  });

  it('rejects bad numbers and languages', () => {
    assert.throws(() => loadConfig({ MOWDECK_MATTER_PORT_BASE: '80' }), /PORT_BASE/);
    assert.throws(() => loadConfig({ MOWDECK_MATTER_LANGUAGE: 'fr' }), /LANGUAGE/);
  });
});

type Call = { url: string; init: RequestInit };

function fakeFetch(answer: (call: Call) => [number, unknown]) {
  const calls: Call[] = [];
  const fetcher = (async (url: string | URL, init: RequestInit = {}) => {
    const call = { url: String(url), init };
    calls.push(call);
    const [status, body] = answer(call);
    return new Response(body === null ? null : JSON.stringify(body), { status });
  }) as typeof fetch;
  return { calls, fetcher };
}

describe('MowDeckClient', () => {
  it('exchanges the API token once, sends commands and retries once after a 401', async () => {
    let rejectNext = false;
    const { calls, fetcher } = fakeFetch(({ url }) => {
      if (url.endsWith('/identity/token')) return [200, { access_token: 'acc', expires_in: 600 }];
      if (url.endsWith('/commands')) {
        if (rejectNext) {
          rejectNext = false;
          return [401, {}];
        }
        return [202, { id: 'c' }];
      }
      return [404, {}];
    });
    const client = new MowDeckClient('http://api', 'mdk_secret', fetcher);
    await client.command(A, { kind: 'pause_mow' });
    await client.command(A, { kind: 'pause_mow' });
    rejectNext = true;
    await client.command(A, { kind: 'resume_mow' });
    const exchanges = calls.filter((c) => c.url.endsWith('/identity/token'));
    assert.equal(exchanges.length, 2, 'one exchange, plus one after the 401');
    const command = calls.find((c) => c.url.endsWith('/commands'));
    assert.equal(new Headers(command?.init.headers).get('authorization'), 'Bearer acc');
    assert.deepEqual(JSON.parse(String(command?.init.body)), { command: { kind: 'pause_mow' } });
  });

  it('reads mowers, areas, the saved pattern and a missing state', async () => {
    const { fetcher } = fakeFetch(({ url }) => {
      if (url.endsWith('/identity/token')) return [200, { access_token: 'acc' }];
      if (url.endsWith('/fleet/mowers'))
        return [200, { items: [{ id: A, name: 'Luba', model: null }, { bad: 1 }] }];
      if (url.endsWith(`/telemetry/mowers/${A}/map`))
        return [200, { map: { areas: [{ id: 'x' }, { id: 2 }, { id: 'y' }] } }];
      if (url.endsWith(`/fleet/mowers/${A}`))
        return [200, { mow_defaults: { pattern: { kind: 'stripes' } } }];
      return [404, {}];
    });
    const client = new MowDeckClient('http://api', 'mdk', fetcher);
    assert.deepEqual(await client.mowers(), [{ id: A, name: 'Luba', model: null, serial: null }]);
    assert.deepEqual(await client.areaIds(A), ['x', 'y']);
    assert.deepEqual(await client.mowPattern(A), { kind: 'stripes' });
    assert.equal(await client.widget(A), null);
  });
});

describe('checkUserPermissions', () => {
  it("asks Identity with the caller's own token", async () => {
    const { calls, fetcher } = fakeFetch(() => [200, [true, false, true, true]]);
    const answers = await checkUserPermissions('http://api', 'Bearer user-token', [A, B, A], fetcher);
    assert.deepEqual(answers.get(A), { view: true, control: false });
    assert.deepEqual(answers.get(B), { view: true, control: true });
    assert.equal(new Headers(calls[0]?.init.headers).get('authorization'), 'Bearer user-token');
    assert.deepEqual(JSON.parse(String(calls[0]?.init.body)).checks[1], {
      relation: 'can_control',
      object: `mower:${A}`,
    });
  });

  it('refuses a missing token without asking', async () => {
    const { calls, fetcher } = fakeFetch(() => [200, []]);
    await assert.rejects(checkUserPermissions('http://api', undefined, [A], fetcher), /bearer/);
    await assert.rejects(checkUserPermissions('http://api', 'Basic x', [A], fetcher), /bearer/);
    assert.equal(calls.length, 0);
  });
});
