/**
 * Fleet's link endpoints of a mower (ADR 0038): the Bluetooth bridge next to the primary
 * connection, typed from `openapi/fleet.json`. The live state of an open link arrives with the
 * telemetry (`MowerState.links.ble_bridge`, `LinkStatus`); Fleet keeps the options and what the
 * last scan learnt about the bridge (`proxy_info`).
 *
 * The key (`noise_psk`) is write-only: Fleet answers `***`, and sending `***` back keeps the
 * stored value. Nothing here names a mower manufacturer; the bridge is an ESPHome proxy.
 */
import createClient from 'openapi-fetch';

import { authFetch, fleetApi, unwrap } from '$lib/api/client';
import { session } from '$lib/auth/session.svelte';
import type { components, paths as FleetPaths } from '$lib/generated/fleet';
import type { components as Telemetry } from '$lib/generated/telemetry';

export const BLE_BRIDGE = 'ble_bridge';

/** Where the guide to building the bridge lives (linked from the "outdated" badge). */
export const BRIDGE_GUIDE_URL =
  'https://github.com/lukislp/mowdeck/blob/main/docs/ops/ble-bridge-esphome.md';

/** Longest a scan may wait for the mower's advertisement (`BRIDGE_SCAN_MAX_TIMEOUT_S`). */
export const BRIDGE_SCAN_MAX_S = 30;

export type LinkStatus = Telemetry['schemas']['LinkStatus'];
export type ProxyInfo = components['schemas']['ProxyInfo'];
export type ScannedDevice = components['schemas']['ScannedDevice'];
export type BridgeScanErrorCode = components['schemas']['BridgeScanErrorCode'];
export type BridgeScanError = components['schemas']['BridgeScanError'];
export type BridgeScanReply = components['schemas']['BridgeScanReply'];
export type BridgeScanIn = components['schemas']['BridgeScanIn'];
export type BridgeLinkIn = components['schemas']['BridgeLinkIn'];
export type LinkOut = components['schemas']['LinkOut'];

/** The stored link as the app reads it: `LinkOut.options` flattened, the key never included. */
export interface BridgeLink {
  host: string;
  port: number;
  ble_address: string;
  device_name: string;
  allow_manual_blades: boolean;
  manual_blade_height_mm: number | null;
  proxy_info: ProxyInfo | null;
}

const SECRET_MASK = '***';

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function integer(value: unknown, fallback: number | null): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? Math.round(value) : fallback;
}

/** Read a link out of Fleet's `LinkOut` (the options may also come flat); null without a host. */
export function bridgeLinkOf(raw: unknown): BridgeLink | null {
  if (!raw || typeof raw !== 'object') return null;
  const record = raw as Record<string, unknown>;
  const options =
    record.options && typeof record.options === 'object'
      ? (record.options as Record<string, unknown>)
      : record;
  const host = text(options.host);
  if (!host) return null;
  return {
    host,
    port: integer(options.port, 6053) ?? 6053,
    ble_address: text(options.ble_address),
    device_name: text(options.device_name),
    allow_manual_blades: options.allow_manual_blades === true,
    manual_blade_height_mm: integer(options.manual_blade_height_mm, null),
    proxy_info:
      record.proxy_info && typeof record.proxy_info === 'object'
        ? (record.proxy_info as ProxyInfo)
        : null,
  };
}

/** The bridge link of a mower from `MowerOut.links`, if it has one. */
export function bridgeLinkOfMower(
  mower: { links?: Record<string, LinkOut> } | null | undefined,
): BridgeLink | null {
  return bridgeLinkOf(mower?.links?.[BLE_BRIDGE]);
}

/** A scan waits up to 30 s on the gateway and 35 s in Fleet: this client waits a little longer. */
export const SCAN_REQUEST_TIMEOUT_MS = 45_000;

const fleetScanApi = createClient<FleetPaths>({
  fetch: authFetch(
    session,
    (input, init) => globalThis.fetch(input, init),
    SCAN_REQUEST_TIMEOUT_MS,
  ),
});

const path = (mowerId: string) => ({ params: { path: { mower_id: mowerId } } });

/**
 * Look through the proxy for the mower. The outcomes the user acts on (`unreachable`,
 * `auth_failed`, `timeout`, `busy`) come inside the 200 reply; a relay failure is a problem
 * (409 `bridge-scan-unsupported`, 503 when no gateway answered).
 */
export function scanBridge(
  mowerId: string,
  input: BridgeScanIn,
  signal?: AbortSignal,
): Promise<BridgeScanReply> {
  return unwrap(
    fleetScanApi.POST('/api/v1/fleet/mowers/{mower_id}/links/ble_bridge/scan', {
      ...path(mowerId),
      body: input,
      signal,
    }),
  );
}

/** Re-check the stored link with the stored key (updates Fleet's `proxy_info`). */
export function checkBridge(mowerId: string): Promise<BridgeScanReply> {
  return unwrap(
    fleetScanApi.POST('/api/v1/fleet/mowers/{mower_id}/links/ble_bridge/check', path(mowerId)),
  );
}

export async function getBridgeLink(mowerId: string): Promise<BridgeLink | null> {
  const result = await fleetApi.GET(
    '/api/v1/fleet/mowers/{mower_id}/links/ble_bridge',
    path(mowerId),
  );
  if (result.response.status === 404) return null;
  return bridgeLinkOf(await unwrap(Promise.resolve(result)));
}

export async function putBridgeLink(mowerId: string, input: BridgeLinkIn): Promise<BridgeLink> {
  const saved = bridgeLinkOf(
    await unwrap(
      fleetApi.PUT('/api/v1/fleet/mowers/{mower_id}/links/ble_bridge', {
        ...path(mowerId),
        body: input,
      }),
    ),
  );
  // Fleet masks the key; the rest is what was sent.
  return (
    saved ?? {
      host: input.host,
      port: input.port,
      ble_address: input.ble_address,
      device_name: input.device_name ?? '',
      allow_manual_blades: input.allow_manual_blades,
      manual_blade_height_mm: input.manual_blade_height_mm ?? null,
      proxy_info: null,
    }
  );
}

export async function deleteBridgeLink(mowerId: string): Promise<void> {
  const result = await fleetApi.DELETE(
    '/api/v1/fleet/mowers/{mower_id}/links/ble_bridge',
    path(mowerId),
  );
  if (result.response.status === 404) return;
  await unwrap(Promise.resolve(result));
}

/** The scan input that keeps the stored key. */
export function keepStoredKey(link: BridgeLink): BridgeScanIn {
  return { host: link.host, port: link.port, noise_psk: SECRET_MASK };
}
