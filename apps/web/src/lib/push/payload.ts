/**
 * The Web Push payload (`WebPushPayload` of `mowdeck_contracts.v1.notifications`) as the service
 * worker receives it, and the notification it shows. Shared by the worker and the tests; it has
 * no imports, so the worker bundle stays small. Texts arrive rendered in the user's language.
 */

/** A parsed push message, ready to show. */
export interface PushMessage {
  title: string;
  body: string;
  /** Same-origin path to open on click. */
  url: string;
  /** Replaces an earlier notification with the same tag (`mower:<id>:<kind>`). */
  tag: string | null;
  kind: string | null;
  mowerId: string | null;
  notificationId: string | null;
}

export const FALLBACK_URL = '/notifications';
const FALLBACK_TITLE = 'MowDeck';

function text(value: unknown, max: number): string | null {
  return typeof value === 'string' && value.trim() ? value.slice(0, max) : null;
}

/**
 * A path on this origin for `url`: relative paths are kept, absolute URLs are reduced to their
 * path (the server may know the app under another public origin), anything else falls back to
 * the mower page or the inbox. A push never opens a foreign site.
 */
export function safePath(url: unknown, mowerId: string | null): string {
  const fallback = mowerId ? `/mowers/${encodeURIComponent(mowerId)}` : FALLBACK_URL;
  if (typeof url !== 'string' || !url) return fallback;
  if (url.startsWith('/') && !url.startsWith('//') && !url.startsWith('/\\')) return url;
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return fallback;
    return parsed.pathname + parsed.search + parsed.hash;
  } catch {
    return fallback;
  }
}

/** Parse the push data (JSON text or an object); null when it is not a MowDeck payload. */
export function parsePushPayload(data: unknown): PushMessage | null {
  let raw: unknown = data;
  if (typeof raw === 'string') {
    try {
      raw = JSON.parse(raw);
    } catch {
      return null;
    }
  }
  if (!raw || typeof raw !== 'object') return null;
  const body = raw as Record<string, unknown>;
  const title = text(body.title, 200);
  if (!title) return null;
  const mowerId = text(body.mower_id, 64);
  return {
    title,
    body: text(body.body, 1000) ?? '',
    url: safePath(body.url, mowerId),
    tag: text(body.tag, 200),
    kind: text(body.kind, 64),
    mowerId,
    notificationId: text(body.notification_id, 64),
  };
}

/** What to show when a push carries no readable payload (a push must always show something). */
export function fallbackMessage(): PushMessage {
  return {
    title: FALLBACK_TITLE,
    body: '',
    url: FALLBACK_URL,
    tag: null,
    kind: null,
    mowerId: null,
    notificationId: null,
  };
}

/** Options for `registration.showNotification`. */
export function notificationOptions(message: PushMessage): NotificationOptions {
  return {
    body: message.body,
    tag: message.tag ?? undefined,
    icon: '/icons/icon-192.png',
    badge: '/icons/icon-192.png',
    data: { url: message.url, notificationId: message.notificationId, kind: message.kind },
  };
}
