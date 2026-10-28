/**
 * One standalone Matter node per mower (no bridge): every mower is commissioned on its own, like
 * a lamp, which Apple Home handles far better than bridged robot vacuums. One process serves all
 * nodes, each on its own UDP port with its own passcode and discriminator ({@link NodeStore}).
 *
 * - sync (every `syncSeconds`): the mowers of every API token's account. A new mower gets a node;
 *   a mower that is gone from a complete answer twice in a row loses its node (factory reset, so
 *   its fabrics and keys are erased). A failed answer never removes anything.
 * - poll (every `pollSeconds`): Telemetry's compact state of every mower, shown by the profile.
 * - commands from a controller run through {@link planIntent} and Fleet, with the server's API
 *   token, so Fleet's checks per command kind apply.
 */
import { DeviceTypeId, Environment, ServerNode, VendorId } from '@matter/main';
import { CommissioningServer } from '@matter/main/node';

import { errorText, log } from './log.ts';
import {
  UNKNOWN_SNAPSHOT,
  planIntent,
  snapshotFromWidget,
  startCommand,
  type Intent,
  type MowerSnapshot,
} from './model.ts';
import type { MowDeckClient, MowerInfo } from './mowdeck.ts';
import { PRODUCT_ID, VENDOR_ID, pairingCodes, type PairingCodes } from './pairing.ts';
import type { DeviceProfile, IntentResult, ProfileLabels } from './profiles/profile.ts';
import type { NodeRecord, NodeStore } from './store.ts';

/** matter.js logs the passcode and the QR code when a node waits for commissioning. The codes
 * are secrets here (only the web app shows them, to users who may control the mower). */
export class QuietCommissioningServer extends CommissioningServer {
  protected override initiateCommissioning(): void {}
}

const QuietRoot = ServerNode.RootEndpoint.with(QuietCommissioningServer);

export interface NodeManagerOptions {
  clients: MowDeckClient[];
  store: NodeStore;
  profile: DeviceProfile;
  labels: ProfileLabels;
  portBase: number;
  maxNodes: number;
  version: string;
  environment?: Environment;
}

interface Running {
  record: NodeRecord;
  mower: MowerInfo;
  client: MowDeckClient;
  node: ServerNode;
  endpoint: ReturnType<DeviceProfile['createEndpoint']>;
  snapshot: MowerSnapshot;
}

export interface PairingInfo {
  mowerId: string;
  name: string;
  commissioned: boolean;
  fabricCount: number;
  codes: PairingCodes;
}

export interface NodeSummary {
  mowerId: string;
  name: string;
  commissioned: boolean;
  fabricCount: number;
}

/** Matter strings of the Basic Information cluster are at most 32 characters. */
function clip(value: string, max = 32): string {
  return [...value].slice(0, max).join('');
}

export class NodeManager {
  readonly #options: NodeManagerOptions;
  readonly #running = new Map<string, Running>();
  /** Mowers missing from the last complete answer (removed after the second one). */
  readonly #missing = new Set<string>();
  readonly #busy = new Set<string>();
  #synced = false;
  /** Node ids are stable per mower so matter.js finds its storage again. */
  static nodeId(mowerId: string): string {
    return `mower-${mowerId}`;
  }

  constructor(options: NodeManagerOptions) {
    this.#options = options;
  }

  get ready(): boolean {
    return this.#synced;
  }

  get size(): number {
    return this.#running.size;
  }

