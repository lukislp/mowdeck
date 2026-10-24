/**
 * Where a deep link or a tapped notification leads inside the web app.
 *
 * Deep links use the custom scheme `mowdeck://` (`mowdeck://mowers/<id>` opens `/mowers/<id>`,
 * `mowdeck://vision/proposals?x=1` opens `/vision/proposals?x=1`). Universal links (https) are
 * not possible: Apple and Google verify them through files on a public domain, and a MowDeck
 * server is usually internal. Notification payloads carry an app URL (`url`), reduced to a path
 * on this origin by the same rule as Web Push, so a notification never opens a foreign site.
 */
import { FALLBACK_URL, safePath } from '#lib/push/payload.js';

export const SCHEME = 'mowdeck:';

/** The app path of a `mowdeck://` link; null for anything else. */
export function pathFromDeepLink(link: string): string | null {
  let url: URL;
  try {
    url = new URL(link);
  } catch {
    return null;
  }
  if (url.protocol !== SCHEME) return null;
  // mowdeck://mowers/1 parses with host "mowers" and path "/1"; mowdeck:///mowers/1 with an
  // empty host. Both mean /mowers/1.
  const path = `/${url.host}${url.pathname}`.replace(/\/{2,}/g, '/');
  const target = (path.length > 1 ? path.replace(/\/$/, '') : '/') + url.search + url.hash;
  return target.startsWith('/') && !target.startsWith('//') ? target : null;
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : null;
}

/**
 * The app path of a tapped notification: a local notification (`notification.extra.url`) or an
 * APNs message (`notification.data.url`); the mower page or the inbox when there is no URL.
 */
export function pathFromNotificationAction(action: unknown): string {
  const notification = record(record(action)?.notification);
  const data = record(notification?.extra) ?? record(notification?.data);
  if (!data) return FALLBACK_URL;
  const mowerId = typeof data.mower_id === 'string' ? data.mower_id : null;
  return safePath(data.url, mowerId);
}
