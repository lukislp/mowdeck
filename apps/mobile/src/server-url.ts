/**
 * The address of the owner's MowDeck server, as typed on the first-launch screen.
 *
 * The shell loads the web app from the server's origin (ADR 0036), so only an origin is
 * accepted: `mowdeck.example.lan`, `https://mowdeck.example.lan/` and
 * `https://mowdeck.example.lan:8443` are fine, a path, a query or credentials are not (the web
 * app is always served at `/`). Without a scheme HTTPS is assumed. Plain HTTP is accepted but
 * flagged: the refresh cookie is `Secure`, so login only works over HTTPS (or on localhost).
 */

export type ServerUrlProblem = 'empty' | 'invalid' | 'scheme' | 'path' | 'credentials';

export type ServerUrl =
  { ok: true; origin: string; insecure: boolean } | { ok: false; problem: ServerUrlProblem };

const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]']);

export function normalizeServerUrl(input: string): ServerUrl {
  const text = input.trim();
  if (!text) return { ok: false, problem: 'empty' };
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(text) ? text : `https://${text}`;
  let url: URL;
  try {
    url = new URL(withScheme);
  } catch {
    return { ok: false, problem: 'invalid' };
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:')
    return { ok: false, problem: 'scheme' };
  if (!url.hostname) return { ok: false, problem: 'invalid' };
  if (url.username || url.password) return { ok: false, problem: 'credentials' };
  if ((url.pathname !== '/' && url.pathname !== '') || url.search || url.hash) {
    return { ok: false, problem: 'path' };
  }
  const insecure = url.protocol === 'http:' && !LOCAL_HOSTS.has(url.hostname);
  return { ok: true, origin: url.origin, insecure };
}

/** True when a web app manifest (JSON text or parsed) is MowDeck's. */
export function isMowDeckManifest(data: unknown): boolean {
  let value = data;
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value) as unknown;
    } catch {
      return false;
    }
  }
  if (!value || typeof value !== 'object') return false;
  const manifest = value as { name?: unknown; short_name?: unknown };
  return manifest.name === 'MowDeck' || manifest.short_name === 'MowDeck';
}
