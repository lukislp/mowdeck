/**
 * Typed API clients, one per service, generated from the committed OpenAPI documents
 * (`src/lib/generated`). They share one fetch wrapper: bearer token, correlation id, one retry
 * after a refresh when a 401 arrives (with the token that replaced the rejected one; concurrent
 * 401s share one refresh), and the session ends when that refresh is refused. No request waits
 * forever: after {@link REQUEST_TIMEOUT_MS} it fails as a network problem, so pages show an
 * error with "try again" instead of loading endlessly.
 */
import createClient from 'openapi-fetch';

import { session } from '$lib/auth/session.svelte';
import type { paths as BillingPaths } from '$lib/generated/billing';
import type { paths as EntitlementsPaths } from '$lib/generated/entitlements';
import type { paths as FleetPaths } from '$lib/generated/fleet';
import type { paths as IdentityPaths } from '$lib/generated/identity';
import type { paths as NotificationsPaths } from '$lib/generated/notifications';
import type { paths as SchedulerPaths } from '$lib/generated/scheduler';
import type { paths as TelemetryPaths } from '$lib/generated/telemetry';
import type { paths as VisionPaths } from '$lib/generated/vision';

import { ApiProblem, problemFromBody } from './problem';

export interface TokenSource {
  /** `force` after a 401, with the token the server `rejected`. */
  validToken(force?: boolean, rejected?: string | null): Promise<string | null>;
  expire(): void;
}

/** A request (token wait included) fails after this long. */
export const REQUEST_TIMEOUT_MS = 30_000;

function correlationId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : Math.random().toString(36).slice(2);
}

/** Rejects with a network problem after `ms` (the timer is cleared by `clear`). */
function deadline(ms: number): {
  promise: Promise<never>;
  signal: AbortSignal;
  clear(): void;
  cancel(): void;
} {
  const abort = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const promise = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      abort.abort();
      reject(ApiProblem.network());
    }, ms);
  });
  promise.catch(() => undefined);
  return {
    promise,
    signal: abort.signal,
    clear: () => clearTimeout(timer),
    cancel: () => {
      clearTimeout(timer);
      abort.abort();
    },
  };
}

/** A fetch that authenticates every request and retries once after a 401. */
export function authFetch(
  tokens: TokenSource,
  base: typeof fetch = fetch,
  timeoutMs = REQUEST_TIMEOUT_MS,
): typeof fetch {
  return async (input, init) => {
    const request = new Request(input, init);
    const limit = deadline(timeoutMs);
    const caller = request.signal;
    // The attempts carry our signal; the caller's abort still cancels them.
    const onAbort = () => limit.cancel();
    caller.addEventListener('abort', onAbort);
    const send = async (token: string | null): Promise<Response> => {
      const attempt = request.clone();
      if (token) attempt.headers.set('Authorization', `Bearer ${token}`);
      attempt.headers.set('X-Correlation-ID', correlationId());
      try {
        return await base(attempt, { signal: limit.signal });
      } catch (error) {
        if (caller.aborted) throw error;
        throw ApiProblem.network();
      }
    };
    const run = async (): Promise<Response> => {
      const used = await tokens.validToken();
      const first = await send(used);
      if (first.status !== 401) return first;
      const fresh = await tokens.validToken(true, used);
      if (!fresh) {
        tokens.expire();
        return first;
      }
      // The refresh could not reach Identity (the old token came back): no verdict, no retry.
      if (fresh === used) return first;
      const second = await send(fresh);
      if (second.status === 401) tokens.expire();
      return second;
    };
    try {
      return await Promise.race([run(), limit.promise]);
    } finally {
      limit.clear();
      caller.removeEventListener('abort', onAbort);
    }
  };
}

/** The authenticated fetch every client uses (also for plain requests, e.g. the system probes). */
export const apiFetch = authFetch(session, (input, init) => globalThis.fetch(input, init));

export const identityApi = createClient<IdentityPaths>({ fetch: apiFetch });
export const fleetApi = createClient<FleetPaths>({ fetch: apiFetch });
export const telemetryApi = createClient<TelemetryPaths>({ fetch: apiFetch });
export const notificationsApi = createClient<NotificationsPaths>({ fetch: apiFetch });
export const schedulerApi = createClient<SchedulerPaths>({ fetch: apiFetch });
export const entitlementsApi = createClient<EntitlementsPaths>({ fetch: apiFetch });
export const billingApi = createClient<BillingPaths>({ fetch: apiFetch });
export const visionApi = createClient<VisionPaths>({ fetch: apiFetch });

/** Linking or re-reading a vendor cloud account logs in there, which can take close to a minute. */
export const SLOW_REQUEST_TIMEOUT_MS = 90_000;

/** Fleet with the longer deadline, only for the linked account calls that log in to the cloud. */
export const fleetSlowApi = createClient<FleetPaths>({
  fetch: authFetch(
    session,
    (input, init) => globalThis.fetch(input, init),
    SLOW_REQUEST_TIMEOUT_MS,
  ),
});

/** The data of an openapi-fetch result, or the problem as an exception. */
export async function unwrap<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await call;
  if (!response.ok) throw problemFromBody(response.status, error, response.headers);
  return data as T;
}