  /** Mower id -> the client (account) that sees it, from every token; null if one failed. */
  async #collect(): Promise<{ found: Map<string, [MowerInfo, MowDeckClient]>; complete: boolean }> {
    const found = new Map<string, [MowerInfo, MowDeckClient]>();
    let complete = true;
    for (const client of this.#options.clients) {
      try {
        for (const mower of await client.mowers()) {
          if (!found.has(mower.id)) found.set(mower.id, [mower, client]);
        }
      } catch (error) {
        complete = false;
        log('warn', 'matter.sync.list_failed', { error: errorText(error) });
      }
    }
    return { found, complete };
  }

  async sync(): Promise<void> {
    const { found, complete } = await this.#collect();
    for (const [mowerId, [mower, client]] of found) {
      this.#missing.delete(mowerId);
      const running = this.#running.get(mowerId);
      if (running) {
        running.client = client;
        if (running.mower.name !== mower.name) await this.#rename(running, mower);
        continue;
      }
      if (this.#busy.has(mowerId)) continue;
      try {
        await this.#open(mower, client);
      } catch (error) {
        log('error', 'matter.node.start_failed', { mower_id: mowerId, error: errorText(error) });
      }
    }
    if (complete) {
      for (const mowerId of [...this.#running.keys()]) {
        if (found.has(mowerId)) continue;
        if (!this.#missing.has(mowerId)) {
          this.#missing.add(mowerId);
          continue;
        }
        await this.decommission(mowerId);
      }
      // Credentials of mowers that no longer exist and never got a node in this run.
      for (const record of this.#options.store.all()) {
        if (!found.has(record.mowerId) && !this.#running.has(record.mowerId)) {
          await this.#options.store.remove(record.mowerId);
        }
      }
    }
    this.#synced = true;
  }

  async #open(mower: MowerInfo, client: MowDeckClient): Promise<void> {
    const { store, portBase, maxNodes, profile, labels, version } = this.#options;
    const record = await store.ensure(mower.id, portBase, maxNodes);
    if (!record) {
      log('warn', 'matter.node.no_free_port', { mower_id: mower.id, max_nodes: maxNodes });
      return;
    }
    const snapshot = await this.#fetchSnapshot(client, mower.id).catch(() => UNKNOWN_SNAPSHOT);
    const node = await ServerNode.create(QuietRoot, {
      id: NodeManager.nodeId(mower.id),
      ...(this.#options.environment ? { environment: this.#options.environment } : {}),
      network: { port: record.port },
      commissioning: { passcode: record.passcode, discriminator: record.discriminator },
      productDescription: {
        name: clip(mower.name),
        deviceType: DeviceTypeId(profile.deviceTypeId),
      },
      basicInformation: {
        vendorName: 'MowDeck',
        vendorId: VendorId(VENDOR_ID),
        productName: clip(mower.model ?? 'Robotic mower'),
        productLabel: clip(mower.model ?? 'Robotic mower', 64),
        productId: PRODUCT_ID,
        nodeLabel: clip(mower.name),
        serialNumber: clip(mower.serial ?? mower.id.replaceAll('-', '')),
        uniqueId: clip(mower.id.replaceAll('-', '')),
        hardwareVersion: 1,
        hardwareVersionString: '1',
        softwareVersion: 1,
        softwareVersionString: clip(version, 64),
      },
    });
    const endpoint = profile.createEndpoint(snapshot, labels, (intent) =>
      this.handleIntent(mower.id, intent),
    );
    await node.add(endpoint);
    await node.start();
    this.#running.set(mower.id, { record, mower, client, node, endpoint, snapshot });
    log('info', 'matter.node.started', {
      mower_id: mower.id,
      port: record.port,
      commissioned: node.state.commissioning.commissioned,
    });
  }

  async #rename(running: Running, mower: MowerInfo): Promise<void> {
    running.mower = mower;
    try {
      await running.node.set({ basicInformation: { nodeLabel: clip(mower.name) } });
    } catch (error) {
      log('warn', 'matter.node.rename_failed', { mower_id: mower.id, error: errorText(error) });
    }
  }

  async #fetchSnapshot(client: MowDeckClient, mowerId: string): Promise<MowerSnapshot> {
    return snapshotFromWidget(await client.widget(mowerId));
  }

  /** Read the state of one mower (or all) and show it. */
  async poll(mowerId?: string): Promise<void> {
    const targets = mowerId
      ? [this.#running.get(mowerId)].filter((r): r is Running => !!r)
      : [...this.#running.values()];
    for (const running of targets) {
      try {
        running.snapshot = await this.#fetchSnapshot(running.client, running.mower.id);
      } catch (error) {
        log('warn', 'matter.poll.failed', { mower_id: running.mower.id, error: errorText(error) });
        running.snapshot = { ...running.snapshot, activity: 'offline' };
      }
      try {
        await this.#options.profile.apply(running.endpoint, running.snapshot);
      } catch (error) {
        log('warn', 'matter.apply.failed', { mower_id: running.mower.id, error: errorText(error) });
      }
    }
  }

  /** A controller's command: planned against the last state, sent through Fleet. */
  async handleIntent(mowerId: string, intent: Intent): Promise<IntentResult> {
    const running = this.#running.get(mowerId);
    if (!running) return { ok: false, reason: 'failed' };
    const plan = planIntent(intent, running.snapshot);
    log('info', 'matter.command', { mower_id: mowerId, intent, plan: plan.kind });
    try {
      switch (plan.kind) {
        case 'refuse':
          return { ok: false, reason: plan.reason };
        case 'noop':
          return { ok: true };
        case 'command':
          await running.client.command(mowerId, plan.command);
          break;
        case 'start': {
          const [areas, pattern] = await Promise.all([
            running.client.areaIds(mowerId),
            running.client.mowPattern(mowerId),
          ]);
          if (!areas.length) return { ok: false, reason: 'invalid_in_state' };
          await running.client.command(mowerId, startCommand(areas, pattern));
          break;
        }
      }
    } catch (error) {
      log('warn', 'matter.command.failed', { mower_id: mowerId, intent, error: errorText(error) });
      return { ok: false, reason: 'failed' };
    }
    // The real state follows; look again soon instead of waiting for the next round.
    setTimeout(() => void this.poll(mowerId), 3_000).unref();
    return { ok: true };
  }

  summaries(): NodeSummary[] {
    return [...this.#running.values()].map((running) => ({
      mowerId: running.mower.id,
      name: running.mower.name,
      ...this.#commissioning(running),
    }));
  }

  #commissioning(running: Running): { commissioned: boolean; fabricCount: number } {
    const state = running.node.state.commissioning;
    return {
      commissioned: state.commissioned,
      fabricCount: Object.keys(state.fabrics ?? {}).length,
    };
  }

  pairing(mowerId: string): PairingInfo | null {
    const running = this.#running.get(mowerId);
    if (!running) return null;
    return {
      mowerId,
      name: running.mower.name,
      ...this.#commissioning(running),
      codes: pairingCodes(running.record),
    };
  }

  /** Factory reset of one node with new credentials: every controller loses the mower, and
   * only the new code pairs it again. */
  async reset(mowerId: string): Promise<PairingInfo | null> {
    const running = this.#running.get(mowerId);
    if (!running || this.#busy.has(mowerId)) return null;
    this.#busy.add(mowerId);
    try {
      await this.#erase(running);
      await this.#options.store.regenerate(mowerId);
      await this.#open(running.mower, running.client);
      log('info', 'matter.node.reset', { mower_id: mowerId });
    } finally {
      this.#busy.delete(mowerId);
    }
    return this.pairing(mowerId);
  }

  /** Remove a mower's node for good (the mower left MowDeck). */
  async decommission(mowerId: string): Promise<void> {
    const running = this.#running.get(mowerId);
    if (!running || this.#busy.has(mowerId)) return;
    this.#busy.add(mowerId);
    try {
      await this.#erase(running);
      await this.#options.store.remove(mowerId);
      this.#missing.delete(mowerId);
      log('info', 'matter.node.decommissioned', { mower_id: mowerId });
    } catch (error) {
      log('error', 'matter.node.decommission_failed', {
        mower_id: mowerId,
        error: errorText(error),
      });
    } finally {
      this.#busy.delete(mowerId);
    }
  }

  async #erase(running: Running): Promise<void> {
    this.#running.delete(running.mower.id);
    // Factory reset: fabrics, sessions and keys are erased (Matter Core § 13.4).
    await running.node.erase();
    await running.node.close();
  }

  async close(): Promise<void> {
    await Promise.allSettled([...this.#running.values()].map((running) => running.node.close()));
    this.#running.clear();
  }
}
