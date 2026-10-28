/**
 * MowDeck Matter server (ADR 0063): every mower of the configured accounts as its own Matter node,
 * plus the admin API for the web app's pairing codes. Started with `node src/main.ts` (Node 24
 * runs the TypeScript directly).
 */
import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';

import { Environment, Logger } from '@matter/main';

import { loadConfig } from './config.ts';
import { startHttp } from './http.ts';
import { errorText, log, setLevel } from './log.ts';
import { MowDeckClient, checkUserPermissions } from './mowdeck.ts';
import { NodeManager } from './nodes.ts';
import { LABELS, type DeviceProfile } from './profiles/profile.ts';
import { rvcProfile } from './profiles/rvc.ts';
import { NodeStore } from './store.ts';

/** The device profiles by name; a future Matter mower type is added here (ADR 0063). */
const PROFILES: Record<string, DeviceProfile> = { rvc: rvcProfile };

async function main(): Promise<void> {
  const config = loadConfig();
  setLevel(config.logLevel);
  // matter.js' own log: warnings and errors only (it never sees a pairing code: see nodes.ts).
  Logger.level = config.logLevel === 'debug' ? 'info' : 'warn';

  const profile = PROFILES[config.profile];
  if (!profile) throw new Error(`unknown MOWDECK_MATTER_DEVICE_PROFILE ${config.profile}`);
  if (!config.apiTokens.length) {
    log('warn', 'matter.no_api_token', {
      hint: 'MOWDECK_MATTER_API_TOKENS_FILE names no token: no mower is served',
    });
  }

  const storage = join(config.dataDir, 'matter');
  await mkdir(storage, { recursive: true, mode: 0o700 });
  const environment = Environment.default;
  environment.vars.set('storage.path', storage);
  if (config.mdnsInterface) environment.vars.set('mdns.networkInterface', config.mdnsInterface);

  const store = new NodeStore(config.dataDir);
  await store.load();
  const clients = config.apiTokens.map((token) => new MowDeckClient(config.apiUrl, token));
  const manager = new NodeManager({
    clients,
    store,
    profile,
    labels: LABELS[config.language],
    portBase: config.portBase,
    maxNodes: config.maxNodes,
    version: config.version,
    environment,
  });

  const server = startHttp(
    {
      manager,
      permissions: (authorization, ids) =>
        checkUserPermissions(config.apiUrl, authorization, ids),
      version: config.version,
      sourceUrl: config.sourceUrl,
    },
    config.httpHost,
    config.httpPort,
  );
  log('info', 'matter.started', {
    version: config.version,
    profile: profile.name,
    accounts: clients.length,
    http_port: config.httpPort,
    port_base: config.portBase,
  });

  let stopping = false;
  const loop = (seconds: number, task: () => Promise<void>, name: string) => {
    const run = async () => {
      if (stopping) return;
      try {
        await task();
      } catch (error) {
        log('error', name, { error: errorText(error) });
      }
      if (!stopping) setTimeout(() => void run(), seconds * 1000);
    };
    void run();
  };
  loop(config.syncSeconds, () => manager.sync(), 'matter.sync.failed');
  loop(config.pollSeconds, () => manager.poll(), 'matter.poll.failed');

  const stop = async (signal: string) => {
    if (stopping) return;
    stopping = true;
    log('info', 'matter.stopping', { signal });
    server.close();
    await manager.close();
    process.exit(0);
  };
  process.on('SIGTERM', () => void stop('SIGTERM'));
  process.on('SIGINT', () => void stop('SIGINT'));
}

main().catch((error: unknown) => {
  log('error', 'matter.crashed', { error: errorText(error) });
  process.exit(1);
});
