/**
 * The browser session (ADR 0021).
 *
 * - The access token lives only in this module's memory, never in any storage.
 * - The refresh token lives only in Identity's HttpOnly cookie `mowdeck_refresh`; the login asks
 *   for `X-Mowdeck-Refresh: cookie-only`, so no response body ever carries it.
 * - Refreshes are single-flight inside a tab and across tabs: they run under the Web Lock
 *   `mowdeck-refresh`, and a tab first adopts a token another tab obtained less than 30 s ago
 *   (BroadcastChannel `mowdeck-auth`), because a reused refresh token ends the whole session.
 * - The token is refreshed 60 s before it expires; listeners (the realtime socket) get every new
 *   token for re-authentication.
 * - A refresh never waits forever: the Web Lock is given up after 15 s (a frozen or stuck tab
 *   holding it must not block the others; the refresh then runs without it) and the refresh
 *   request is aborted after 10 s (then it counts as a network failure: the token is kept and
 *   the timer retries). The lock callback always settles, so the lock is always released.
 * - A caller that waited for the lock, or a request that got a 401 for a token that has been
 *   replaced meanwhile, takes the new token instead of refreshing again: one 401 wave (and one
 *   expiry across several tabs) costs exactly one refresh.
 */
import { ApiProblem, problemFromResponse } from '#lib/api/problem.js';

export const IDENTITY = '/api/v1/identity';
const REFRESH_LEAD_MS = 60_000;
const ADOPT_WINDOW_MS = 30_000;
/** Give up waiting for another tab's refresh lock after this long. */
export const LOCK_TIMEOUT_MS = 15_000;
/** Abort a refresh request after this long. */
export const REFRESH_TIMEOUT_MS = 10_000;
const CSRF_HEADERS = { 'X-Requested-With': 'mowdeck-web', 'X-Mowdeck-Refresh': 'cookie-only' };

export interface Membership {
  account_id: string;
  account_name: string;
  is_owner: boolean;
  is_admin: boolean;
}

export interface Me {
  subject: string;
  kind: string;
  account_id: string | null;
  display_name: string | null;
  username?: string | null;
  must_change_password?: boolean;
  roles: string[];
  permissions: string[];
  memberships?: Membership[];
}

type Broadcast =
  { type: 'token'; token: string; expiresAt: number; at: number } | { type: 'logout' };

interface ChannelLike {
  postMessage(message: Broadcast): void;
  onmessage: ((event: MessageEvent<Broadcast>) => void) | null;
  close(): void;
}

interface LocksLike {
  request<T>(
    name: string,
    options: { signal?: AbortSignal },
    callback: () => Promise<T>,
  ): Promise<T>;
}

export interface SessionDeps {
  fetch: typeof fetch;
  now: () => number;
  locks: LocksLike | null;
  channel: ChannelLike | null;
}

export type SessionStatus = 'unknown' | 'anonymous' | 'authenticated';

function browserDeps(): SessionDeps {
  const hasWindow = typeof window !== 'undefined';
  return {
    fetch: (...args) => globalThis.fetch(...args),
    now: () => Date.now(),
    locks: hasWindow && navigator.locks ? (navigator.locks as unknown as LocksLike) : null,
    channel:
      hasWindow && 'BroadcastChannel' in window
        ? (new BroadcastChannel('mowdeck-auth') as unknown as ChannelLike)
        : null,
  };
}

export class SessionManager {
  status = $state<SessionStatus>('unknown');
  me = $state<Me | null>(null);
  /** Why the last session ended without a logout (shown on the login page). */
  ended = $state<'expired' | null>(null);

  #deps: SessionDeps;
  #token: string | null = null;
  #expiresAt = 0;
  #timer: ReturnType<typeof setTimeout> | undefined;
  #inflight: Promise<string | null> | null = null;
  #shared: { token: string; expiresAt: number; at: number } | null = null;
  #listeners = new Set<(token: string | null) => void>();

  constructor(deps: SessionDeps = browserDeps()) {
    this.#deps = deps;
    if (deps.channel) deps.channel.onmessage = (event) => this.#onBroadcast(event.data);
  }

