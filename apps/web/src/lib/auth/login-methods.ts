/**
 * `GET /login-methods`: which sign-in methods the installation offers (flags only, never a
 * provider setting). The sign-in page asks for passkeys, GitHub and Apple at once; calls that
 * overlap share one request, so the page costs one request against the per-address rate limit
 * instead of three. Nothing is kept once the answer is there: the next page asks again.
 */
import { IDENTITY } from './session.svelte';

export type LoginMethods = Record<string, unknown>;

const inFlight = new WeakMap<typeof fetch, Promise<LoginMethods | null>>();

const defaultFetch: typeof fetch = (...args) => globalThis.fetch(...args);

/** The flags of the installation, or null when they cannot be read; never rejects. */
export function loginMethods(fetchFn: typeof fetch = defaultFetch): Promise<LoginMethods | null> {
  const pending = inFlight.get(fetchFn);
  if (pending) return pending;
  const request = read(fetchFn).finally(() => inFlight.delete(fetchFn));
  inFlight.set(fetchFn, request);
  return request;
}

async function read(fetchFn: typeof fetch): Promise<LoginMethods | null> {
  try {
    const response = await fetchFn(`${IDENTITY}/login-methods`, { credentials: 'omit' });
    if (!response.ok) return null;
    const body: unknown = await response.json();
    return body && typeof body === 'object' ? (body as LoginMethods) : null;
  } catch {
    return null;
  }
}

/** Whether the installation offers `method` (`passkey`, `github`, `apple`); never rejects. */
export async function methodEnabled(method: string, fetchFn?: typeof fetch): Promise<boolean> {
  return (await loginMethods(fetchFn))?.[method] === true;
}
