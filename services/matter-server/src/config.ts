/**
 * Settings from the environment (`MOWDECK_MATTER_*`, plus the shared `MOWDECK_*` names). A
 * secret comes from a file (`*_FILE`, Compose and Kubernetes secrets); it is never read from a
 * plain variable and never logged.
 */
import { readFileSync } from 'node:fs';

export interface Config {
  /** MowDeck's API as the server reaches it (Traefik on the host: `http://127.0.0.1`). */
  apiUrl: string;
  /** API tokens, one per account (least privilege: `mower.view` and `mower.control`). */
  apiTokens: string[];
  /** Directory for the Matter fabrics and the setup credentials (a volume). */
  dataDir: string;
  /** Admin API (pairing codes for the web app) and health. */
  httpHost: string;
  httpPort: number;
  /** UDP port of the first mower node; every further node takes the next free one. */
  portBase: number;
  maxNodes: number;
  /** Seconds between two state polls of a mower. */
  pollSeconds: number;
  /** Seconds between two looks at the mower list (new mowers, removed mowers). */
  syncSeconds: number;
  /** Language of the labels the controllers show (run modes, battery). */
  language: 'de' | 'en';
  /** The Matter device profile (`rvc` until a mower device type exists). */
  profile: string;
  /** Network interface for mDNS (empty: every interface). */
  mdnsInterface: string;
  logLevel: 'debug' | 'info' | 'warn' | 'error';
  version: string;
  sourceUrl: string;
}

type Env = Record<string, string | undefined>;

function int(env: Env, name: string, fallback: number, min: number, max: number): number {
  const raw = env[name]?.trim();
  if (!raw) return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < min || value > max) {
    throw new Error(`${name} must be an integer from ${min} to ${max}`);
  }
  return value;
}

/** The non-empty lines of a token file; `#` starts a comment line. */
export function parseTokens(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('#'));
}

export function loadConfig(env: Env = process.env, read = readFileSync): Config {
  const tokenFile = env.MOWDECK_MATTER_API_TOKENS_FILE?.trim();
  const apiTokens = tokenFile ? parseTokens(read(tokenFile, 'utf8').toString()) : [];
  const language = (env.MOWDECK_MATTER_LANGUAGE?.trim() || 'de').toLowerCase();
  if (language !== 'de' && language !== 'en') {
    throw new Error('MOWDECK_MATTER_LANGUAGE must be de or en');
  }
  const level = (env.MOWDECK_LOG_LEVEL?.trim() || 'info').toLowerCase();
  const portBase = int(env, 'MOWDECK_MATTER_PORT_BASE', 5540, 1024, 65000);
  const maxNodes = int(env, 'MOWDECK_MATTER_MAX_NODES', 16, 1, 64);
  return {
    apiUrl: (env.MOWDECK_MATTER_API_URL?.trim() || 'http://127.0.0.1').replace(/\/+$/, ''),
    apiTokens,
    dataDir: env.MOWDECK_MATTER_DATA_DIR?.trim() || '/data',
    httpHost: env.MOWDECK_MATTER_HTTP_HOST?.trim() || '0.0.0.0',
    httpPort: int(env, 'MOWDECK_MATTER_HTTP_PORT', 8790, 1024, 65535),
    portBase,
    maxNodes,
    pollSeconds: int(env, 'MOWDECK_MATTER_POLL_SECONDS', 15, 5, 600),
    syncSeconds: int(env, 'MOWDECK_MATTER_SYNC_SECONDS', 60, 15, 3600),
    language,
    profile: env.MOWDECK_MATTER_DEVICE_PROFILE?.trim() || 'rvc',
    mdnsInterface: env.MOWDECK_MATTER_MDNS_INTERFACE?.trim() || '',
    logLevel:
      level === 'debug' || level === 'warn' || level === 'error'
        ? level
        : level === 'warning'
          ? 'warn'
          : 'info',
    version: env.MOWDECK_VERSION?.trim() || '0.0.0',
    sourceUrl: env.MOWDECK_SOURCE_URL?.trim() || 'https://github.com/lukislp/mowdeck',
  };
}
