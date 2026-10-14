/**
 * The system page of the operator console: which services answer their public `GET` endpoints
 * through the gateway, with status and latency measured in this browser. The services' `/readyz`
 * is not routed through Traefik, so this is reachability, not readiness, and the page says so.
 * Response bodies are discarded (no mower data is read or shown); the Realtime service has no
 * HTTP endpoint and is shown by the state of this tab's socket instead.
 */

export interface ServiceProbe {
  service: string;
  /** Public path under the gateway, answered without mower data of other accounts. */
  path: string;
}

export const SERVICE_PROBES: readonly ServiceProbe[] = [
  { service: 'identity', path: '/api/v1/identity/.well-known/jwks.json' },
  { service: 'entitlements', path: '/api/v1/entitlements/plans' },
  { service: 'fleet', path: '/api/v1/fleet/connection-kinds' },
  { service: 'telemetry', path: '/api/v1/telemetry/accounts/me/mowers/states' },
  { service: 'scheduler', path: '/api/v1/scheduler/schedules' },
  { service: 'notifications', path: '/api/v1/notifications/push/vapid-public-key' },
  { service: 'device-gateway', path: '/api/v1/device-gateway/mowers' },
  { service: 'vision', path: '/api/v1/vision/status' },
];

export type ProbeVerdict = 'up' | 'degraded' | 'down';

export interface ProbeResult {
  service: string;
  path: string;
  status: number | null;
  latencyMs: number | null;
  verdict: ProbeVerdict;
}

/** 2xx and 3xx: up; 401 and 403 answer too (the service runs); 5xx, 404 or no answer: down. */
export function verdictOf(status: number | null): ProbeVerdict {
  if (status === null || status === 0) return 'down';
  if (status < 400) return 'up';
  if (status === 401 || status === 403 || status === 429) return 'degraded';
  return 'down';
}

/** Probe one service with `fetchFn` (the authenticated fetch); never throws. */
export async function probeService(
  probe: ServiceProbe,
  fetchFn: typeof fetch,
  now: () => number = () => performance.now(),
): Promise<ProbeResult> {
  const started = now();
  try {
    const response = await fetchFn(probe.path, { cache: 'no-store' });
    const latencyMs = Math.max(0, Math.round(now() - started));
    // Read and drop the body so the connection is released; nothing of it is kept.
    await response.arrayBuffer().catch(() => undefined);
    return { ...probe, status: response.status, latencyMs, verdict: verdictOf(response.status) };
  } catch {
    return { ...probe, status: null, latencyMs: null, verdict: 'down' };
  }
}

/** The operator's hosted vision default as `GET /api/v1/vision/status` reports it (never a key). */
export interface VisionDefault {
  enabled: boolean;
  /** The model string, only when the default is enabled. */
  model: string | null;
}

export const VISION_STATUS_URL = '/api/v1/vision/status';

/** Validate an untrusted status body; null unless it is a Vision status. */
export function parseVisionDefault(raw: unknown): VisionDefault | null {
  if (!raw || typeof raw !== 'object') return null;
  const body = raw as Record<string, unknown>;
  if (typeof body.hosted_default_enabled !== 'boolean') return null;
  const enabled = body.hosted_default_enabled;
  const model = typeof body.hosted_default_model === 'string' ? body.hosted_default_model : null;
  return { enabled, model: enabled ? model : null };
}

/**
 * Read the hosted vision default with `fetchFn` (the authenticated fetch); null when the Vision
 * service is not deployed or does not answer (404, the SPA fallback, a network error). Never throws.
 */
export async function probeVisionDefault(fetchFn: typeof fetch): Promise<VisionDefault | null> {
  try {
    const response = await fetchFn(VISION_STATUS_URL, {
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    });
    if (!response.ok) return null;
    if (!(response.headers.get('content-type') ?? '').includes('json')) return null;
    return parseVisionDefault(await response.json());
  } catch {
    return null;
  }
}
