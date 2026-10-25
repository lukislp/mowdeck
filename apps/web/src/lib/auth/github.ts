/**
 * Sign in with GitHub (ADR 0051).
 *
 * - Login: `POST /oauth/github/start` with the sign-in page's query (`next`, the app hand-off)
 *   answers with GitHub's authorization URL and sets an HttpOnly cookie that binds the round trip
 *   to this browser; the page then navigates to GitHub. GitHub sends the browser back to
 *   Identity's callback, which sets the session cookie and redirects to
 *   `/login?…&signed_in_with=github` (or `&sign_in_error=<code>`); the page restores the session
 *   from the cookie as on every load.
 * - Link (settings): the current password again, then the same round trip; Identity redirects
 *   to `/settings?linked=github#profile` or `?link_error=<code>`.
 * - Only accounts linked in the settings can sign in; nothing is matched by e-mail address. The
 *   installation's GitHub settings never reach the browser: `GET /login-methods` says only
 *   whether GitHub is on.
 */
import { problemFromResponse } from '#lib/api/problem.js';

import { IDENTITY } from './session.svelte';

export const PROVIDER = 'github';

/** Read access to a query (`page.url.searchParams` is read-only). */
type Query = Pick<URLSearchParams, 'get'>;

/** Query parameters of the sign-in page that survive the round trip. */
const KEPT = ['next', 'app_request', 'app_state'] as const;

export interface GitHubDeps {
  fetch: typeof fetch;
  /** Leave the page for GitHub (`location.assign`). */
  navigate: (url: string) => void;
}

function browserDeps(): GitHubDeps {
  return {
    fetch: (...args) => globalThis.fetch(...args),
    navigate: (url) => window.location.assign(url),
  };
}

/** Whether the installation offers GitHub (`GET /login-methods`); never rejects. */
export async function githubEnabled(
  fetchFn: typeof fetch = (...args) => globalThis.fetch(...args),
): Promise<boolean> {
  try {
    const response = await fetchFn(`${IDENTITY}/login-methods`, { credentials: 'omit' });
    if (!response.ok) return false;
    const body = (await response.json()) as { github?: unknown };
    return body.github === true;
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
function leaveFor(url: unknown, deps: GitHubDeps): void {
  if (typeof url !== 'string') throw new TypeError('no authorization URL');
  const parsed = new URL(url, 'https://invalid.invalid');
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
    throw new TypeError('unexpected authorization URL');
  }
  deps.navigate(parsed.href);
}

/** Start signing in with GitHub; resolves when the browser is on its way. Throws `ApiProblem`. */
export async function signInWithGitHub(
  params: Query,
  deps: GitHubDeps = browserDeps(),
): Promise<void> {
  const response = await deps.fetch(`${IDENTITY}/oauth/${PROVIDER}/start`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ return_query: returnQuery(params) }),
  });
  if (!response.ok) throw await problemFromResponse(response);
  const body = (await response.json()) as { authorize_url?: unknown };
  leaveFor(body.authorize_url, deps);
}

/** Start linking GitHub to the signed-in user (`start`: the typed API call with the password). */
export async function linkGitHub(
  start: () => Promise<{ authorize_url: string }>,
  deps: Pick<GitHubDeps, 'navigate'> = browserDeps(),
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

/** What came back from GitHub to the sign-in page: signed in, an error, or nothing. */
export function signInResult(params: Query): { signedIn: true } | { error: SignInError } | null {
  if (params.get('signed_in_with') === PROVIDER) return { signedIn: true };
  const error = params.get('sign_in_error');
  if (error === null || params.get('provider') !== PROVIDER) return null;
  return {
    error: (SIGN_IN_ERRORS as readonly string[]).includes(error)
      ? (error as SignInError)
      : 'denied',
  };
}

/** What came back from GitHub to the settings: linked, an error, or nothing. */
export function linkResult(params: Query): { linked: true } | { error: LinkError } | null {
  if (params.get('linked') === PROVIDER) return { linked: true };
  const error = params.get('link_error');
  if (error === null || params.get('provider') !== PROVIDER) return null;
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
