/**
 * The app's notification singletons: the inbox store and this device's Web Push, wired to the
 * Notifications API (`/api/v1/notifications`).
 */
import { notificationsApi, unwrap } from '$lib/api/client';
import { ApiProblem } from '$lib/api/problem';
import { PushDevice } from '$lib/push/device';
import { InboxStore } from '$lib/stores/inbox.svelte';

export const inbox = new InboxStore({
  list: (limit) =>
    unwrap(notificationsApi.GET('/api/v1/notifications/inbox', { params: { query: { limit } } })),
  read: (id) =>
    unwrap(
      notificationsApi.POST('/api/v1/notifications/inbox/{notification_id}/read', {
        params: { path: { notification_id: id } },
      }),
    ),
  ack: (id) =>
    unwrap(
      notificationsApi.POST('/api/v1/notifications/inbox/{notification_id}/ack', {
        params: { path: { notification_id: id } },
      }),
    ),
});

function localStore(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

export const pushDevice = new PushDevice({
  api: {
    async vapidKey() {
      try {
        const found = await unwrap(
          notificationsApi.GET('/api/v1/notifications/push/vapid-public-key'),
        );
        return found.public_key;
      } catch (error) {
        if (error instanceof ApiProblem && error.status === 404) return null;
        throw error;
      }
    },
    add: (subscription) =>
      unwrap(
        notificationsApi.POST('/api/v1/notifications/push-subscriptions', { body: subscription }),
      ),
    async remove(id) {
      await unwrap(
        notificationsApi.DELETE('/api/v1/notifications/push-subscriptions/{subscription_id}', {
          params: { path: { subscription_id: id } },
        }),
      );
    },
  },
  registration: () => navigator.serviceWorker.ready,
  requestPermission: () => Notification.requestPermission(),
  storage: localStore(),
});
