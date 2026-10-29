/**
 * Fleet's linked account endpoints. Linking and re-reading log in to the vendor cloud and use
 * the client with the longer deadline; the rest is ordinary.
 */
import { fleetApi, fleetSlowApi, unwrap } from '#lib/api/client.js';
import { getLocale } from '#lib/paraglide/runtime.js';
import type { components } from '#lib/generated/fleet.js';

import type {
  LinkedAccount,
  LinkedAccountDetail,
  LinkedMowersIn,
  LinkedMowersOut,
} from './accounts';
import type { CredentialValues, ProviderDescriptor } from './providers';

type DeviceList = components['schemas']['LinkedDeviceList'];

/** The cloud providers enabled in the Device-Gateway (disabled ones are not listed), with
 * their texts in the UI language. */
export async function listProviders(): Promise<ProviderDescriptor[]> {
  return (
    await unwrap(
      fleetApi.GET('/api/v1/fleet/linked-accounts/providers', {
        params: { query: { locale: getLocale() } },
      }),
    )
  ).items;
}

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

/** Link a vendor account with the credential fields of its provider. */
export function linkAccount(
  provider: string,
  credentials: CredentialValues,
): Promise<LinkedAccountDetail> {
  return unwrap(
    fleetSlowApi.POST('/api/v1/fleet/linked-accounts', { body: { provider, credentials } }),
  );
}

/** Re-read the device list, optionally with new secret fields (write-only, never echoed). */
export function syncAccount(
  id: string,
  credentials?: CredentialValues,
): Promise<LinkedAccountDetail> {
  const secrets = credentials && Object.keys(credentials).length > 0 ? credentials : undefined;
  return unwrap(
    fleetSlowApi.POST('/api/v1/fleet/linked-accounts/{linked_account_id}/sync', {
      params: { path: { linked_account_id: id } },
      body: secrets ? { credentials: secrets } : {},
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
