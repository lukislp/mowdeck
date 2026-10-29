/**
 * The app's native shell singleton, wired to the Notifications API. Inert in a browser.
 */
import { fleetApi, identityApi, notificationsApi, unwrap } from '#lib/api/client.js';
import { ApiProblem } from '#lib/api/problem.js';
import { session } from '#lib/auth/session.svelte.js';

import { nativeBridge } from './capacitor';
import { NativeShell } from './shell.svelte';

function localStore(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

export const nativeShell = new NativeShell({
  bridge: typeof window === 'undefined' ? null : nativeBridge(window),
  storage: localStore(),
  user: () => (session.me ? `${session.me.subject}@${session.me.account_id ?? ''}` : null),
  session: () => session.me?.session_id ?? null,
  apns: {
    add: (token, deviceName, installationId) =>
      unwrap(
        notificationsApi.POST('/api/v1/notifications/me/push/apns', {
          body: {
            token,
            device_name: deviceName,
            ...(installationId ? { installation_id: installationId } : {}),
          },
        }),
      ),
    async remove(id) {
      await unwrap(
        notificationsApi.DELETE('/api/v1/notifications/me/push/apns/{device_id}', {
          params: { path: { device_id: id } },
        }),
      );
    },
  },
  widgetsApi: {
    async createCredential(deviceName, replaces) {
      const created = await unwrap(
        identityApi.POST('/api/v1/identity/me/widget-credentials', {
          body: { device_name: deviceName, ...(replaces ? { replaces } : {}) },
        }),
      );
      if (!created.token || !created.expires_at) throw new Error('no secret');
      return { id: created.id, token: created.token, expires_at: created.expires_at };
    },
    async revokeCredential(id) {
      await unwrap(
        identityApi.DELETE('/api/v1/identity/me/widget-credentials/{token_id}', {
          params: { path: { token_id: id } },
        }),
      );
    },
    async mowers() {
      const list = await unwrap(fleetApi.GET('/api/v1/fleet/mowers'));
      return list.items.map((mower) => ({ id: mower.id, name: mower.name }));
    },
    async registerDevice(body) {
      await unwrap(
        notificationsApi.PUT('/api/v1/notifications/me/live-activities/device', { body }),
      );
    },
    async unregisterDevice(installationId) {
      await unwrap(
        notificationsApi.DELETE(
          '/api/v1/notifications/me/live-activities/device/{installation_id}',
          {
            params: { path: { installation_id: installationId } },
          },
        ),
      );
    },
    async reportActivity(mowerId, body) {
      await unwrap(
        notificationsApi.PUT('/api/v1/notifications/me/live-activities/mowers/{mower_id}', {
          params: { path: { mower_id: mowerId } },
          body,
        }),
      );
    },
    async endActivity(mowerId, installationId) {
      await unwrap(
        notificationsApi.DELETE('/api/v1/notifications/me/live-activities/mowers/{mower_id}', {
          params: { path: { mower_id: mowerId }, query: { installation_id: installationId } },
        }),
      );
    },
  },
  async ntfyConfigured() {
    try {
      await unwrap(notificationsApi.GET('/api/v1/notifications/ntfy'));
      return true;
    } catch (error) {
      if (error instanceof ApiProblem && error.status === 404) return false;
      throw error;
    }
  },
});
