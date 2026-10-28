/**
 * The share address of a tracking link: `<origin>/track#<token>`. The token travels in the URL
 * fragment, so it never reaches a server log or a Referer header; the public page reads it from
 * `location.hash` and removes it from the address bar.
 */

/** Characters of a link token (URL-safe base64) and the lengths accepted before asking the server. */
const TOKEN = /^[A-Za-z0-9_-]{16,256}$/;

export function shareUrl(origin: string, token: string): string {
  return `${origin.replace(/\/+$/, '')}/track#${token}`;
}

/** The token in a `location.hash` (`#<token>`), or null when there is none or it is malformed. */
export function tokenFromHash(hash: string): string | null {
  const value = hash.startsWith('#') ? hash.slice(1) : hash;
  return TOKEN.test(value) ? value : null;
}

/** The Web Share API can share this address (a phone's share sheet). */
export function canShare(): boolean {
  return typeof navigator !== 'undefined' && typeof navigator.share === 'function';
}
