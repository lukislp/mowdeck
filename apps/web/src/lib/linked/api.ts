/**
 * Fleet's linked account endpoints. Linking and re-reading log in to the vendor cloud and use
 * the client with the longer deadline; the rest is ordinary.
 */
import { fleetApi, fleetSlowApi, unwrap } from '$lib/api/client';
import type { components } from '$lib/generated/fleet';

import type {
  LinkedAccount,
  LinkedAccountDetail,
  LinkedMowersIn,
  LinkedMowersOut,
} from './accounts';

type DeviceList = components['schemas']['LinkedDeviceList'];

export async function listLinkedAccounts(): Promise<LinkedAccount[]> {
  return (await unwrap(fleetApi.GET('/api/v1/fleet/linked-accounts'))).items;
}

export function getLinkedAccount(id: string): Promise<LinkedAccount> {
  return unwrap(
    fleetApi.GET('/api/v1/fleet/linked-accounts/{linked_account_id}', {
      params: { path: { linked_account_id: id } },
    }),
  );
}

export function linkAccount(email: string, password: string): Promise<LinkedAccountDetail> {
  return unwrap(
    fleetSlowApi.POST('/api/v1/fleet/linked-accounts', {
      // The generated type still lists `provider`; Fleet no longer takes it.
      body: { email: email.trim(), password } as components['schemas']['LinkedAccountIn'],
    }),
  );
}

/** Re-read the device list, optionally with a new password (write-only, never echoed). */
export function syncAccount(id: string, password?: string): Promise<LinkedAccountDetail> {
  return unwrap(
    fleetSlowApi.POST('/api/v1/fleet/linked-accounts/{linked_account_id}/sync', {
      params: { path: { linked_account_id: id } },
      body: password ? { password } : {},
    }),
  );
}

export function listDevices(id: string): Promise<DeviceList> {
  return unwrap(
    fleetApi.GET('/api/v1/fleet/linked-accounts/{linked_account_id}/devices', {
      params: { path: { linked_account_id: id } },
    }),
  );
}

export async function removeLinkedAccount(id: string): Promise<void> {
  await unwrap(
    fleetApi.DELETE('/api/v1/fleet/linked-accounts/{linked_account_id}', {
      params: { path: { linked_account_id: id } },
    }),
  );
}

export function registerMowers(id: string, body: LinkedMowersIn): Promise<LinkedMowersOut> {
  return unwrap(
    fleetSlowApi.POST('/api/v1/fleet/linked-accounts/{linked_account_id}/mowers', {
      params: { path: { linked_account_id: id } },
      body,
    }),
  );
}
