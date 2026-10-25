/**
 * App login hand-off (ADR 0050): the native app signs in through the system browser.
 *
 * A WKWebView or Android WebView cannot use the owner's passkeys without Associated Domains or
 * Digital Asset Links, which a self-hosted server at any address cannot provide. So the app
 * opens the server's sign-in page in the system browser (iOS `ASWebAuthenticationSession`,
 * Android Custom Tab, through `MowDeckShell.openAuthSession`) and takes the login over:
 *
 *   1. App: a random verifier stays in this page's memory; its S256 hash is registered with
 *      Identity (`POST /app-login/start`), which answers with a short-lived, single-use request
 *      id. Request id and a random `app_state` go into `/login?app_request=...&app_state=...`
 *      (a link cannot bring its own challenge along).
 *   2. Browser: any login (passkey, password; later a redirect provider), then the user
 *      confirms "continue in the app". The fresh browser session is exchanged for a one-time
 *      code bound to the request (`POST /app-login/code`, which ends the browser session), and
 *      the page navigates to `mowdeck://auth/callback?code=...&state=...` (on Android as an
 *      `intent:` URL naming the app's package, so no other app can catch it).
 *   3. App: iOS hands that URL back as the result of the session; Android delivers it as a deep
 *      link (`appUrlOpen`). The app checks the state and redeems code and verifier
 *      (`POST /app-login/session`) for its own session, the refresh cookie in the web view.
 *
 * No token ever travels in a URL; the code alone is useless without the verifier, which never
 * leaves the app's web view, and is single use and valid for a minute.
 */
import { toBase64url } from '#lib/auth/webauthn.js';

import type { NativeBridge } from './capacitor';
import { AUTH_CALLBACK, authCallback } from './links';

export const SHELL = 'MowDeckShell';
export const OPEN_AUTH_SESSION = 'openAuthSession';
/** Give up waiting for the browser after this long (Android cannot tell when it is closed). */
export const HANDOFF_TIMEOUT_MS = 5 * 60_000;

export type HandoffResult = 'signed-in' | 'cancelled';

export class HandoffError extends Error {
  constructor(readonly reason: 'state' | 'failed' | 'timeout') {
    super(`app login hand-off failed: ${reason}`);
    this.name = 'HandoffError';
  }
}

export interface HandoffDeps {
  bridge: NativeBridge;
  /** The server origin (the page's own origin inside the shell). */
  origin: string;
  /** Register the PKCE challenge (`POST /app-login/start`); resolves to the request id. */
  start: (challenge: string) => Promise<string>;
  /** Redeem code and verifier for the app's session (`POST /app-login/session`). */
  redeem: (code: string, verifier: string) => Promise<void>;
  random?: (length: number) => Uint8Array<ArrayBuffer>;
  sha256?: (data: Uint8Array<ArrayBuffer>) => Promise<ArrayBuffer>;
  timeoutMs?: number;
}

/** The shell can open the system browser for a login (app builds with ADR 0050). */
export function handoffAvailable(bridge: NativeBridge | null): bridge is NativeBridge {
  return !!bridge && bridge.hasMethod(SHELL, OPEN_AUTH_SESSION) && bridge.has('App');
}

const VALUE = /^[A-Za-z0-9_-]{16,128}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
/** The package of the Android app: the callback intent may only open it. */
export const ANDROID_PACKAGE = 'app.mowdeck.mobile';

/** The app parameters of a sign-in page opened for the app; null for a normal sign-in. */
export function appLoginRequest(
  search: Pick<URLSearchParams, 'get'>,
): { requestId: string; state: string } | null {
  const requestId = search.get('app_request');
  const state = search.get('app_state');
  if (!requestId || !state) return null;
  if (!UUID.test(requestId) || !VALUE.test(state)) return null;
  return { requestId, state };
}

/**
 * The link that returns the result to the app (fixed scheme and package: never an open
 * redirect). On Android an `intent:` URL that only the MowDeck app can receive; Chrome hands
 * the app `mowdeck://auth/callback?...` as the data.
 */
