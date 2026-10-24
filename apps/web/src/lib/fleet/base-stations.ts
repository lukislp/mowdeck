/**
 * RTK base stations (ADR 0047): Fleet's records under `/api/v1/fleet/base-stations`, the link of a
 * mower (`PUT /mowers/{id}/base-station`), and the pure rules of the base station page: which
 * mowers a base station can serve, which mower's state reports it, and why its firmware update
 * cannot start now (the controller's `base_station_firmware_update_problem` mirrored). A base
 * station has no connection of its own: its state comes from a linked mower
 * (`MowerState.base_station`), its firmware is checked and installed through one.
 */
import { fleetApi, fleetSlowApi, unwrap } from '$lib/api/client';
import type { components as Fleet } from '$lib/generated/fleet';
import type { components as Telemetry } from '$lib/generated/telemetry';

export type BaseStation = Fleet['schemas']['BaseStationOut'];
export type BaseStationIn = Fleet['schemas']['BaseStationIn'];
export type BaseStationState = Telemetry['schemas']['BaseStationState'];
export type FirmwareInfo = Telemetry['schemas']['FirmwareInfo'];
export type CommandView = Fleet['schemas']['CommandView'];
type Mower = Fleet['schemas']['MowerOut'];
type LinkedDevice = Fleet['schemas']['LinkedDeviceOut'];

// --- API ------------------------------------------------------------------------------------

export async function listBaseStations(): Promise<BaseStation[]> {
  return (await unwrap(fleetApi.GET('/api/v1/fleet/base-stations'))).items;
}

export function registerBaseStation(body: BaseStationIn): Promise<BaseStation> {
  return unwrap(fleetApi.POST('/api/v1/fleet/base-stations', { body }));
}

export function renameBaseStation(id: string, name: string): Promise<BaseStation> {
  return unwrap(
    fleetApi.PATCH('/api/v1/fleet/base-stations/{base_station_id}', {
      params: { path: { base_station_id: id } },
      body: { name },
    }),
  );
}

export async function deleteBaseStation(id: string): Promise<void> {
  await unwrap(
    fleetApi.DELETE('/api/v1/fleet/base-stations/{base_station_id}', {
      params: { path: { base_station_id: id } },
    }),
  );
}

/** Asks the vendor through a linked mower; can take a while (the slow client). */
export function checkBaseStationFirmware(id: string): Promise<FirmwareInfo> {
  return unwrap(
    fleetSlowApi.POST('/api/v1/fleet/base-stations/{base_station_id}/firmware/check', {
      params: { path: { base_station_id: id } },
    }),
  );
}

/** Sends `base_station_firmware_update` to a linked mower; answers the command's id. */
export async function updateBaseStationFirmware(
  id: string,
  version: string,
  mowerId: string | null,
): Promise<string> {
  const accepted = await unwrap(
    fleetApi.POST('/api/v1/fleet/base-stations/{base_station_id}/firmware/update', {
      params: { path: { base_station_id: id } },
      body: mowerId ? { version, mower_id: mowerId } : { version },
    }),
  );
  return accepted.command_id;
}

export function getCommand(commandId: string): Promise<CommandView> {
  return unwrap(
    fleetApi.GET('/api/v1/fleet/commands/{command_id}', {
      params: { path: { command_id: commandId } },
    }),
  );
}

/** Link the mower to the base station, or unlink it with null. */
export function setMowerBaseStation(mowerId: string, baseStationId: string | null): Promise<Mower> {
  return unwrap(
    fleetApi.PUT('/api/v1/fleet/mowers/{mower_id}/base-station', {
      params: { path: { mower_id: mowerId } },
      body: { base_station_id: baseStationId },
    }),
  );
}

// --- rules ----------------------------------------------------------------------------------

/**
 * Whether the base station can serve the mower (Fleet's rule): a cloud base station serves the
 * cloud mowers of its own linked account, the simulated one simulated mowers.
 */
export function canServe(
  base: Pick<BaseStation, 'source' | 'linked_account_id'>,
  mower: Pick<Mower, 'connection_kind' | 'linked_account_id'>,
): boolean {
  if (base.source === 'simulated') return mower.connection_kind === 'simulated';
  return (
    mower.connection_kind === 'cloud' &&
    !!base.linked_account_id &&
    mower.linked_account_id === base.linked_account_id
  );
}

