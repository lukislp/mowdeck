/**
 * The app's native shell singleton, wired to the Notifications API. Inert in a browser.
 */
import { notificationsApi, unwrap } from '#lib/api/client.js';
import { ApiProblem } from '#lib/api/problem.js';

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
  apns: {
    add: (token, deviceName) =>
      unwrap(
        notificationsApi.POST('/api/v1/notifications/me/push/apns', {
          body: { token, device_name: deviceName },
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
