/**
 * The real thing on loopback: a node per mower, commissioned by a matter.js controller with the
 * pairing code the admin API shows, then driven like Apple Home would (run mode, pause, go home),
 * reset with a new code and decommissioned when the mower leaves. Skipped with
 * MATTER_SKIP_INTEGRATION=1 (no UDP on the machine).
 */
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, describe, it } from 'node:test';

import { Environment, Logger, ServerNode } from '@matter/main';
import { PowerSourceClient } from '@matter/main/behaviors/power-source';
import { RvcOperationalStateClient } from '@matter/main/behaviors/rvc-operational-state';
import { RvcRunModeClient } from '@matter/main/behaviors/rvc-run-mode';
import { ControllerBehavior } from '@matter/main/node';

import type { MowDeckClient, MowerInfo } from '../src/mowdeck.ts';
import { NodeManager } from '../src/nodes.ts';
import { LABELS } from '../src/profiles/profile.ts';
import { rvcProfile } from '../src/profiles/rvc.ts';
import { NodeStore } from '../src/store.ts';

const A = '11111111-2222-4333-8444-555555555555';
const PORT_BASE = 25_540 + Math.floor(Math.random() * 200);
const skip = process.env.MATTER_SKIP_INTEGRATION === '1';

class FakeClient {
  mowerList: MowerInfo[] = [{ id: A, name: 'Luba Garten', model: 'LUBA 2 AWD', serial: 'S1' }];
  state: Record<string, unknown> = { activity: 'docked', battery_percent: 80, charging: true };
  commands: Record<string, unknown>[] = [];
  accountId = 'acc';
  async mowers() {
    return this.mowerList;
  }
  async widget() {
    return this.state;
  }
  async areaIds() {
    return ['a1', 'a2'];
  }
  async mowPattern() {
    return { kind: 'stripes', angle: 30 };
  }
  async command(_id: string, command: Record<string, unknown>) {
    this.commands.push(command);
    const kind = command.kind;
    if (kind === 'start_mow' || kind === 'resume_mow') this.state.activity = 'mowing';
    if (kind === 'pause_mow') this.state.activity = 'paused';
    if (kind === 'return_to_dock') this.state.activity = 'returning';
  }
}

describe('NodeManager with a matter.js controller', { skip }, () => {
  let dir = '';
  let manager: NodeManager;
  let controller: ServerNode;
  const client = new FakeClient();
  const store = () => new NodeStore(dir);

  before(async () => {
    Logger.level = 'error';
    dir = await mkdtemp(join(tmpdir(), 'matter-it-'));
    Environment.default.vars.set('storage.path', join(dir, 'matter'));
    const nodes = store();
    await nodes.load();
    manager = new NodeManager({
      clients: [client as unknown as MowDeckClient],
      store: nodes,
      profile: rvcProfile,
      labels: LABELS.de,
      portBase: PORT_BASE,
      maxNodes: 4,
      version: '0.1.0',
    });
    await manager.sync();
    controller = await ServerNode.create(ServerNode.RootEndpoint.with(ControllerBehavior), {
      id: 'controller',
      network: { port: PORT_BASE + 100 },
      commissioning: { enabled: false },
      controller: { ble: false },
    } as never);
    await controller.start();
  });

  after(async () => {
    await controller?.close();
    await manager?.close();
    await rm(dir, { recursive: true, force: true });
  });

  async function commission() {
    const pairing = manager.pairing(A);
    assert.ok(pairing);
    const record = (await (async () => {
      const s = store();
      await s.load();
      return s.get(A);
    })())!;
    const peer = await controller.peers.forDescriptor({
      addresses: [{ type: 'udp', ip: '127.0.0.1', port: record.port }],
      discriminator: record.discriminator,
    } as never);
    await peer.commission({ pairingCode: pairing.codes.manual.replaceAll('-', '') } as never);
    return peer;
  }

  it('serves one commissionable node per mower', () => {
    assert.equal(manager.size, 1);
    assert.deepEqual(manager.summaries(), [
      { mowerId: A, name: 'Luba Garten', commissioned: false, fabricCount: 0 },
    ]);
  });

  it('is commissioned with its code and maps commands to MowDeck', async () => {
    const peer = await commission();
    assert.equal(manager.pairing(A)?.commissioned, true);
    const endpoint = peer.endpoints.for(1);
    const op = endpoint.stateOf(RvcOperationalStateClient as never) as {
      operationalState: number;
      acceptedCommandList: number[];
    };
    assert.equal(op.operationalState, 65, 'docked and charging');
    assert.deepEqual([...op.acceptedCommandList].sort(), [0, 128, 3].sort());
    const power = endpoint.stateOf(PowerSourceClient as never) as { batPercentRemaining: number };
    assert.equal(power.batPercentRemaining, 160);

    const run = endpoint.commandsOf(RvcRunModeClient as never) as {
      changeToMode(r: { newMode: number }): Promise<{ status: number }>;
    };
    const ops = endpoint.commandsOf(RvcOperationalStateClient as never) as {
      pause(): Promise<{ commandResponseState: { errorStateId: number } }>;
      goHome(): Promise<{ commandResponseState: { errorStateId: number } }>;
      resume(): Promise<{ commandResponseState: { errorStateId: number } }>;
    };
    assert.equal((await run.changeToMode({ newMode: 1 })).status, 0);
    await manager.poll(A);
    assert.equal((await ops.pause()).commandResponseState.errorStateId, 0);
    await manager.poll(A);
    assert.equal((await ops.goHome()).commandResponseState.errorStateId, 0);
    await manager.poll(A);
    // Returning home: resume is refused (CommandInvalidInState), nothing is sent.
    assert.equal((await ops.resume()).commandResponseState.errorStateId, 3);
    assert.deepEqual(client.commands, [
      { kind: 'start_mow', area_ids: ['a1', 'a2'], pattern: { kind: 'stripes', angle: 30 } },
      { kind: 'pause_mow' },
      { kind: 'return_to_dock' },
    ]);
  });

  it('refuses commands while the mower is offline', async () => {
    client.state = { activity: 'mowing', battery_percent: 50, stale: true };
    await manager.poll(A);
    const result = await manager.handleIntent(A, 'pause');
    assert.deepEqual(result, { ok: false, reason: 'offline' });
    client.state = { activity: 'docked', battery_percent: 50, charging: false };
    await manager.poll(A);
  });

  it('resets the pairing with a new code', async () => {
    const before = manager.pairing(A)?.codes.manual;
    const after = await manager.reset(A);
    assert.ok(after);
    assert.equal(after.commissioned, false);
    assert.notEqual(after.codes.manual, before);
    await commission();
    assert.equal(manager.pairing(A)?.commissioned, true);
  });

  it('decommissions a mower that left MowDeck after two complete answers', async () => {
    client.mowerList = [];
    await manager.sync();
    assert.equal(manager.size, 1, 'one missing answer is not enough');
    await manager.sync();
    assert.equal(manager.size, 0);
    const s = store();
    await s.load();
    assert.equal(s.get(A), undefined);
  });
});