export function callbackUrl(
  params: { code: string; state: string } | { error: string; state: string },
  userAgent = '',
): string {
  const query = new URLSearchParams(params).toString();
  if (/Android/i.test(userAgent)) {
    return `intent://auth/callback?${query}#Intent;scheme=mowdeck;package=${ANDROID_PACKAGE};end`;
  }
  return `${AUTH_CALLBACK}?${query}`;
}

/** Register the app's PKCE challenge (`POST /app-login/start`); resolves to the request id. */
export async function startAppLogin(
  challenge: string,
  fetchFn: typeof fetch = (...args) => globalThis.fetch(...args),
): Promise<string> {
  const response = await fetchFn('/api/v1/identity/app-login/start', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code_challenge: challenge }),
  });
  if (!response.ok) throw new HandoffError('failed');
  const { request_id: requestId } = (await response.json()) as { request_id: string };
  return requestId;
}

function defaultRandom(length: number): Uint8Array<ArrayBuffer> {
  return crypto.getRandomValues(new Uint8Array(new ArrayBuffer(length)));
}

/** PKCE: a verifier (43 characters) and its S256 challenge. */
export async function pkcePair(
  random: (length: number) => Uint8Array<ArrayBuffer> = defaultRandom,
  sha256: (data: Uint8Array<ArrayBuffer>) => Promise<ArrayBuffer> = (data) =>
    crypto.subtle.digest('SHA-256', data),
): Promise<{ verifier: string; challenge: string }> {
  const verifier = toBase64url(random(32));
  const challenge = toBase64url(
    await sha256(new TextEncoder().encode(verifier) as Uint8Array<ArrayBuffer>),
  );
  return { verifier, challenge };
}

/**
 * Sign the app in through the system browser. Resolves `signed-in` once the app's session is
 * up, `cancelled` when the user closed the browser (iOS) or `signal` fired; rejects with
 * {@link HandoffError} for a foreign state or a timeout, or with the redeem's problem.
 */
export async function signInThroughBrowser(
  deps: HandoffDeps,
  signal?: AbortSignal,
): Promise<HandoffResult> {
  const random = deps.random ?? defaultRandom;
  const { verifier, challenge } = await pkcePair(random, deps.sha256);
  const state = toBase64url(random(16));
  const requestId = await deps.start(challenge);
  const query = new URLSearchParams({ app_request: requestId, app_state: state });
  const url = `${deps.origin}/login?${query.toString()}`;
  const app = deps.bridge.plugin('App');
  const shell = deps.bridge.plugin(SHELL);

  let off = () => undefined as void;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const callback = new Promise<string | null>((resolve, reject) => {
    off = app.on('appUrlOpen', (data) => {
      const link = (data as { url?: unknown } | null)?.url;
      if (typeof link === 'string' && authCallback(link)) resolve(link);
    });
    timer = setTimeout(
      () => reject(new HandoffError('timeout')),
      deps.timeoutMs ?? HANDOFF_TIMEOUT_MS,
    );
    if (signal?.aborted) resolve(null);
    else signal?.addEventListener('abort', () => resolve(null), { once: true });
  });
  callback.catch(() => undefined);

  try {
    let link: string | null;
    try {
      // iOS resolves with the callback URL; Android opens a Custom Tab and resolves at once,
      // the callback arrives as a deep link.
      const opened = await shell.call<{ url?: unknown } | undefined>(OPEN_AUTH_SESSION, {
        url,
        callbackScheme: 'mowdeck',
      });
      link = typeof opened?.url === 'string' ? opened.url : await callback;
    } catch (error) {
      if (cancelledByUser(error)) return 'cancelled';
      throw error;
    }
    if (link === null) return 'cancelled';
    const result = authCallback(link);
    if (!result || result.state !== state) throw new HandoffError('state');
    if (result.error === 'cancelled') return 'cancelled';
    if (result.error || !result.code) throw new HandoffError('failed');
    await deps.redeem(result.code, verifier);
    return 'signed-in';
  } finally {
    off();
    clearTimeout(timer);
  }
}

function cancelledByUser(error: unknown): boolean {
  const text = `${(error as { code?: unknown } | null)?.code ?? ''} ${(error as Error | null)?.message ?? ''}`;
  return /cancel/i.test(text);
}