  /** The current access token (may be close to expiry; see {@link validToken}). */
  get token(): string | null {
    return this.#token;
  }

  get expiresAt(): number {
    return this.#expiresAt;
  }

  /** Permission keys held somewhere in the account (an upper bound for navigation). */
  has(permission: string): boolean {
    return this.me?.permissions.includes(permission) ?? false;
  }

  /** Called with every new token, and with null on logout. Returns the unsubscribe function. */
  onToken(listener: (token: string | null) => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  /** Password login; resolves when `/me` is loaded. Throws {@link ApiProblem}. */
  async login(username: string, password: string): Promise<void> {
    await this.signIn(`${IDENTITY}/session`, { username, password });
  }

  /**
   * A login of any kind whose endpoint answers like `POST /session` (password, passkey, the
   * native app's code): token into memory, refresh token only as the cookie, then `/me`.
   */
  async signIn(url: string, body: object): Promise<void> {
    const response = await this.#post(url, body);
    if (!response.ok) throw await problemFromResponse(response);
    const token = (await response.json()) as { access_token: string; expires_in: number };
    this.#setToken(token.access_token, token.expires_in);
    this.ended = null;
    await this.loadMe();
  }

  /**
   * Native app hand-off, browser side (ADR 0050): exchange this fresh login for a one-time code
   * bound to the app's PKCE challenge. Identity ends the browser session (and drops the cookie);
   * so does this tab.
   */
  async handOffToApp(codeChallenge: string): Promise<string> {
    const token = this.#token;
    const response = await this.#deps.fetch(`${IDENTITY}/app-login/code`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ code_challenge: codeChallenge }),
    });
    if (!response.ok) throw await problemFromResponse(response);
    const { code } = (await response.json()) as { code: string };
    this.#deps.channel?.postMessage({ type: 'logout' });
    this.#clear('anonymous');
    return code;
  }

  /** Start of the app: get a token from the refresh cookie, if there is one. */
  async restore(): Promise<boolean> {
    const token = await this.refresh();
    if (token === null) {
      this.status = 'anonymous';
      return false;
    }
    try {
      await this.loadMe();
      return true;
    } catch {
      this.#clear('anonymous');
      return false;
    }
  }

  async loadMe(): Promise<Me> {
    const token = this.#token;
    const response = await this.#deps.fetch(`${IDENTITY}/me`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) throw await problemFromResponse(response);
    const me = (await response.json()) as Me;
    this.me = me;
    this.status = 'authenticated';
    return me;
  }

  /**
   * A token valid for at least the refresh lead time (refreshes when needed). `force` (after a
   * 401) with the `rejected` token the request carried: when that token has been replaced in
   * the meantime the new one is returned without another refresh.
   */
  async validToken(force = false, rejected?: string | null): Promise<string | null> {
    const now = this.#deps.now();
    if (!force && this.#token && this.#expiresAt - now > REFRESH_LEAD_MS / 2) {
      return this.#token;
    }
    if (force && rejected && this.#token && this.#token !== rejected && this.#expiresAt > now) {
      return this.#token;
    }
    return this.refresh();
  }

  /** Single-flight refresh (tab and cross-tab). Resolves to null when the session is gone. */
  refresh(): Promise<string | null> {
    if (!this.#inflight) {
      this.#inflight = this.#refreshShared(this.#token).finally(() => {
        this.#inflight = null;
      });
    }
    return this.#inflight;
  }

  /** The API said the session is gone (refresh failed after a 401). */
  expire(): void {
    if (this.status === 'authenticated') this.ended = 'expired';
    this.#clear('anonymous');
  }

  async logout(): Promise<void> {
    try {
      await this.#deps.fetch(`${IDENTITY}/session/logout`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: CSRF_HEADERS,
      });
    } catch {
      // offline: the cookie is cleared server side on the next refresh attempt anyway
    }
    this.#deps.channel?.postMessage({ type: 'logout' });
    this.ended = null;
    this.#clear('anonymous');
  }

  /** `seen`: the token when the refresh was asked for (a newer one makes it unnecessary). */
  async #refreshShared(seen: string | null): Promise<string | null> {
    const run = async (): Promise<string | null> => {
      const now = this.#deps.now();
      // Another tab (broadcast) or this one replaced the token while we waited for the lock.
      if (this.#token && this.#token !== seen && this.#expiresAt - now > REFRESH_LEAD_MS) {
        return this.#token;
      }
      const shared = this.#shared;
      if (
        shared &&
        now - shared.at < ADOPT_WINDOW_MS &&
        shared.expiresAt - now > REFRESH_LEAD_MS &&
        shared.token !== this.#token
      ) {
        this.#applyToken(shared.token, shared.expiresAt);
        return shared.token;
      }
      const abort = new AbortController();
      const timer = setTimeout(() => abort.abort(), REFRESH_TIMEOUT_MS);
      let body: { access_token: string; expires_in: number };
      try {
        const response = await this.#deps.fetch(`${IDENTITY}/session/refresh`, {
          method: 'POST',
          credentials: 'same-origin',
          headers: CSRF_HEADERS,
          signal: abort.signal,
        });
        // A server error is no verdict on the session: keep the token and try again later.
        if (response.status >= 500 || response.status === 429) throw ApiProblem.network();
        if (!response.ok) return null;
        body = (await response.json()) as { access_token: string; expires_in: number };
      } catch (error) {
        if (error instanceof ApiProblem) throw error;
        throw ApiProblem.network();
      } finally {
        clearTimeout(timer);
      }
      this.#setToken(body.access_token, body.expires_in);
      this.#deps.channel?.postMessage({
        type: 'token',
        token: body.access_token,
        expiresAt: this.#expiresAt,
        at: this.#deps.now(),
      });
      return body.access_token;
    };
    try {
      return await this.#locked(run);
    } catch (error) {
      // Offline: keep the current token (it may still work); the timer retries.
      if (error instanceof ApiProblem && error.status === 0) {
        this.#schedule(30_000);
        return this.#token;
      }
      throw error;
    }
  }

  /** Run `task` under the cross-tab lock; without it when the lock is not granted in time. */
  async #locked<T>(task: () => Promise<T>): Promise<T> {
    const locks = this.#deps.locks;
    if (!locks) return task();
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), LOCK_TIMEOUT_MS);
    let granted = false;
    try {
      return await locks.request('mowdeck-refresh', { signal: abort.signal }, () => {
        granted = true;
        clearTimeout(timer);
        return task();
      });
    } catch (error) {
      // A tab that holds the lock and does not finish (frozen, stuck) must not block this one.
      if (!granted && abort.signal.aborted) return task();
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  #onBroadcast(message: Broadcast): void {
    if (message.type === 'logout') {
      this.#clear('anonymous');
      return;
    }
    this.#shared = { token: message.token, expiresAt: message.expiresAt, at: message.at };
    if (this.status === 'authenticated' && message.token !== this.#token) {
      this.#applyToken(message.token, message.expiresAt);
    }
  }

  #setToken(token: string, expiresIn: number): void {
    this.#applyToken(token, this.#deps.now() + expiresIn * 1000);
  }

  #applyToken(token: string, expiresAt: number): void {
    this.#token = token;
    this.#expiresAt = expiresAt;
    this.#schedule(Math.max(1_000, expiresAt - this.#deps.now() - REFRESH_LEAD_MS));
    for (const listener of this.#listeners) listener(token);
  }

  #schedule(delay: number): void {
    clearTimeout(this.#timer);
    this.#timer = setTimeout(() => {
      void this.refresh()
        .then((token) => {
          if (token === null) this.expire();
        })
        .catch(() => this.#schedule(30_000));
    }, delay);
  }

  #clear(status: SessionStatus): void {
    clearTimeout(this.#timer);
    const had = this.#token !== null;
    this.#token = null;
    this.#expiresAt = 0;
    this.me = null;
    this.status = status;
    if (had) for (const listener of this.#listeners) listener(null);
  }

  #post(url: string, body: unknown): Promise<Response> {
    return this.#deps.fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', ...CSRF_HEADERS },
      body: JSON.stringify(body),
    });
  }
}

/** The session of this tab. */
export const session = new SessionManager();
