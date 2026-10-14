/// <reference types="@sveltejs/kit" />
/// <reference no-default-lib="true"/>
/// <reference lib="esnext" />
/// <reference lib="webworker" />
/**
 * Offline shell: the built app, the static files and the SPA fallback are precached per
 * version. Navigations try the network first and fall back to the cached shell; app assets are
 * served from the cache. The API (/api/) is never cached or proxied: the worker has no token.
 * A new version waits until the page asks it to take over (update prompt).
 *
 * Web Push: `push` shows the `WebPushPayload` (title, body, `tag` so a repeat replaces the
 * earlier one, the page to open), `notificationclick` focuses an open window and moves it to
 * that page or opens a new one, `pushsubscriptionchange` re-subscribes with the stored VAPID
 * key. The worker never calls the API (it has no token): the renewed subscription waits in the
 * push cache until the page posts it (`PushDevice.syncPending`).
 */
import { build, files, version } from '$service-worker';

import {
  fallbackMessage,
  notificationOptions,
  parsePushPayload,
  FALLBACK_URL,
} from '$lib/push/payload';
import { PUSH_CACHE, readVapidKey, savePending, urlBase64ToUint8Array } from '$lib/push/store';

const sw = self as unknown as ServiceWorkerGlobalScope;
const CACHE = `mowdeck-shell-${version}`;
const SHELL = '/200.html';
// The legal pages' Markdown stays out of the cache: the operator replaces it without a new build.
const ASSETS = [...build, ...files.filter((file) => !file.startsWith('/legal/')), SHELL];

sw.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS)));
});

sw.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== CACHE && key !== PUSH_CACHE)
            .map((key) => caches.delete(key)),
        ),
      )
      .then(() => sw.clients.claim()),
  );
});

sw.addEventListener('message', (event) => {
  if (event.data?.type === 'skip-waiting') void sw.skipWaiting();
});

sw.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== sw.location.origin || url.pathname.startsWith('/api/')) return;

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(async () => (await caches.match(SHELL)) ?? Response.error()),
    );
    return;
  }
  if (ASSETS.includes(url.pathname)) {
    event.respondWith(caches.match(url.pathname).then((cached) => cached ?? fetch(request)));
  }
});

// --- Web Push -----------------------------------------------------------------------------------

sw.addEventListener('push', (event) => {
  const read = (): unknown => {
    try {
      return event.data?.json() ?? null;
    } catch {
      return event.data?.text() ?? null;
    }
  };
  const message = parsePushPayload(read()) ?? fallbackMessage();
  event.waitUntil(sw.registration.showNotification(message.title, notificationOptions(message)));
});

sw.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const data = event.notification.data as { url?: unknown } | null;
  const path = typeof data?.url === 'string' && data.url.startsWith('/') ? data.url : FALLBACK_URL;
  const target = new URL(path, sw.location.origin);
  event.waitUntil(
    (async () => {
      const windows = await sw.clients.matchAll({ type: 'window', includeUncontrolled: true });
      const own = windows.filter((client) => new URL(client.url).origin === target.origin);
      const exact = own.find((client) => client.url === target.href);
      if (exact) {
        await exact.focus();
        return;
      }
      const open = own[0];
      if (open) {
        // The app routes itself (client-side navigation keeps the session in memory).
        open.postMessage({ type: 'navigate', url: path });
        await open.focus();
        return;
      }
      await sw.clients.openWindow(target.href);
    })(),
  );
});

interface SubscriptionChangeEvent extends ExtendableEvent {
  readonly oldSubscription: PushSubscription | null;
  readonly newSubscription: PushSubscription | null;
}

sw.addEventListener('pushsubscriptionchange', (event) => {
  const change = event as SubscriptionChangeEvent;
  change.waitUntil(
    (async () => {
      let fresh = change.newSubscription;
      if (!fresh) {
        const key = await readVapidKey();
        if (!key) return;
        fresh = await sw.registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(key),
        });
      }
      await savePending(fresh.toJSON());
      for (const client of await sw.clients.matchAll({ type: 'window' })) {
        client.postMessage({ type: 'push-renewed' });
      }
    })(),
  );
});
