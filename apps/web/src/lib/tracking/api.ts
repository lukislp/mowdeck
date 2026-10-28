/** Authenticated calls of the theft tracking view (Telemetry, `/mowers/{id}/tracking`). */
import { apiFetch, telemetryApi, unwrap } from '#lib/api/client.js';
import { problemFromBody } from '#lib/api/problem.js';
import type { components } from '#lib/generated/telemetry.js';

type Schemas = components['schemas'];
export type TrackingStatus = Schemas['TrackingStatus'];
export type TrackingSession = Schemas['TrackingSessionOut'];
export type TrackingLink = Schemas['TrackingLinkOut'];
export type TrackingLinkCreated = Schemas['TrackingLinkCreated'];
export type StopReason = Schemas['TrackingStopIn']['reason'];

const path = (mowerId: string) => ({ path: { mower_id: mowerId } });

/** The status; with `after` only the points after that time (polling). */
export function getTracking(
  mowerId: string,
  query: { sessionId?: string; after?: string | null } = {},
): Promise<TrackingStatus> {
  return unwrap(
    telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/tracking', {
      params: {
        path: path(mowerId).path,
        query: {
          ...(query.sessionId ? { session_id: query.sessionId } : {}),
          ...(query.after ? { after: query.after } : {}),
        },
      },
    }),
  );
}

export function startTracking(mowerId: string): Promise<TrackingSession> {
  return unwrap(
    telemetryApi.POST('/api/v1/telemetry/mowers/{mower_id}/tracking', {
      params: { path: path(mowerId).path },
    }),
  );
}

export function stopTracking(mowerId: string, reason: StopReason): Promise<TrackingSession> {
  return unwrap(
    telemetryApi.POST('/api/v1/telemetry/mowers/{mower_id}/tracking/stop', {
      params: { path: path(mowerId).path },
      body: { reason },
    }),
  );
}

export function createLink(mowerId: string, hours?: number): Promise<TrackingLinkCreated> {
  return unwrap(
    telemetryApi.POST('/api/v1/telemetry/mowers/{mower_id}/tracking/links', {
      params: { path: path(mowerId).path },
      body: hours ? { hours } : {},
    }),
  );
}

export function listLinks(mowerId: string): Promise<TrackingLink[]> {
  return unwrap(
    telemetryApi.GET('/api/v1/telemetry/mowers/{mower_id}/tracking/links', {
      params: { path: path(mowerId).path },
    }),
  );
}

export async function revokeLink(mowerId: string, linkId: string): Promise<void> {
  await unwrap(
    telemetryApi.DELETE('/api/v1/telemetry/mowers/{mower_id}/tracking/links/{link_id}', {
      params: { path: { mower_id: mowerId, link_id: linkId } },
    }),
  );
}

export async function deleteSession(mowerId: string, sessionId: string): Promise<void> {
  await unwrap(
    telemetryApi.DELETE('/api/v1/telemetry/mowers/{mower_id}/tracking/sessions/{session_id}', {
      params: { path: { mower_id: mowerId, session_id: sessionId } },
    }),
  );
}

/** The PDF report as a blob (the bearer header is needed, so no plain link). */
export async function fetchReport(
  mowerId: string,
  sessionId: string,
  lang: 'de' | 'en',
): Promise<Blob> {
  const query = new URLSearchParams({ session_id: sessionId, lang });
  const response = await apiFetch(
    `/api/v1/telemetry/mowers/${encodeURIComponent(mowerId)}/tracking/report.pdf?${query}`,
  );
  if (!response.ok) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      /* not a problem document */
    }
    throw problemFromBody(response.status, body, response.headers);
  }
  return response.blob();
}

/** Hands a blob to the user as a file download. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.rel = 'noopener';
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30_000);
}
