/**
 * Sign in with an external login provider (GitHub, ADR 0051; Apple, ADR 0052): the same round
 * trip for each one. `github.ts` and `apple.ts` bind these functions to their provider.
 *
 * - Login: `POST /oauth/<provider>/start` with the sign-in page's query (`next`, the app
 *   hand-off) answers with the provider's authorization URL and sets an HttpOnly cookie that
 *   binds the round trip to this browser; the page then navigates to the provider. The provider
 *   sends the browser back to Identity's callback, which sets the session cookie and redirects to
 *   `/login?…&signed_in_with=<provider>` (or `&sign_in_error=<code>`); the page restores the
 *   session from the cookie as on every load.
 * - Link (settings): the current password again, then the same round trip; Identity redirects
 *   to `/settings?linked=<provider>#profile` or `?link_error=<code>`.
 * - Only accounts linked in the settings can sign in; nothing is matched by e-mail address. The
 *   installation's provider settings never reach the browser: `GET /login-methods` says only
 *   whether a provider is on.
 */
import { problemFromResponse } from '#lib/api/problem.js';

import { IDENTITY } from './session.svelte';

/** Read access to a query (`page.url.searchParams` is read-only). */
export type Query = Pick<URLSearchParams, 'get'>;

/** Query parameters of the sign-in page that survive the round trip. */
const KEPT = ['next', 'app_request', 'app_state'] as const;

export interface OAuthDeps {
  fetch: typeof fetch;
  /** Leave the page for the provider (`location.assign`). */
  navigate: (url: string) => void;
}

function browserDeps(): OAuthDeps {
  return {
    fetch: (...args) => globalThis.fetch(...args),
    navigate: (url) => window.location.assign(url),
  };
}

/** Whether the installation offers a provider (`GET /login-methods`); never rejects. */
export async function providerEnabled(
  provider: string,
  fetchFn: typeof fetch = (...args) => globalThis.fetch(...args),
): Promise<boolean> {
  try {
    const response = await fetchFn(`${IDENTITY}/login-methods`, { credentials: 'omit' });
    if (!response.ok) return false;
    const body = (await response.json()) as Record<string, unknown>;
    return body[provider] === true;
  } catch {
    return false;
  }
}

/** The sign-in page's query to come back to: only what the page understands. */
export function returnQuery(params: Query): string | null {
  const kept = new URLSearchParams();
  for (const key of KEPT) {
    const value = params.get(key);
    if (value) kept.set(key, value);
  }
  const text = kept.toString();
  return text ? text : null;
}

/** Only an http(s) URL is followed (the server's answer, never anything from the page). */
function leaveFor(url: unknown, deps: Pick<OAuthDeps, 'navigate'>): void {
  if (typeof url !== 'string') throw new TypeError('no authorization URL');
  const parsed = new URL(url, 'https://invalid.invalid');
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
    throw new TypeError('unexpected authorization URL');
  }
  deps.navigate(parsed.href);
}

/** Start signing in with a provider; resolves when the browser is on its way. Throws `ApiProblem`. */
export async function signInWith(
  provider: string,
  params: Query,
  deps: OAuthDeps = browserDeps(),
): Promise<void> {
  const response = await deps.fetch(`${IDENTITY}/oauth/${provider}/start`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ return_query: returnQuery(params) }),
  });
  if (!response.ok) throw await problemFromResponse(response);
  const body = (await response.json()) as { authorize_url?: unknown };
  leaveFor(body.authorize_url, deps);
}

/** Start linking a provider to the signed-in user (`start`: the typed API call with the password). */
export async function linkProvider(
  start: () => Promise<{ authorize_url: string }>,
  deps: Pick<OAuthDeps, 'navigate'> = browserDeps(),
): Promise<void> {
  const body = await start();
  leaveFor(body.authorize_url, { ...browserDeps(), ...deps });
}

export type SignInError = 'cancelled' | 'expired' | 'denied' | 'unavailable' | 'rate_limited';
export type LinkError = 'cancelled' | 'expired' | 'denied' | 'unavailable' | 'taken' | 'already';

const SIGN_IN_ERRORS: readonly SignInError[] = [
  'cancelled',
  'expired',
  'denied',
  'unavailable',
  'rate_limited',
];
const LINK_ERRORS: readonly LinkError[] = [
  'cancelled',
  'expired',
  'denied',
  'unavailable',
  'taken',
  'already',
];

/** What came back from a provider to the sign-in page: signed in, an error, or nothing. */
export function signInResultOf(
  provider: string,
  params: Query,
): { signedIn: true } | { error: SignInError } | null {
  if (params.get('signed_in_with') === provider) return { signedIn: true };
  const error = params.get('sign_in_error');
  if (error === null || params.get('provider') !== provider) return null;
  return {
    error: (SIGN_IN_ERRORS as readonly string[]).includes(error)
      ? (error as SignInError)
      : 'denied',
  };
}

/** What came back from a provider to the settings: linked, an error, or nothing. */
export function linkResultOf(
  provider: string,
  params: Query,
): { linked: true } | { error: LinkError } | null {
  if (params.get('linked') === provider) return { linked: true };
  const error = params.get('link_error');
  if (error === null || params.get('provider') !== provider) return null;
  return {
    error: (LINK_ERRORS as readonly string[]).includes(error) ? (error as LinkError) : 'denied',
  };
}

/** The URL without the round trip's result parameters (so a reload does not repeat it). */
export function withoutResult(url: { readonly href: string }): string {
  const clean = new URL(url.href);
  for (const key of ['signed_in_with', 'sign_in_error', 'linked', 'link_error', 'provider']) {
    clean.searchParams.delete(key);
  }
  return `${clean.pathname}${clean.search}${clean.hash}`;
}
