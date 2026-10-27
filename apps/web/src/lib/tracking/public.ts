/**
 * The public tracking endpoint behind a share link. A plain `fetch` on purpose: no bearer token,
 * no refresh, no cookies, no referrer and nothing from the app's auth machinery, so a visitor
 * without an account never triggers a login or a token refresh. Not importing the API client
 * keeps the whole session module out of this page's bundle.
 */
import type { components } from '#lib/generated/telemetry.js';

export type PublicTrack = components['schemas']['PublicTrackOut'];

export type PublicResult =
  | { kind: 'ok'; data: PublicTrack }
  /** 404: unknown, revoked or expired token (the server does not say which). */
  | { kind: 'invalid' }
  | { kind: 'limited'; retryAfter: number | null }
  | { kind: 'error' };

export const PUBLIC_VIEW = '/api/v1/telemetry/public/tracking/view';

export function parseRetryAfter(value: string | null): number | null {
  if (!value) return null;
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds > 0 ? seconds : null;
}

export async function viewPublic(
  token: string,
  fetchFn: typeof fetch = (input, init) => fetch(input, init),
  signal?: AbortSignal,
): Promise<PublicResult> {
  try {
    const response = await fetchFn(PUBLIC_VIEW, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
      credentials: 'omit',
      referrerPolicy: 'no-referrer',
      cache: 'no-store',
      ...(signal ? { signal } : {}),
    });
    if (response.status === 404) return { kind: 'invalid' };
    if (response.status === 429) {
      return { kind: 'limited', retryAfter: parseRetryAfter(response.headers.get('Retry-After')) };
    }
    if (!response.ok) return { kind: 'error' };
    return { kind: 'ok', data: (await response.json()) as PublicTrack };
  } catch {
    return { kind: 'error' };
  }
}
