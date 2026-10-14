/**
 * State the page and the service worker share about Web Push, kept in the Cache API (the only
 * storage both can reach without IndexedDB code): the VAPID key the page subscribed with, so
 * the worker can re-subscribe on `pushsubscriptionchange`, and a renewed subscription that the
 * worker could not post itself (it has no token, ADR 0021) until the page posts it.
 */
export const PUSH_CACHE = 'mowdeck-push';
const KEY_PATH = '/__mowdeck/push/vapid-key';
const PENDING_PATH = '/__mowdeck/push/pending';

async function open(): Promise<Cache | null> {
  try {
    return typeof caches === 'undefined' ? null : await caches.open(PUSH_CACHE);
  } catch {
    return null;
  }
}

async function read(path: string): Promise<string | null> {
  const cache = await open();
  const found = await cache?.match(path);
  return found ? found.text() : null;
}

async function write(path: string, value: string | null): Promise<void> {
  const cache = await open();
  if (!cache) return;
  if (value === null) await cache.delete(path);
  else await cache.put(path, new Response(value, { headers: { 'Content-Type': 'text/plain' } }));
}

export function saveVapidKey(key: string): Promise<void> {
  return write(KEY_PATH, key);
}

export function readVapidKey(): Promise<string | null> {
  return read(KEY_PATH);
}

export function savePending(subscription: PushSubscriptionJSON): Promise<void> {
  return write(PENDING_PATH, JSON.stringify(subscription));
}

/** The renewed subscription waiting to be posted, removed from the store. */
export async function takePending(): Promise<PushSubscriptionJSON | null> {
  const value = await read(PENDING_PATH);
  if (value === null) return null;
  await write(PENDING_PATH, null);
  try {
    return JSON.parse(value) as PushSubscriptionJSON;
  } catch {
    return null;
  }
}

/** The VAPID public key (base64url) as the bytes `pushManager.subscribe` expects. */
export function urlBase64ToUint8Array(value: string): Uint8Array<ArrayBuffer> {
  const padded = value + '='.repeat((4 - (value.length % 4)) % 4);
  const binary = atob(padded.replace(/-/g, '+').replace(/_/g, '/'));
  const bytes = new Uint8Array(new ArrayBuffer(binary.length));
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** True when `buffer` holds the same bytes as the base64url `key`. */
export function sameKey(buffer: ArrayBuffer | null | undefined, key: string): boolean {
  if (!buffer) return false;
  const a = new Uint8Array(buffer);
  const b = urlBase64ToUint8Array(key);
  return a.length === b.length && a.every((value, i) => value === b[i]);
}
