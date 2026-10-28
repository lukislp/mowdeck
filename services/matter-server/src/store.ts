/**
 * The setup credentials and UDP port of every mower node, in `<data>/nodes.json` (mode 0600) on
 * the server's volume: a mower keeps its pairing code and port across restarts and image
 * updates. The Matter fabrics themselves live in matter.js' storage beside it (`<data>/matter`).
 * Writes go to a temporary file and are renamed into place, so a crash never leaves half a file.
 */
import { chmod, mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

import { credentialsValid, generateCredentials, type SetupCredentials } from './pairing.ts';

export interface NodeRecord extends SetupCredentials {
  mowerId: string;
  port: number;
  createdAt: string;
}

interface FileShape {
  version: 1;
  nodes: NodeRecord[];
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isMowerId(value: string): boolean {
  return UUID.test(value);
}

/** The lowest port from `base` that no record uses, or null when `max` nodes exist. */
export function freePort(records: Iterable<NodeRecord>, base: number, max: number): number | null {
  const used = new Set([...records].map((record) => record.port));
  for (let port = base; port < base + max; port++) {
    if (!used.has(port)) return port;
  }
  return null;
}

export class NodeStore {
  readonly #file: string;
  readonly #records = new Map<string, NodeRecord>();
  #chain: Promise<void> = Promise.resolve();

  constructor(dataDir: string) {
    this.#file = join(dataDir, 'nodes.json');
  }

  async load(): Promise<void> {
    let text: string;
    try {
      text = await readFile(this.#file, 'utf8');
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return;
      throw error;
    }
    const data = JSON.parse(text) as Partial<FileShape>;
    for (const record of data.nodes ?? []) {
      if (
        typeof record?.mowerId === 'string' &&
        isMowerId(record.mowerId) &&
        Number.isInteger(record.port) &&
        credentialsValid(record)
      ) {
        this.#records.set(record.mowerId, record);
      }
    }
  }

  get(mowerId: string): NodeRecord | undefined {
    return this.#records.get(mowerId);
  }

  all(): NodeRecord[] {
    return [...this.#records.values()];
  }

  /** The record of `mowerId`, created with new credentials and a free port when missing; null
   * when every port of the range is taken. */
  async ensure(mowerId: string, base: number, max: number): Promise<NodeRecord | null> {
    const known = this.#records.get(mowerId);
    if (known) return known;
    const port = freePort(this.#records.values(), base, max);
    if (port === null) return null;
    const record: NodeRecord = {
      mowerId,
      port,
      ...generateCredentials(),
      createdAt: new Date().toISOString(),
    };
    this.#records.set(mowerId, record);
    await this.#save();
    return record;
  }

  /** New credentials for `mowerId` (same port): the old pairing code stops working. */
  async regenerate(mowerId: string): Promise<NodeRecord | null> {
    const known = this.#records.get(mowerId);
    if (!known) return null;
    const record: NodeRecord = {
      ...known,
      ...generateCredentials(),
      createdAt: new Date().toISOString(),
    };
    this.#records.set(mowerId, record);
    await this.#save();
    return record;
  }

  async remove(mowerId: string): Promise<void> {
    if (this.#records.delete(mowerId)) await this.#save();
  }

  #save(): Promise<void> {
    const data: FileShape = { version: 1, nodes: this.all() };
    const run = async () => {
      await mkdir(join(this.#file, '..'), { recursive: true, mode: 0o700 });
      const temp = `${this.#file}.tmp`;
      await writeFile(temp, `${JSON.stringify(data, null, 2)}\n`, { mode: 0o600 });
      await chmod(temp, 0o600);
      await rename(temp, this.#file);
    };
    this.#chain = this.#chain.then(run, run);
    return this.#chain;
  }
}