/** The base stations of the linked accounts' device lists that could be registered. */
export function baseStationDevices(devices: readonly LinkedDevice[]): LinkedDevice[] {
  return devices
    .filter((device) => device.device_type === 'rtk_base')
    .sort(
      (a, b) =>
        Number(a.registered || a.registered_elsewhere) -
          Number(b.registered || b.registered_elsewhere) ||
        a.device_name.localeCompare(b.device_name),
    );
}

/** The newest report of the base station among its linked mowers' states. */
export function reportOf(
  base: Pick<BaseStation, 'id' | 'mower_ids'>,
  stateOf: (mowerId: string) => { base_station?: BaseStationState | null } | null | undefined,
): { mowerId: string; state: BaseStationState } | null {
  let best: { mowerId: string; state: BaseStationState } | null = null;
  for (const mowerId of base.mower_ids) {
    const state = stateOf(mowerId)?.base_station;
    if (!state || (state.base_station_id && state.base_station_id !== base.id)) continue;
    const at = Date.parse(state.details_at ?? state.firmware_info?.checked_at ?? '') || 0;
    const bestAt = best
      ? Date.parse(best.state.details_at ?? best.state.firmware_info?.checked_at ?? '') || 0
      : -1;
    if (!best || at > bestAt) best = { mowerId, state };
  }
  return best;
}

/** Why the base station's firmware update cannot start through a mower now; null when it may. */
export type BaseUpdateBlocker =
  | 'no_permission'
  | 'plan'
  | 'no_mower'
  | 'offline'
  | 'unsupported'
  | 'no_check'
  | 'installing'
  | 'no_update'
  | 'mower_busy'
  | 'drive_session';

/** The relaying mower must not mow, be paused, return or update (ADR 0047). */
const RELAY_BLOCKING = new Set(['mowing', 'paused', 'returning', 'updating']);

export function baseUpdateBlocker(input: {
  /** `can_manage_mowers` on the account. */
  allowed: boolean;
  features: readonly string[] | null;
  restricted: boolean;
  /** The relaying mower: none when the base station has no linked mower. */
  relay: {
    activity: string | null;
    offline: boolean;
    capable: boolean;
    driveSession: boolean;
  } | null;
  info: FirmwareInfo | null | undefined;
}): BaseUpdateBlocker | null {
  if (!input.allowed) return 'no_permission';
  if (input.restricted) return 'plan';
  if (input.features && !input.features.includes('*') && !input.features.includes('control'))
    return 'plan';
  const relay = input.relay;
  if (!relay) return 'no_mower';
  if (relay.offline || relay.activity === null || relay.activity === 'offline') return 'offline';
  if (!relay.capable) return 'unsupported';
  const info = input.info;
  if (!info) return 'no_check';
  if (info.update_state === 'installing') return 'installing';
  if (info.update_state !== 'available' || !info.available) return 'no_update';
  if (RELAY_BLOCKING.has(relay.activity)) return 'mower_busy';
  if (relay.driveSession) return 'drive_session';
  return null;
}

/** The progress of an installing update, 0..100; null otherwise. */
export function updateProgress(info: FirmwareInfo | null | undefined): number | null {
  if (info?.update_state !== 'installing' || typeof info.progress !== 'number') return null;
  return Math.round(Math.min(100, Math.max(0, info.progress)));
}

/** The newer of the reported firmware info and the last check's answer. */
export function newerInfo(
  reported: FirmwareInfo | null | undefined,
  checked: FirmwareInfo | null | undefined,
): FirmwareInfo | null {
  if (!reported) return checked ?? null;
  if (!checked) return reported;
  const at = (info: FirmwareInfo) => Date.parse(info.checked_at ?? '') || 0;
  return at(checked) > at(reported) ? checked : reported;
}

/** A command record is final: a result arrived (`result_at`) or it ended otherwise. */
export function commandSettled(view: Pick<CommandView, 'status' | 'result_at'>): boolean {
  if (['executed', 'rejected', 'failed', 'expired'].includes(view.status)) return true;
  return view.status === 'accepted' && !!view.result_at;
}

/** `48.137131, 11.576090`: six decimals, about 10 cm. */
export function formatLatLon(position: { lat: number; lon: number }): string {
  return `${position.lat.toFixed(6)}, ${position.lon.toFixed(6)}`;
}
